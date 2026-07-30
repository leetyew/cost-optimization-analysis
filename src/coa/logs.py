"""Log line classifier, pairing state machine, and action-field extraction.

This is the hard part of the project. Two responsibilities are kept strictly
separate, because `coa reparse` depends on the split:

* **Pairing** (`ingest_log`) is positional — it needs the surrounding lines, so it
  only ever runs while streaming a source file.
* **Field extraction** (`parse_action_line`) is a pure function of one raw line.
  It runs during ingest *and* again during reparse, straight off the stored
  `raw_action_line`, so a parser fix costs seconds instead of a 2 GB re-read.

The pairing rule comes from the operator: an `action type - ...` line is trusted
to belong to the `[se10]` on the line above it only when that line is a
`web_search_call`. Async logging interleaves lines from concurrent workers, so
that adjacency is frequently broken. Broken cases are recorded as orphans rather
than guessed at, and the orphan rate is a reported data-quality KPI.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from .anomalies import AnomalyRecorder
from .config import Config

TIMESTAMPED_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+) \| ([\w.]+) \| \[(\d+)\] (.*)$"
)
# The trailing group is optional: `action type - open_page` with no fields at all
# is a real shape (PLAN.md §10), and it must classify as an ACTION so it reaches
# ACTION_FIELD_MISSING rather than being silently binned as noise.
ACTION_RE = re.compile(r"^action type - ([\w ]+?)\s*(?:,\s*(.*))?$")
WS_ID_RE = re.compile(r"id - (ws_\w+)")
# Stop at a `, ` field boundary rather than consuming it: `url - X, pattern - Y`
# would otherwise capture "X,". A bare `\S+` is wrong, and rstrip(",") would be too
# blunt — URLs may legitimately contain commas inside a path or query string.
URL_RE = re.compile(r"url - (\S+?)(?=,\s|$)")
PATTERN_RE = re.compile(r"pattern - (.*)$")

WEB_SEARCH_MARKER = "Response tool type - web_search_call"
# No trailing space: the line is stripped before this test, so a degenerate
# `action type - ` with no fields would otherwise miss the prefix and be dropped.
ACTION_PREFIX = "action type -"
QUERIES_MARKER = ", queries - "
QUERY_PREFIX = "query - "
REPLACEMENT_CHAR = "�"

# `find in page` and `find_in_page` are both observed. Normalizing on the way in
# means grouping works without every downstream query knowing about the alias;
# the original spelling always survives in raw_action_line.
KNOWN_ACTION_TYPES = {"search", "open_page", "find_in_page"}


@dataclass
class ParsedAction:
    """Everything extractable from a single action line, plus how much to trust it."""

    action_type: str
    query_raw: str | None = None
    queries_raw: str | None = None
    queries: list[str] | None = None
    url: str | None = None
    pattern: str | None = None
    parse_conf: str = "clean"
    notes: list[tuple[str, str]] = field(default_factory=list)

    def note(self, code: str, detail: str) -> None:
        self.notes.append((code, detail))


def _norm_ws(text: str) -> str:
    """Collapse whitespace for comparison. Used only to compare, never to store."""
    return " ".join(text.split())


def _repair_queries(query_raw: str, queries_raw: str, max_sane: int) -> tuple[list[str], str, list]:
    """Split `queries` on commas, repairing splits that landed inside a query.

    Double quotes are *content* in this field, not delimiters, so a quote-aware
    CSV parse would corrupt it — plain comma splitting is correct here, and the
    resulting over-split is repaired using the redundant singular `query` value as
    ground truth. That redundancy is the one honest signal available.

    Returns (items, parse_conf, notes).
    """
    notes: list[tuple[str, str]] = []
    items = [s.strip() for s in queries_raw.split(",")]
    items = [s for s in items if s]

    q_norm = _norm_ws(query_raw)
    present = q_norm in _norm_ws(queries_raw)

    if present and "," in query_raw:
        # The split cut through the query itself. Find the run of adjacent items
        # that reassembles to it and glue them back together.
        for i in range(len(items)):
            for j in range(i + 1, len(items) + 1):
                if _norm_ws(", ".join(items[i:j])) == q_norm:
                    items = [*items[:i], query_raw.strip(), *items[j:]]
                    notes.append(
                        ("COMMA_IN_QUERY", f"merged {j - i} comma-split items into {query_raw!r}")
                    )
                    return items, "heuristic", notes

    if not present:
        notes.append(
            (
                "QUERY_NOT_IN_QUERIES",
                f"query {query_raw!r} not found within queries {queries_raw!r}",
            )
        )
        return items, "heuristic", notes

    # Sanity gate: real queries are short. An over-long item means the split went
    # wrong in a way the `query` field could not reveal.
    if any(len(s) > max_sane for s in items):
        notes.append(("QUERIES_ITEM_TOO_LONG", f"item exceeds {max_sane} chars"))
        return items, "heuristic", notes

    return items, "clean", notes


def parse_action_line(raw: str, max_sane_query_chars: int = 300) -> ParsedAction:
    """Extract fields from one action line. Pure — no I/O, no surrounding context.

    Never raises. A line it cannot understand comes back with parse_conf="failed"
    and a note; the caller still stores the row.
    """
    m = ACTION_RE.match(raw.strip())
    if not m:
        pa = ParsedAction(action_type="?", parse_conf="failed")
        pa.note("ACTION_UNPARSEABLE", "line did not match the action pattern")
        return pa

    raw_type, rest = m.group(1), (m.group(2) or "")
    action_type = raw_type.strip().lower().replace(" ", "_")
    pa = ParsedAction(action_type=action_type)

    if action_type not in KNOWN_ACTION_TYPES:
        pa.note(
            "UNKNOWN_ACTION_TYPE", f"action type {raw_type!r} is not one of {KNOWN_ACTION_TYPES}"
        )
        pa.parse_conf = "heuristic"

    if action_type == "search":
        _parse_search(pa, rest, max_sane_query_chars)
    elif action_type == "open_page":
        _parse_url_only(pa, rest)
    elif action_type == "find_in_page":
        _parse_url_only(pa, rest)
        pm = PATTERN_RE.search(rest)
        if pm:
            pa.pattern = pm.group(1).strip()
        else:
            pa.note("ACTION_FIELD_MISSING", "find_in_page without a `pattern - ` field")
            pa.parse_conf = "heuristic"
    else:
        # Unknown type: grab a url if one happens to be present, keep everything raw.
        um = URL_RE.search(rest)
        if um:
            pa.url = um.group(1)

    return pa


def _parse_search(pa: ParsedAction, rest: str, max_sane: int) -> None:
    """Pull `query` and `queries` out of a search action's field text."""
    n_markers = rest.count(QUERIES_MARKER)
    if n_markers > 1:
        pa.note("MULTI_QUERIES_MARKER", f"{QUERIES_MARKER!r} appears {n_markers} times")

    if n_markers:
        # Last occurrence wins (PLAN.md §3) — the query may itself contain the marker.
        head, _, tail = rest.rpartition(QUERIES_MARKER)
        pa.queries_raw = tail.strip()
    else:
        head, pa.queries_raw = rest, None

    head = head.strip()
    if head.startswith(QUERY_PREFIX):
        pa.query_raw = head[len(QUERY_PREFIX) :].strip()
    else:
        pa.note("ACTION_FIELD_MISSING", "search without a `query - ` field")
        pa.parse_conf = "failed"
        return

    if pa.queries_raw:
        items, conf, notes = _repair_queries(pa.query_raw, pa.queries_raw, max_sane)
        pa.queries = items
        pa.notes.extend(notes)
        if conf != "clean":
            pa.parse_conf = "heuristic"
    else:
        # No `queries` field at all: the singular query is all we have. Not an
        # error, just less redundancy to cross-check against.
        pa.queries = [pa.query_raw]


def _parse_url_only(pa: ParsedAction, rest: str) -> None:
    um = URL_RE.search(rest)
    if um:
        pa.url = um.group(1)
    else:
        pa.note("ACTION_FIELD_MISSING", f"{pa.action_type} without a `url - ` field")
        pa.parse_conf = "heuristic"


def _context(lines: Sequence[str], i: int, span: int) -> list[str]:
    """+/- span raw lines around index i, with i marked, ready to paste."""
    lo, hi = max(0, i - span), min(len(lines), i + span + 1)
    return [(">> " if j == i else "   ") + lines[j] for j in range(lo, hi)]


def ingest_log(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    lines: Sequence[str],
    cfg: Config,
) -> Counter:
    """Parse one log file into log_events / search_calls / query_instances.

    Single pass, per file. There is no cross-file ordering to exploit — the race
    is between concurrent workers writing into the same file — so each file is
    independent and can be transacted on its own.
    """
    stats: Counter = Counter()
    span = cfg.anomalies.context_lines
    max_sane = cfg.thresholds.max_sane_query_chars

    pending_ws: tuple[str, str, str | None] | None = None  # (se10, ts, ws_id)
    last_action_id: int | None = None

    for i, raw in enumerate(lines):
        line_no = i + 1
        stats["lines"] += 1

        if REPLACEMENT_CHAR in raw:
            stats["encoding_damaged"] += 1
            rec.record(
                "ENCODING",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw,
                detail="undecodable byte replaced with U+FFFD; line parsed anyway",
            )

        ts_match = TIMESTAMPED_RE.match(raw)
        if ts_match:
            stats["timestamped"] += 1
            ts, level, module, se10, message = ts_match.groups()
            if WEB_SEARCH_MARKER in message:
                stats["web_search_call"] += 1
                ws_match = WS_ID_RE.search(message)
                ws_id = ws_match.group(1) if ws_match else None
                conn.execute(
                    "INSERT INTO log_events (se10, ts, level, module, ws_id, message, "
                    "src_file, src_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (se10, ts, level, module, ws_id, message, src_name, line_no),
                )
                pending_ws = (se10, ts, ws_id)
            else:
                # A timestamped line that is not a web_search_call breaks adjacency.
                pending_ws = None
            last_action_id = None
            continue

        # Prefix check as well as pattern match: a line announcing itself as an
        # action but failing the pattern (an unexpected character in the type, say)
        # must not fall through to OTHER, which would silently drop it.
        stripped = raw.strip()
        if ACTION_RE.match(stripped) or stripped.startswith(ACTION_PREFIX):
            stats["action"] += 1
            pa = parse_action_line(raw, max_sane)
            pairing = "strict" if pending_ws else "orphan"
            stats[pairing] += 1
            stats[f"conf_{pa.parse_conf}"] += 1
            se10, ts, ws_id = pending_ws if pending_ws else (None, None, None)

            cur = conn.execute(
                "INSERT INTO search_calls (se10, ts, ws_id, action_type, raw_action_line, "
                "query_raw, queries_raw, queries_json, url, pattern, pairing, parse_conf, "
                "src_file, src_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    se10,
                    ts,
                    ws_id,
                    pa.action_type,
                    raw,
                    pa.query_raw,
                    pa.queries_raw,
                    json.dumps(pa.queries) if pa.queries is not None else None,
                    pa.url,
                    pa.pattern,
                    pairing,
                    pa.parse_conf,
                    src_name,
                    line_no,
                ),
            )
            call_id = cur.lastrowid
            _insert_queries(conn, call_id, se10, pa)

            if pairing == "orphan":
                rec.record(
                    "ORPHAN_ACTION",
                    src_file=src_name,
                    src_line=line_no,
                    context=_context(lines, i, span),
                    detail="action line not directly preceded by a web_search_call line",
                )
            for code, detail in pa.notes:
                rec.record(
                    code,
                    se10=se10,
                    src_file=src_name,
                    src_line=line_no,
                    raw_excerpt=raw,
                    detail=detail,
                )

            pending_ws = None  # consumed; a second adjacent action is an orphan
            last_action_id = call_id
            continue

        # OTHER. A non-boilerplate line straight after an action may be that
        # action's wrapped continuation. Record the suspicion and keep both raw
        # forms; do NOT re-run extraction on the merged text, because the operator
        # is not certain action lines are always single-line.
        stats["other"] += 1
        if last_action_id is not None and raw.strip() and not cfg.is_noise(raw):
            conn.execute(
                "UPDATE search_calls SET raw_wrap_line = ?, possible_wrap = 1 WHERE id = ?",
                (raw, last_action_id),
            )
            stats["possible_wrap"] += 1
            rec.record(
                "POSSIBLE_WRAPPED_ACTION",
                src_file=src_name,
                src_line=line_no,
                context=_context(lines, i, span),
                detail="non-noise line follows an action line; may be a wrapped continuation",
            )
        pending_ws = None
        last_action_id = None

    # Pairing loss KPI: how many web_search_call lines never got an adjacent action.
    stats["pairing_delta"] = stats["web_search_call"] - stats["strict"]
    return stats


def _insert_queries(
    conn: sqlite3.Connection, call_id: int, se10: str | None, pa: ParsedAction
) -> None:
    """Write one query_instances row per query on this call.

    The singular `query` is flagged `is_billed_query` — billing is per call, so
    cost attribution must use exactly one query per call or archetype shares stop
    summing to 100%. The plural `queries` items are stored for redundancy
    analysis only.
    """
    if pa.query_raw is None and not pa.queries:
        return
    rows = []
    if pa.query_raw is not None:
        rows.append((call_id, se10, pa.query_raw, 1))
    for q in pa.queries or []:
        if pa.query_raw is not None and _norm_ws(q) == _norm_ws(pa.query_raw):
            continue  # already stored as the billed query
        rows.append((call_id, se10, q, 0))
    conn.executemany(
        "INSERT INTO query_instances (search_call_id, se10, query_text, is_billed_query) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def reparse(conn: sqlite3.Connection, rec: AnomalyRecorder, cfg: Config) -> Counter:
    """Re-run field extraction over stored raw lines, without touching sources.

    This is what makes the operator loop affordable: a parser fix is validated
    against the whole corpus in seconds rather than by re-reading 2 GB of zipped
    logs. Pairing is *not* recomputed — it is positional and its inputs are gone —
    so se10/ts/ws_id/pairing are left exactly as ingest determined them.
    """
    stats: Counter = Counter()
    max_sane = cfg.thresholds.max_sane_query_chars
    rows = conn.execute(
        "SELECT id, se10, raw_action_line, src_file, src_line FROM search_calls ORDER BY id"
    ).fetchall()

    for row in rows:
        pa = parse_action_line(row["raw_action_line"], max_sane)
        stats["reparsed"] += 1
        stats[f"conf_{pa.parse_conf}"] += 1
        conn.execute(
            "UPDATE search_calls SET action_type = ?, query_raw = ?, queries_raw = ?, "
            "queries_json = ?, url = ?, pattern = ?, parse_conf = ? WHERE id = ?",
            (
                pa.action_type,
                pa.query_raw,
                pa.queries_raw,
                json.dumps(pa.queries) if pa.queries is not None else None,
                pa.url,
                pa.pattern,
                pa.parse_conf,
                row["id"],
            ),
        )
        conn.execute("DELETE FROM query_instances WHERE search_call_id = ?", (row["id"],))
        _insert_queries(conn, row["id"], row["se10"], pa)
        for code, detail in pa.notes:
            rec.record(
                code,
                se10=row["se10"],
                src_file=row["src_file"],
                src_line=row["src_line"],
                raw_excerpt=row["raw_action_line"],
                detail=detail,
            )
    return stats
