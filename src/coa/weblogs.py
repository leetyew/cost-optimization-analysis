"""Web-search log parser: `logs/jsonl/*.jsonl` -> runs / search_calls / query_instances.

One JSON object per line, one merchant per object:

    {"<se10>": {"run_0": {"usage_metadata": {...},
                          "response_reasoning": {...},
                          "web_search_calls": [{"id", "status", "action_type",
                                                "query", "queries"}]}}}

**This replaced a 460-line text-log parser**, and the reason is worth keeping in
view: merchant and run are *structural keys* here, so attribution is an exact
join. Everything the old parser existed to reconstruct -- adjacency pairing,
orphan tracking, burst-based run assignment, comma-split repair of a flattened
`queries` string -- is given directly. The two sources were reconciled first and
agreed exactly (592,710 search / 87,854 open_page / 14,131 find_in_page), so
nothing was lost by dropping the text logs except timestamps, which no
deliverable needs: position within a run comes from array order.

Cost attribution is unchanged and now has evidence behind it. One call is one
billed search -- 592,710 calls over 19,269 merchants at ~2 runs each is ~14 per
run, matching the operator's independently-stated figure. The plural `queries`
is a set of sub-queries *inside* one billed call, so it feeds redundancy
analysis and never cost.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from .anomalies import AnomalyRecorder, note_encoding_damage
from .config import Config

BAD_JSON_EXCERPT = 500

# Observed across the whole corpus. An unknown type is stored verbatim and
# flagged rather than dropped -- the set may grow.
KNOWN_ACTION_TYPES = frozenset({"search", "open_page", "find_in_page"})

# `completed` is overwhelmingly the norm. Anything else is a cost question
# (was it billed?) and a quality question (did the answer lose evidence?).
EXPECTED_STATUS = "completed"

# usage_metadata keys copied onto `runs`. Deliberately not a SELECT *: the
# subset semantics of cache_read/reasoning are load-bearing for the cost model
# and belong in a named, documented column.
USAGE_KEYS: tuple[str, ...] = (
    "service_tier",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read",
    "reasoning",
)


@dataclass(frozen=True)
class ParsedCall:
    """Fields extracted from one `web_search_calls` entry.

    Pure over a single dict, exactly as `parse_action_line` was pure over a
    single line: `coa reparse` re-runs it against the stored `raw_json` without
    reading a source file.
    """

    action_type: str
    call_id: str | None
    status: str | None
    query_raw: str | None
    queries: list[str] | None
    url: str | None
    details: str | None
    parse_conf: str
    notes: list[tuple[str, str]]


def parse_call(call: object) -> ParsedCall:
    """Extract one call. Never raises; an unusable shape comes back as failed."""
    notes: list[tuple[str, str]] = []
    if not isinstance(call, dict):
        notes.append(("CALL_NOT_AN_OBJECT", f"expected an object, got {type(call).__name__}"))
        return ParsedCall("?", None, None, None, None, None, None, "failed", notes)

    action_type = str(call.get("action_type") or "?").strip()
    conf = "clean"
    if action_type not in KNOWN_ACTION_TYPES:
        notes.append(
            (
                "UNKNOWN_ACTION_TYPE",
                f"action_type {action_type!r} is not one of {sorted(KNOWN_ACTION_TYPES)}",
            )
        )
        conf = "heuristic"

    status = call.get("status")
    if status is not None and status != EXPECTED_STATUS:
        notes.append(
            ("CALL_STATUS_NOT_COMPLETED", f"status is {status!r}, not {EXPECTED_STATUS!r}")
        )

    query_raw = call.get("query")
    query_raw = query_raw if isinstance(query_raw, str) else None
    raw_queries = call.get("queries")
    queries: list[str] | None = None
    if isinstance(raw_queries, list):
        queries = [q if isinstance(q, str) else json.dumps(q, sort_keys=True) for q in raw_queries]
    elif raw_queries is not None:
        notes.append(("QUERIES_NOT_A_LIST", f"`queries` is {type(raw_queries).__name__}"))
        conf = "heuristic"

    # The operator believed the singular query always appears verbatim in the
    # plural list. On real data it does not, 2.9% of the time — but nearly all of
    # those are the model requoting its own query rather than a different search:
    #
    #   query      "MARIANNA" "Cathedral City, CA 92234"
    #   queries[n] "Marianna" "Cathedral City" "CA 92234"
    #
    # Same terms, different case and quote grouping. Flagging that as a mismatch
    # buries the cases where the two fields genuinely disagree, so the check is
    # tiered: exact, then requoted, then actually absent. Only the last is an
    # anomaly; the middle is counted so the reformulation rate stays visible.
    if query_raw is not None and queries is not None:
        if query_raw in queries:
            pass
        elif _norm_query(query_raw) in {_norm_query(q) for q in queries}:
            notes.append(
                (
                    "QUERY_REQUOTED",
                    f"query {query_raw!r} appears in `queries` only after normalizing case "
                    f"and quoting",
                )
            )
        else:
            notes.append(
                (
                    "QUERY_NOT_IN_QUERIES",
                    f"query {query_raw!r} is absent from its {len(queries)}-item `queries` "
                    f"list even after normalizing; entries: {queries[:4]}",
                )
            )

    if action_type == "search" and query_raw is None:
        notes.append(
            (
                "CALL_FIELD_MISSING",
                "search call has no `query`; it cannot be attributed to an archetype",
            )
        )
        conf = "heuristic"

    url = call.get("url") if isinstance(call.get("url"), str) else None
    details = call.get("details") if isinstance(call.get("details"), str) else None
    if action_type == "open_page" and url is None:
        notes.append(("CALL_FIELD_MISSING", "open_page call has no `url`"))
        conf = "heuristic"

    call_id = call.get("id")
    return ParsedCall(
        action_type=action_type,
        call_id=call_id if isinstance(call_id, str) else None,
        status=status if isinstance(status, str) else None,
        query_raw=query_raw,
        queries=queries,
        url=url,
        details=details,
        parse_conf=conf,
        notes=notes,
    )


# Quote characters and separators the model shuffles while meaning the same
# search. Everything outside this is left alone: stripping more would start
# collapsing genuinely different queries into each other.
_QUERY_NOISE = re.compile(r"[\"\u201c\u201d\u2018\u2019\',;:()\[\]]+")


def _norm_query(text: str) -> str:
    """Comparison form for query text: casefold, drop quoting, collapse spaces.

    Used only to decide whether the singular `query` and a `queries` entry are
    the same search written differently. Never stored — `query_text` keeps the
    verbatim string, because P3 templates the real text and a normalized form
    would mask the PII it must replace.
    """
    return " ".join(_QUERY_NOISE.sub(" ", text).casefold().split())


def _run_id(run_key: str) -> int | None:
    """Integer run index from a `run_<n>` key. Exact -- no burst heuristic."""
    tail = run_key.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def ingest_weblog(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    lines: Iterable[str],
    cfg: Config,
) -> Counter:
    """Parse one `logs/jsonl/*.jsonl` file. One merchant per line."""
    stats: Counter = Counter()

    for i, raw in enumerate(lines):
        line_no = i + 1
        stats["lines"] += 1
        if not raw.strip():
            continue
        if note_encoding_damage(
            rec, src_name, line_no, raw, detail="undecodable byte in web-search log record"
        ):
            stats["encoding_damaged"] += 1

        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            stats["wl_bad_json"] += 1
            rec.record(
                "BAD_JSON_LINE",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail=f"web-search log line did not parse as JSON: {exc}",
            )
            continue

        if not isinstance(record, dict):
            stats["wl_not_an_object"] += 1
            rec.record(
                "WEBLOG_SHAPE_UNEXPECTED",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail=f"expected an object keyed by se10, got {type(record).__name__}",
            )
            continue

        for se10, runs in record.items():
            _ingest_merchant(conn, rec, src_name, line_no, str(se10), runs, stats)

    return stats


def _ingest_merchant(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    line_no: int,
    se10: str,
    runs: object,
    stats: Counter,
) -> None:
    """Store every run for one merchant, and every call within each run."""
    if not isinstance(runs, dict):
        stats["wl_runs_not_an_object"] += 1
        rec.record(
            "WEBLOG_SHAPE_UNEXPECTED",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            detail=f"expected run keys under se10, got {type(runs).__name__}",
        )
        return

    stats["wl_merchants"] += 1
    for run_key, run in runs.items():
        if not isinstance(run, dict):
            stats["wl_run_not_an_object"] += 1
            rec.record(
                "WEBLOG_SHAPE_UNEXPECTED",
                se10=se10,
                src_file=src_name,
                src_line=line_no,
                detail=f"run {run_key!r} is {type(run).__name__}, expected an object",
            )
            continue

        run_pk = _store_run(conn, rec, src_name, line_no, se10, str(run_key), run, stats)
        calls = run.get("web_search_calls")
        if not isinstance(calls, list):
            if calls is not None:
                stats["wl_calls_not_a_list"] += 1
                rec.record(
                    "WEBLOG_SHAPE_UNEXPECTED",
                    se10=se10,
                    src_file=src_name,
                    src_line=line_no,
                    detail=f"run {run_key!r}: web_search_calls is {type(calls).__name__}",
                )
            continue

        for index, call in enumerate(calls):
            _store_call(
                conn,
                rec,
                src_name,
                line_no,
                se10,
                run_pk,
                _run_id(str(run_key)),
                index,
                call,
                stats,
            )


def _store_run(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    line_no: int,
    se10: str,
    run_key: str,
    run: dict,
    stats: Counter,
) -> int | None:
    """Insert the run and its token usage. Returns the runs.id for the calls."""
    run_id = _run_id(run_key)
    if run_id is None:
        # Every per-run figure keys off this. Silently NULL would disable them
        # all with no trace, which is this codebase's signature failure mode.
        stats["wl_run_key_unparsed"] += 1
        rec.record(
            "RUN_KEY_UNPARSED",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            raw_excerpt=run_key,
            detail=f"run key {run_key!r} does not match run_<n>; run_id stored as NULL",
        )

    usage = run.get("usage_metadata")
    if not isinstance(usage, dict):
        usage = {}
        stats["wl_no_usage_metadata"] += 1
        rec.record(
            "MISSING_USAGE_METADATA",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            detail=f"run {run_key!r} has no usage_metadata; its tokens cannot be costed",
        )

    values = {k: usage.get(k) for k in USAGE_KEYS}
    # The subset relationship is what the cost formula rests on. If it ever
    # stops holding, every token figure is suspect, so check rather than assume.
    i_tok, o_tok = values["input_tokens"], values["output_tokens"]
    t_tok = values["total_tokens"]
    if all(isinstance(v, int) for v in (i_tok, o_tok, t_tok)) and i_tok + o_tok != t_tok:
        stats["wl_token_sum_mismatch"] += 1
        rec.record(
            "TOKEN_SUM_MISMATCH",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            detail=(
                f"run {run_key!r}: input {i_tok} + output {o_tok} != total {t_tok}. "
                "The cost model assumes cache_read and reasoning are SUBSETS of input "
                "and output; if they are addends here, every cost figure is wrong."
            ),
        )

    cur = conn.execute(
        "INSERT OR IGNORE INTO runs (se10, run_id, run_key, "
        + ", ".join(USAGE_KEYS)
        + ", src_file, src_line) VALUES (?, ?, ?, "
        + ", ".join("?" * len(USAGE_KEYS))
        + ", ?, ?)",
        (se10, run_id, run_key, *(values[k] for k in USAGE_KEYS), src_name, line_no),
    )
    stats["wl_runs"] += 1
    # `rowcount`, not `lastrowid`: an ignored INSERT leaves lastrowid pointing at
    # whatever row this connection inserted last, so testing it would silently
    # attach a duplicate run's calls to an unrelated merchant's run.
    if cur.rowcount:
        return cur.lastrowid
    # UNIQUE(se10, run_key) collided: the same run appears twice in the corpus.
    stats["wl_dup_run"] += 1
    rec.record(
        "DUP_RUN",
        se10=se10,
        src_file=src_name,
        src_line=line_no,
        detail=f"run {run_key!r} for se10 {se10} was already ingested; calls attach to the first",
    )
    row = conn.execute(
        "SELECT id FROM runs WHERE se10 = ? AND run_key = ?", (se10, run_key)
    ).fetchone()
    return row["id"] if row else None


def _store_call(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    line_no: int,
    se10: str,
    run_pk: int | None,
    run_id: int | None,
    index: int,
    call: object,
    stats: Counter,
) -> None:
    """Insert one call plus its query_instances rows."""
    parsed = parse_call(call)
    stats["wl_calls"] += 1
    stats[f"wl_action_{parsed.action_type}"] += 1
    stats[f"wl_conf_{parsed.parse_conf}"] += 1

    raw_json = json.dumps(call, sort_keys=True)
    for code, detail in parsed.notes:
        rec.record(
            code,
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            raw_excerpt=raw_json[:BAD_JSON_EXCERPT],
            detail=f"run_id={run_id} call {index}: {detail}",
        )

    cur = conn.execute(
        "INSERT INTO search_calls (se10, run_pk, run_id, call_index, call_id, action_type, "
        "status, query_raw, queries_json, url, details, raw_json, parse_conf, "
        "src_file, src_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            se10,
            run_pk,
            run_id,
            index,
            parsed.call_id,
            parsed.action_type,
            parsed.status,
            parsed.query_raw,
            json.dumps(parsed.queries) if parsed.queries is not None else None,
            parsed.url,
            parsed.details,
            raw_json,
            parsed.parse_conf,
            src_name,
            line_no,
        ),
    )
    _insert_queries(conn, cur.lastrowid, se10, parsed)


def _insert_queries(conn: sqlite3.Connection, call_id: int, se10: str, parsed: ParsedCall) -> None:
    """One query_instances row per query on this call.

    The singular `query` is flagged `is_billed_query`: billing is per call, so
    cost attribution must use exactly one query per call or archetype shares
    stop summing to 100%. The plural entries are stored for redundancy analysis
    only -- they are sub-queries within a single billed call, not extra calls.
    """
    if parsed.query_raw is None and not parsed.queries:
        return
    rows = [(call_id, se10, parsed.query_raw, 1)] if parsed.query_raw is not None else []
    rows += [
        (call_id, se10, q, 0)
        for q in (parsed.queries or [])
        if q != parsed.query_raw  # already stored as the billed query
    ]
    conn.executemany(
        "INSERT INTO query_instances (search_call_id, se10, query_text, is_billed_query) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def reparse(conn: sqlite3.Connection, rec: AnomalyRecorder, cfg: Config) -> Counter:
    """Re-run call extraction over stored `raw_json`, without reading any source.

    The operator loop depends on this: a parser fix costs seconds instead of a
    full re-ingest, which is what makes iterating against real data affordable
    across the air gap.
    """
    stats: Counter = Counter()
    rows = conn.execute(
        "SELECT id, se10, raw_json, src_file, src_line FROM search_calls ORDER BY id"
    ).fetchall()

    for row in rows:
        try:
            call = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            stats["reparse_bad_json"] += 1
            continue
        parsed = parse_call(call)
        stats["reparsed"] += 1
        stats[f"conf_{parsed.parse_conf}"] += 1
        for code, detail in parsed.notes:
            rec.record(
                code,
                se10=row["se10"],
                src_file=row["src_file"],
                src_line=row["src_line"],
                raw_excerpt=row["raw_json"][:BAD_JSON_EXCERPT],
                detail=detail,
            )
        conn.execute(
            "UPDATE search_calls SET action_type = ?, status = ?, query_raw = ?, "
            "queries_json = ?, url = ?, details = ?, parse_conf = ? WHERE id = ?",
            (
                parsed.action_type,
                parsed.status,
                parsed.query_raw,
                json.dumps(parsed.queries) if parsed.queries is not None else None,
                parsed.url,
                parsed.details,
                parsed.parse_conf,
                row["id"],
            ),
        )
        # query_instances are derived, so they are rebuilt rather than patched.
        conn.execute("DELETE FROM query_instances WHERE search_call_id = ?", (row["id"],))
        _insert_queries(conn, row["id"], row["se10"], parsed)

    return stats
