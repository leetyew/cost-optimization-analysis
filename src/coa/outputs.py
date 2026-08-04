"""Output parser: `output/*.jsonl` -> output_records / questions / answers / citations / votes.

PLAN.md §4. One JSON record per line, per merchant, holding several runs of the
fixed 48-question prompt plus the other team's own post-processing of it.

Three things here are cross-checks rather than extraction, and each is a report
finding in its own right — they measure how lossy the upstream post-processing is:

* prose citations vs `citation_evidence` (`CITATION_SOURCE_MISMATCH`)
* our text parse vs their `answer_dict` (`agree_with_dict`)
* `voted_majority` vs `voted_final` (`votes.differs`)

**The answer format is two regimes, not one** (operator-confirmed — see CLAUDE.md
"Known real-data format facts"). Scale questions answer 1-5, defaulting to 3 when
evidence is insufficient, and carry Evidence only when the answer is <= 3. Text
questions return a value or a literal NULL. So:

* `is_null` keys on the **answer**. Evidence being NULL is a specified outcome for
  a scale answer above 3, never a missing answer, and reading it as one would
  invert the headline metric.
* The Evidence line is **optional** in the block regex. Whether the real logs write
  `Evidence. NULL`, a bare label, or omit the line entirely is not yet confirmed,
  and a regex that requires it would silently lose every block that omits it.
  All three shapes are counted so the ingest summary answers the question.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

from .anomalies import AnomalyRecorder, note_encoding_damage
from .config import Config

BAD_JSON_EXCERPT = 500

# §4 as written. Questions are multi-line (they carry their answer-format
# instruction), so the `\nQ<n>.` lookahead — not a line split — is what delimits.
QUESTION_RE = re.compile(r"Q(\d+)\.\s*(.*?)(?=\nQ\d+\.|\Z)", re.S)

# §4's block regex with the Evidence group made OPTIONAL. The `(?:...)?` is greedy,
# so a present Evidence line still binds to group 3; only when the line is absent
# does the answer group absorb to the block boundary. Without this every answer
# above the evidence threshold fails to match and disappears with no anomaly.
ANSWER_BLOCK_RE = re.compile(
    r"Q(\d+)\..*?\nA\1\.[ \t]*(.*?)"
    r"(?:\n+Evidence[.:]?[ \t]*(.*?))?"
    r"(?=\n\s*\nQ\d+\.|\Z)",
    re.S,
)

# A SECOND prose shape, operator-confirmed 2026-08-04. Some runs omit the `Q<n>.`
# echo entirely and separate answers with a single newline rather than a blank line:
#
#     A1. 5
#     A2. NULL
#     A3. 3
#
# `ANSWER_BLOCK_RE` requires the question echo, so it matched ZERO blocks on these
# and every answer fell back to `answer_dict` -- which is what the 1,047
# "prose-unreadable" runs actually are. They are a format difference, not a parse
# failure, and calling them unreadable charged their pipeline for our assumption.
#
# Evidence stays optional here for the same reason it is optional above: whether
# this shape ever carries an Evidence line is exactly what the ingest counters now
# measure rather than assume.
BARE_ANSWER_RE = re.compile(
    r"^A(\d+)\.[ \t]*(.*?)"
    r"(?:\n+Evidence[.:]?[ \t]*(.*?))?"
    r"(?=\n\s*A\d+\.|\Z)",
    re.S | re.M,
)

# Markdown link wrapped in its own parens, including the observed empty `([]())`.
PROSE_CITATION_RE = re.compile(r"\(\[([^\]]*)\]\(([^)]*)\)\)")

RUN_KEY_RE = re.compile(r"^run[_\-]?(\d+)$", re.I)
A_KEY_RE = re.compile(r"^A(\d+)$", re.I)

# Keys output records duplicate from input/. input/ wins; divergence is reported.
CONVENIENCE_KEYS: dict[str, str] = {
    "website": "website",
    "industry": "industry_tagged",
    "state": "state",
}

# Anomaly details are pasted into a chat session, so lists inside them are capped.
MAX_LISTED = 12

# Top-level record keys, operator-confirmed 2026-08-03/04. PLAN.md §4 got three of
# the four wrong, and each miss was silent: `answers` plural emptied the answer map,
# `question` singular emptied the canonical question set, and nesting
# `citation_evidence` inside `answer_dict` yielded zero dict citations corpus-wide.
QUESTION_KEY = "questions"
ANSWER_KEY = "answer"
CITATION_EVIDENCE_KEY = "citation_evidence"

# The vote maps live INSIDE `answer_dict`, alongside its `run_<n>` keys — not at the
# record top level as PLAN.md §4 assumed. Reading the top level gave `votes 0` on the
# real corpus, which read as "no disagreements" rather than "never loaded".
VOTE_KEYS: tuple[str, str] = ("voted_majority", "voted_final")


@dataclass(frozen=True)
class AnswerBlock:
    """One parsed Q/A/Evidence block.

    `evidence is None` means the line was absent entirely; `""` means the label was
    present but bare. The distinction is kept because which shape real data uses is
    still unconfirmed, and collapsing them would throw away the evidence for it.
    """

    qnum: int
    answer: str
    evidence: str | None
    evidence_start: int | None
    # Which prose shape produced this block. Carried per block rather than per run
    # so `parsed_from` can distinguish them downstream: a bare block's missing
    # Evidence line may be structural to the format rather than a finding about
    # the search, and scoring the two identically would inflate the headline.
    shape: str = "q_echo"


@dataclass(frozen=True)
class _Ctx:
    """Per-record handles, so the section helpers do not take eight arguments each."""

    conn: sqlite3.Connection
    rec: AnomalyRecorder
    src_file: str
    src_line: int
    se10: str
    output_id: int
    stats: Counter


def _find_prompts(record: dict) -> tuple[str | None, str]:
    """`(system_prompt, user_prompt)` from `questions`, shaped `[[system, user]]`.

    Every index is guarded. The value is nested two deep, so a dict, a bare string
    or a short inner list would each raise a different exception and kill the whole
    file — invariant 1 says malformed input degrades to an anomaly, never a crash.
    An unusable shape returns empty and the caller reports what it actually saw.
    """
    value = record.get(QUESTION_KEY)
    prompts = value[0] if isinstance(value, list) and value else None
    if not isinstance(prompts, (list, tuple)):
        return None, ""
    system = prompts[0] if len(prompts) > 0 else None
    user = prompts[1] if len(prompts) > 1 else None
    return (
        (system if isinstance(system, str) else None),
        user if isinstance(user, str) else "",
    )


def _shape(value: object) -> str:
    """Compact description of a value, for anomaly details across the air gap.

    The operator sees only what an anomaly prints, so a detail saying a field was
    "missing" is far less useful than one saying it was a 3-element list. Dict keys
    are included because a wrong key name is the failure this keeps catching.
    """
    if value is None:
        return "absent"
    if isinstance(value, dict):
        return f"dict{{{_capped(sorted(map(str, value)))}}}"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}[{len(value)}]"
    if isinstance(value, str):
        return f"str[{len(value)}]"
    return type(value).__name__


def extract_questions(user_prompt: str) -> dict[int, str]:
    """qnum -> question text, per §4."""
    return {int(n): text.strip() for n, text in QUESTION_RE.findall(user_prompt or "")}


def parse_answer_blocks(text: str) -> list[AnswerBlock]:
    """Parse one run's answer prose, tolerating both known shapes.

    The `Q<n>.`-echo shape is tried first and the bare `A<n>.` shape only if it
    yields nothing, so a run carrying question echoes can never be re-read by the
    looser pattern -- which would match the same answers with worse boundaries.

    Falling back rather than alternating is deliberate. The bare pattern is
    strictly more permissive, so running it everywhere would silently change how
    already-working runs parse; running it only where the strict one found nothing
    keeps every currently-parsed run byte-identical.
    """
    blocks = _blocks_from(ANSWER_BLOCK_RE, text, "q_echo")
    return blocks or _blocks_from(BARE_ANSWER_RE, text, "bare")


def _blocks_from(pattern: re.Pattern, text: str, shape: str) -> list[AnswerBlock]:
    """Every block one pattern finds. Group 3 is the optional Evidence text."""
    blocks = []
    for m in pattern.finditer(text or ""):
        evidence = m.group(3)
        blocks.append(
            AnswerBlock(
                qnum=int(m.group(1)),
                answer=(m.group(2) or "").strip(),
                evidence=evidence,
                evidence_start=None if evidence is None else m.start(3),
                shape=shape,
            )
        )
    return blocks


def evidence_shape(evidence: str | None) -> str:
    """Which of the four Evidence renderings a block used."""
    if evidence is None:
        return "evidence_absent"
    if not evidence.strip():
        return "evidence_empty"
    if evidence.strip().casefold() == "null":
        return "evidence_null"
    return "evidence_present"


def is_null_answer(answer: str) -> bool:
    """Whether an answer carries no information.

    Casefolded rather than an exact `NULL` match: a case variant means the same
    thing, and treating it as a real answer would understate the NULL rate — which
    is one of the numbers the whole cost argument rests on.
    """
    return answer.strip().casefold() == "null"


def _norm_text(value: object) -> str:
    """Comparison form for answers and convenience fields."""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _domain(url: str | None) -> str | None:
    """Hostname of a citation URL, lowercased. Never raises on junk."""
    if not url:
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _run_id(run_key: str) -> int | None:
    """Integer run index from a `run_0`-style key. Exact, unlike burst attribution."""
    m = RUN_KEY_RE.match(run_key.strip())
    return int(m.group(1)) if m else None


def _qnum(a_key: str) -> int | None:
    m = A_KEY_RE.match(str(a_key).strip())
    return int(m.group(1)) if m else None


def _capped(values: Sequence[object]) -> str:
    """Render a list for an anomaly detail without flooding the paste buffer."""
    head = ", ".join(str(v) for v in list(values)[:MAX_LISTED])
    extra = len(values) - MAX_LISTED
    return head + (f", ... (+{extra} more)" if extra > 0 else "")


def _run_answer_map(answer_dict: dict, run_key: str) -> dict[int, str]:
    """Their parsed answers for one run, keyed by qnum.

    `answer_dict` mixes per-run keys with the `voted_majority` / `voted_final`
    siblings, so the caller cannot simply iterate it.
    """
    raw = answer_dict.get(run_key)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        qnum = _qnum(key)
        if qnum is not None:
            out[qnum] = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return out


def _run_ce_map(evidence: dict, run_key: str) -> dict[int, tuple[str | None, str | None]]:
    """Their `(answer, evidence)` for one run, keyed by qnum.

    `citation_evidence` is not a citation list — each entry is a complete
    per-answer record (`question`, `a_key`, `answer`, `citation`, `evidence`,
    `full_answer_block`), so it carries the two fields our prose regex extracts,
    already parsed by the pipeline that produced the prose.

    That makes it the repair path for the runs whose prose we cannot parse at
    all: on the real corpus 1,047 whole runs yielded no blocks, and every one of
    their answers had a NULL `evidence_text` purely because our regex missed,
    not because the pipeline found nothing.

    Entries without a usable `a_key` are skipped here and counted by the citation
    pass, which sees the same list — this is a lookup, not a validation step.
    """
    entries = evidence.get(run_key)
    if not isinstance(entries, list):
        return {}
    out: dict[int, tuple[str | None, str | None]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        qnum = _qnum(entry.get("a_key") or "")
        if qnum is None:
            continue
        out[qnum] = (_ce_text(entry.get("answer")), _ce_text(entry.get("evidence")))
    return out


def _ce_text(value: object) -> str | None:
    """One citation_evidence text field, or None when it carries nothing.

    A non-string is JSON-encoded rather than dropped: the shape is confirmed for
    `citation` but never stated for `answer` / `evidence`, and stringifying keeps
    the value visible for `coa doctor` instead of losing it to a type check.
    """
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def normalize_vote(value: object) -> tuple[str | None, bool]:
    """`(stored_text, is_ambiguous)` for one vote value.

    A list of identical values collapses to the scalar — the observed
    `["NULL", "NULL"]` carries no more information than `"NULL"`. A genuinely
    mixed list means the upstream vote did not resolve, so it stays JSON and is
    flagged rather than having a winner invented for it.
    """
    if value is None:
        return None, False
    if isinstance(value, list):
        if not value:
            return None, False
        distinct = {json.dumps(v, sort_keys=True) for v in value}
        if len(distinct) == 1:
            first = value[0]
            return (first if isinstance(first, str) else json.dumps(first, sort_keys=True)), False
        return json.dumps(value, sort_keys=True), True
    if isinstance(value, (str, int, float, bool)):
        return str(value), False
    return json.dumps(value, sort_keys=True), True


def ingest_output(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    lines: Iterable[str],
    cfg: Config,
) -> Counter:
    """Parse one `output/*.jsonl` file. One record per line, one merchant per record."""
    stats: Counter = Counter()

    for i, raw in enumerate(lines):
        line_no = i + 1
        stats["lines"] += 1
        if not raw.strip():
            continue
        if note_encoding_damage(
            rec, src_name, line_no, raw, detail="undecodable byte in output record"
        ):
            stats["encoding_damaged"] += 1

        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            # The anomaly row *is* the retention here: without an se10 there is no
            # output_records row to hang the raw text on.
            stats["out_bad_json"] += 1
            rec.record(
                "BAD_JSON_LINE",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail=f"output record did not parse as JSON: {exc}",
            )
            continue

        se10 = record.get("se10")
        if se10 in (None, ""):
            stats["out_no_se10"] += 1
            rec.record(
                "OUTPUT_NO_SE10",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail="record has no se10; its answers cannot be attributed",
            )
            continue

        _ingest_record(conn, rec, src_name, line_no, raw, record, str(se10), stats)

    return stats


def _ingest_record(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    line_no: int,
    raw: str,
    record: dict,
    se10: str,
    stats: Counter,
) -> None:
    """Store one output record and everything hanging off it."""
    dup_flag = _flag_duplicate(conn, rec, src_name, line_no, se10, raw, stats)

    system_prompt, user_prompt = _find_prompts(record)
    questions = extract_questions(user_prompt)
    if not questions:
        # Previously a bare counter that nothing printed, which is how an empty
        # `questions` table survived a whole real ingest unnoticed. The canonical
        # question set is the denominator of the primary deliverable, so its absence
        # has to shout — and name what was actually there, because the likely cause
        # is another key-name drift like the one that emptied it in the first place.
        stats["out_no_questions"] += 1
        rec.record(
            "OUTPUT_NO_QUESTIONS",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            detail=(
                "no `Q<n>.` question text found, so this record seeds no canonical "
                f"question set; `{QUESTION_KEY}` is {_shape(record.get(QUESTION_KEY))}, "
                f"expected [[system, user]]; top-level keys present: "
                f"{_capped(sorted(record))}"
            ),
        )
    _sync_questions(conn, rec, src_name, line_no, se10, questions, stats)

    n_runs = record.get("n_runs")
    cur = conn.execute(
        "INSERT INTO output_records (se10, n_runs, question_system_prompt, "
        "question_user_prompt, raw_json_hash, dup_flag, src_file, src_line) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            se10,
            n_runs if isinstance(n_runs, int) else None,
            system_prompt if isinstance(system_prompt, str) else None,
            user_prompt or None,
            _hash(raw),
            dup_flag,
            src_name,
            line_no,
        ),
    )
    ctx = _Ctx(conn, rec, src_name, line_no, se10, cur.lastrowid, stats)
    stats["out_records"] += 1

    answers = record.get(ANSWER_KEY)
    answers = answers if isinstance(answers, dict) else {}
    answer_dict = record.get("answer_dict")
    answer_dict = answer_dict if isinstance(answer_dict, dict) else {}
    if isinstance(n_runs, int) and n_runs != len(answers):
        stats["out_n_runs_mismatch"] += 1

    # A record that yields no runs contributes no answers, no prose citations and
    # nothing to the per-question rates that are the primary deliverable. Silently
    # storing the shell and moving on is exactly the failure this codebase keeps
    # hitting, so it speaks — and names the keys actually present, because the
    # likely cause is the map having moved to another key again.
    if not answers:
        stats["out_no_answers"] += 1
        rec.record(
            "OUTPUT_NO_ANSWERS",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            # No raw_excerpt: `detail` already carries the key list, and this code
            # can fire once per record. Printing it twice doubles the paste-back
            # payload for nothing, and that channel is the whole operator loop.
            detail=(
                f"`{ANSWER_KEY}` is {_shape(record.get(ANSWER_KEY))}, expected a map "
                f"keyed by run; top-level keys present: {_capped(sorted(record))}"
            ),
        )

    canonical = _canonical_qnums(conn) or set(questions)

    # Located BEFORE the answer pass, not after: each citation_evidence entry is a
    # complete per-answer record carrying `answer` and `evidence`, so it is an
    # answer source, not just a citation source. Reading it afterwards would mean
    # a second pass to graft those fields onto rows already written.
    evidence = record.get(CITATION_EVIDENCE_KEY)
    if not isinstance(evidence, dict) or not evidence:
        # Reading the wrong location produced zero dict citations across the entire
        # real corpus with no anomaly and not even a counter, so
        # CITATION_SOURCE_MISMATCH read as "the sources agree" when in truth one of
        # them was never loaded. An absent field is now stated, not inferred.
        stats["out_no_citation_evidence"] += 1
        rec.record(
            "OUTPUT_NO_CITATION_EVIDENCE",
            se10=se10,
            src_file=src_name,
            src_line=line_no,
            detail=(
                f"`{CITATION_EVIDENCE_KEY}` is {_shape(evidence)}, expected a map "
                "keyed by run; the prose-vs-dict cross-check cannot run for this "
                f"record, and its answers lose their only non-prose evidence. "
                f"Top-level keys present: {_capped(sorted(record))}"
            ),
        )
        evidence = {}

    # Each run's prose URLs are kept from the answer pass and handed to the
    # cross-check, rather than re-parsing the run text there: the block regex over
    # 48 blocks is the heaviest thing this parser does, and running it twice per
    # run doubles the cost of the whole ingest for nothing.
    #
    # The union of run keys, not just the answer map's: a run present only in
    # citation_evidence still has 48 parsed answers, and iterating one map would
    # drop them with nothing but a run-count discrepancy to show for it.
    prose_urls: dict[str, dict[int, set[str]]] = {}
    for run_key in sorted(set(answers) | set(evidence)):
        text = answers.get(run_key)
        prose_urls[run_key] = _store_run(
            ctx, run_key, text if isinstance(text, str) else "", answer_dict, evidence, canonical
        )

    _store_dict_citations(ctx, evidence, prose_urls)
    _store_votes(ctx, answer_dict)
    _check_convenience_keys(ctx, record)


def _flag_duplicate(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    line_no: int,
    se10: str,
    raw: str,
    stats: Counter,
) -> int:
    """Flag both copies of a repeated se10. Both are ingested; §4 forbids dropping."""
    prior = conn.execute(
        "SELECT id, n_runs, src_file, src_line, raw_json_hash FROM output_records "
        "WHERE se10 = ? ORDER BY id",
        (se10,),
    ).fetchall()
    if not prior:
        return 0

    stats["out_dup_se10"] += 1
    conn.execute("UPDATE output_records SET dup_flag = 1 WHERE se10 = ?", (se10,))
    same = any(p["raw_json_hash"] == _hash(raw) for p in prior)
    where = "; ".join(f"{p['src_file']}:{p['src_line']} (n_runs={p['n_runs']})" for p in prior)
    rec.record(
        "DUP_OUTPUT_SE10",
        se10=se10,
        src_file=src_name,
        src_line=line_no,
        detail=(
            f"se10 {se10} already seen at {where}. Both copies are ingested and "
            f"dup_flag is set on each; analysis uses the record with the most runs. "
            f"Payloads are {'identical' if same else 'DIFFERENT'}."
        ),
    )
    return 1


def _canonical_qnums(conn: sqlite3.Connection) -> set[int]:
    return {r["qnum"] for r in conn.execute("SELECT qnum FROM questions")}


def _sync_questions(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    line_no: int,
    se10: str,
    questions: dict[int, str],
    stats: Counter,
) -> None:
    """Seed the canonical question set from the first record, then compare.

    Drift is judged on the **qnum set**, not on the text. The 48 questions carry
    merchant values inline (`...building at <zip>...`), so the text differs for
    essentially every merchant — 19,349 of 19,350 records on the real corpus. That
    is the expected shape of the data, not a defect, and firing an anomaly per
    record buried every other code in the summary. What actually threatens the
    per-question scorecard is a question appearing or disappearing, because that
    is what makes qnum N mean two different things.

    Text variation is still counted, since a jump in it would be worth knowing.
    Telling merchant-value substitution from a genuine rewording needs the text
    templated against `pii_terms`, which is P3's job and is not yet reliable.

    Cross-file state lives in the DB rather than in the parser so that resumable,
    per-file ingest stays correct — the parser itself never sees more than one file.
    """
    if not questions:
        return  # already counted and flagged by the caller, which can see the record

    rows = conn.execute("SELECT qnum, text FROM questions").fetchall()
    if not rows:
        conn.executemany(
            "INSERT INTO questions (qnum, text) VALUES (?, ?)", sorted(questions.items())
        )
        stats["out_questions_seeded"] += len(questions)
        return

    canonical = {r["qnum"]: r["text"] for r in rows}
    if any(canonical.get(q) != questions.get(q) for q in set(canonical) | set(questions)):
        stats["out_question_text_varies"] += 1

    missing = sorted(set(canonical) - set(questions))
    added = sorted(set(questions) - set(canonical))
    if not missing and not added:
        return

    stats["out_question_drift"] += 1
    rec.record(
        "QUESTION_SET_DRIFT",
        se10=se10,
        src_file=src_name,
        src_line=line_no,
        # Numbers only, no text: the question text carries merchant PII inline, and
        # this detail is meant to be pasted into a chat session.
        detail=(
            f"question NUMBERS differ from the canonical set of {len(canonical)}: "
            f"missing Q{_capped(missing) or '-'}, extra Q{_capped(added) or '-'}. "
            "Per-question rates key on qnum, so a changed set means qnum N no "
            "longer denotes one question across the corpus."
        ),
    )


def _store_run(
    ctx: _Ctx,
    run_key: str,
    text: str,
    answer_dict: dict,
    evidence: dict,
    canonical: set[int],
) -> dict[int, set[str]]:
    """Answers and prose citations for one run.

    Three parses of the same prose land on each answer row — ours, plus the two
    the other team's pipeline produced (`citation_evidence`, `answer_dict`). None
    of them wins here. Which source is authoritative is a question the corpus
    answers, and `coa scorecard --answer-source` switches between them at query
    time precisely so that changing our mind costs a SQL clause rather than a
    re-ingest of a 19k-merchant corpus across the air gap.

    Returns that run's non-empty prose URLs per qnum, which the citation
    cross-check consumes instead of re-parsing the same text.
    """
    run_id = _run_id(run_key)
    if run_id is None:
        # Silently leaving run_id NULL would disable every per-run metric with no
        # trace. This is the failure mode the codebase keeps hitting, so it speaks.
        ctx.stats["out_run_key_unparsed"] += 1
        ctx.rec.record(
            "RUN_KEY_UNPARSED",
            se10=ctx.se10,
            src_file=ctx.src_file,
            src_line=ctx.src_line,
            raw_excerpt=run_key,
            detail=(
                f"run key {run_key!r} does not match run_<n>; run_id stored as NULL, "
                "so this run drops out of every per-run figure until the pattern is fixed"
            ),
        )
    ctx.stats["out_runs"] += 1

    blocks = parse_answer_blocks(text)
    theirs = _run_answer_map(answer_dict, run_key)
    ce = _run_ce_map(evidence, run_key)
    rows, disagreed = [], []

    def _row(
        qnum: int | None,
        answer: str,
        ev: str | None,
        source: str,
        agree_dict: int | None = None,
    ) -> tuple:
        """One answer row, carrying whichever of the three parses exist for it."""
        ce_answer, ce_evidence = ce.get(qnum, (None, None)) if qnum is not None else (None, None)
        agree_ce = None if ce_answer is None else int(_norm_text(answer) == _norm_text(ce_answer))
        return (
            ctx.se10,
            ctx.output_id,
            run_id,
            qnum,
            answer,
            ev,
            ce_answer,
            ce_evidence,
            int(is_null_answer(answer)),
            source,
            agree_dict,
            agree_ce,
        )

    for block in blocks:
        ctx.stats[evidence_shape(block.evidence)] += 1
        ctx.stats["out_answer_blocks"] += 1
        # Counted per shape as well as overall. Whether the bare shape ever carries
        # an Evidence line decides whether its answers may enter the default-3
        # denominator, and that is a question for the corpus, not an assumption.
        ctx.stats[f"out_block_{block.shape}"] += 1
        if block.shape != "q_echo" and evidence_shape(block.evidence) == "evidence_present":
            ctx.stats["out_bare_with_evidence"] += 1
        mine = block.answer
        agree: int | None = None
        if block.qnum in theirs:
            agree = int(_norm_text(mine) == _norm_text(theirs[block.qnum]))
            if not agree:
                disagreed.append(block.qnum)
        rows.append(
            _row(
                block.qnum,
                mine,
                None if block.evidence is None else block.evidence.strip(),
                # A distinct provenance, NOT "answers_text". `_EVIDENCE_OBSERVABLE`
                # in the scorecard keys off that value, so folding the bare shape
                # into it would silently admit ~1,047 runs to the default-3
                # denominator on the assumption that their missing Evidence line is
                # a finding rather than a property of the format. It stays out
                # until the counters above show whether the shape carries evidence.
                "answers_text" if block.shape == "q_echo" else "answers_text_bare",
                agree,
            )
        )

    # Anything their dict has that our prose parse missed still gets a row. This is
    # the backstop that keeps a wrong assumption about the block format from
    # silently losing answers — they land, flagged with where they came from.
    seen = {b.qnum for b in blocks}
    for qnum in sorted(set(theirs) - seen):
        ctx.stats["out_answers_from_dict"] += 1
        rows.append(_row(qnum, theirs[qnum], None, "answer_dict"))

    # Third backstop, and the only one that can supply evidence. A qnum reaching
    # here was missed by BOTH our regex and answer_dict, so without it the answer
    # would vanish from the per-question rates entirely — the failure invariant 1
    # exists to prevent.
    for qnum in sorted(set(ce) - seen - set(theirs)):
        ce_answer, _ = ce[qnum]
        if ce_answer is None:
            continue
        ctx.stats["out_answers_from_ce"] += 1
        rows.append(_row(qnum, ce_answer, None, "citation_evidence"))

    ctx.conn.executemany(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text, evidence_text, "
        "ce_answer, ce_evidence, is_null, parsed_from, agree_with_dict, agree_with_ce) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    missing = sorted(canonical - seen)
    if canonical and missing:
        ctx.stats["out_short_runs"] += 1
        ctx.rec.record(
            "ANSWER_BLOCK_COUNT",
            se10=ctx.se10,
            src_file=ctx.src_file,
            src_line=ctx.src_line,
            detail=(
                f"run {run_key}: parsed {len(seen)} of {len(canonical)} expected answer "
                f"blocks; missing Q{_capped(missing)}"
            ),
        )

    if disagreed:
        # One anomaly per run, never per answer: if their normalization differs from
        # ours systematically this fires on most answers, and a per-answer row would
        # bury every other code in the summary.
        ctx.stats["out_answer_disagreements"] += len(disagreed)
        ctx.rec.record(
            "ANSWER_PARSE_MISMATCH",
            se10=ctx.se10,
            src_file=ctx.src_file,
            src_line=ctx.src_line,
            detail=(
                f"run {run_key}: {len(disagreed)} of {len(theirs)} comparable answers "
                f"differ from answer_dict; Q{_capped(disagreed)}"
            ),
        )

    return _store_prose_citations(ctx, run_id, text, blocks)


def _store_prose_citations(
    ctx: _Ctx, run_id: int | None, text: str, blocks: Sequence[AnswerBlock]
) -> dict[int, set[str]]:
    """Markdown links inside each block's evidence, positioned within the run text.

    Returns the non-empty URLs per qnum for the cross-check. Empty `([]())`
    placeholders are stored but deliberately left out of that set — see
    `_cross_check_citations`.
    """
    rows = []
    by_qnum: dict[int, set[str]] = {}
    matched = 0
    for block in blocks:
        if block.evidence is None or block.evidence_start is None:
            continue
        for m in PROSE_CITATION_RE.finditer(block.evidence):
            matched += 1
            title, url = m.group(1), m.group(2)
            empty = not title and not url
            ctx.stats["out_empty_placeholders" if empty else "out_prose_citations"] += 1
            rows.append(
                (
                    ctx.se10,
                    ctx.output_id,
                    run_id,
                    block.qnum,
                    url or None,
                    _domain(url),
                    title or None,
                    int(empty),
                    "markdown_prose",
                    block.evidence_start + m.start(),
                )
            )
            if url:
                by_qnum.setdefault(block.qnum, set()).add(url)
    ctx.conn.executemany(
        "INSERT INTO citations (se10, output_id, run_id, qnum, url, domain, title, "
        "empty_placeholder, source, char_pos) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    # A citation outside every parsed block would otherwise vanish without trace.
    stray = len(PROSE_CITATION_RE.findall(text)) - matched
    if stray > 0:
        ctx.stats["out_citations_outside_blocks"] += stray
    return by_qnum


def _store_dict_citations(
    ctx: _Ctx, evidence: dict, prose_urls: dict[str, dict[int, set[str]]]
) -> None:
    """`citation_evidence` entries, then the prose-vs-dict cross-check.

    Takes the already-located map rather than a container to search: the caller owns
    the lookup and the anomaly for a missing one, so this stays a pure store step.
    """
    for run_key, entries in evidence.items():
        if not isinstance(entries, list):
            # A dict keyed by a_key is the shape this would most plausibly be
            # instead, and skipping it silently is how a whole run's citations
            # would vanish while the summary still looked healthy.
            ctx.stats["out_citation_entries_not_a_list"] += 1
            ctx.rec.record(
                "CITATION_ENTRIES_NOT_A_LIST",
                se10=ctx.se10,
                src_file=ctx.src_file,
                src_line=ctx.src_line,
                detail=(
                    f"run {run_key}: citation_evidence entries are "
                    f"{_shape(entries)}, expected a list; this run's dict citations "
                    "are not stored and its cross-check does not run"
                ),
            )
            continue
        run_id = _run_id(run_key)
        rows: list[tuple] = []
        by_qnum: dict[int, set[str]] = {}
        unknown_shape: set[int] = set()

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            qnum = _qnum(entry.get("a_key") or "")
            citation = entry.get("citation")
            title = entry.get("question")

            if citation is None or isinstance(citation, str):
                urls = [citation] if citation else []
            else:
                # §4: any shape other than null-or-string is unexpected. A list is
                # read as several URLs — the only sane reading — but the anomaly
                # fires either way, so nothing is claimed without the operator
                # seeing the raw value.
                if qnum is not None:
                    unknown_shape.add(qnum)
                ctx.stats["out_citation_shape_unexpected"] += 1
                ctx.rec.record(
                    "CITATION_SHAPE_UNEXPECTED",
                    se10=ctx.se10,
                    src_file=ctx.src_file,
                    src_line=ctx.src_line,
                    raw_excerpt=json.dumps(citation, sort_keys=True),
                    detail=(
                        f"run {run_key} {entry.get('a_key')}: `citation` is "
                        f"{type(citation).__name__}, expected string or null"
                    ),
                )
                urls = (
                    [u for u in citation if isinstance(u, str)]
                    if isinstance(citation, list)
                    else []
                )
                if not urls:
                    urls = [json.dumps(citation, sort_keys=True)]

            for url in urls or [None]:
                ctx.stats["out_dict_citations"] += 1
                rows.append(
                    (
                        ctx.se10,
                        ctx.output_id,
                        run_id,
                        qnum,
                        url,
                        _domain(url),
                        title,
                        int(not url),
                        "citation_evidence",
                        None,
                    )
                )
                if qnum is not None and url:
                    by_qnum.setdefault(qnum, set()).add(url)

        ctx.conn.executemany(
            "INSERT INTO citations (se10, output_id, run_id, qnum, url, domain, title, "
            "empty_placeholder, source, char_pos) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        _cross_check_citations(
            ctx, run_key, run_id, prose_urls.get(run_key, {}), by_qnum, unknown_shape
        )


def _cross_check_citations(
    ctx: _Ctx,
    run_key: str,
    run_id: int | None,
    prose: dict[int, set[str]],
    dict_urls: dict[int, set[str]],
    unknown_shape: set[int],
) -> None:
    """Compare prose and dict URL sets per qnum. Divergence measures upstream loss.

    Only **non-empty** URLs are compared. An empty `([]())` prose placeholder and a
    null dict citation are the same fact recorded twice; comparing them raw would
    fire on essentially every record and drown the real signal.

    qnums whose dict citation had an unrecognized shape are skipped: we do not know
    their true URL set, so a difference there is unknown rather than loss.
    """
    for qnum in sorted((set(prose) | set(dict_urls)) - unknown_shape):
        here, there = prose.get(qnum, set()), dict_urls.get(qnum, set())
        if here == there:
            continue
        ctx.stats["out_citation_mismatch"] += 1
        ctx.rec.record(
            "CITATION_SOURCE_MISMATCH",
            se10=ctx.se10,
            src_file=ctx.src_file,
            src_line=ctx.src_line,
            detail=(
                f"run {run_key} (run_id={run_id}) Q{qnum}: prose and citation_evidence "
                f"disagree.\n  prose only: {_capped(sorted(here - there)) or '-'}\n"
                f"  dict only : {_capped(sorted(there - here)) or '-'}"
            ),
        )


def _store_votes(ctx: _Ctx, answer_dict: dict) -> None:
    """`voted_majority` vs `voted_final` per qnum, with list values normalized.

    Both live inside `answer_dict` (see VOTE_KEYS), which is why this takes the dict
    rather than the record.
    """
    majority = answer_dict.get("voted_majority")
    majority = majority if isinstance(majority, dict) else {}
    final = answer_dict.get("voted_final")
    final = final if isinstance(final, dict) else {}
    if not final:
        # §4: an empty voted_final is treated as absent and counted, not flagged.
        ctx.stats["out_empty_voted_final"] += 1
    if not majority and not final:
        # Both absent means the votes table stays empty and the majority-vs-final
        # cross-check never runs. On the real corpus `votes` came back 0, which read
        # as "no disagreements" when it actually meant "no data" — the same
        # ambiguity that hid the citation_evidence lookup.
        ctx.stats["out_no_votes"] += 1
        ctx.rec.record(
            "OUTPUT_NO_VOTES",
            se10=ctx.se10,
            src_file=ctx.src_file,
            src_line=ctx.src_line,
            detail=(
                "neither vote map is usable, so this record contributes no votes; "
                + ", ".join(f"{k}={_shape(answer_dict.get(k))}" for k in VOTE_KEYS)
                + f"; answer_dict keys present: {_capped(sorted(answer_dict))}"
            ),
        )

    rows = []
    for key in sorted(set(majority) | set(final), key=lambda k: (_qnum(k) or 0, str(k))):
        qnum = _qnum(key)
        maj_text, maj_odd = normalize_vote(majority.get(key))
        fin_text, fin_odd = normalize_vote(final.get(key)) if final else (None, False)
        differs = int(fin_text is not None and maj_text != fin_text)
        rows.append((ctx.se10, ctx.output_id, qnum, maj_text, fin_text, differs))
        ctx.stats["out_votes"] += 1
        if differs:
            ctx.stats["out_votes_differ"] += 1
        if maj_odd or fin_odd:
            # Once per (record, qnum): the same unresolved value normally appears in
            # BOTH voted_majority and voted_final, and firing per field would double
            # -count a single upstream defect.
            ctx.stats["out_vote_value_list"] += 1
            ctx.rec.record(
                "VOTE_VALUE_LIST",
                se10=ctx.se10,
                src_file=ctx.src_file,
                src_line=ctx.src_line,
                raw_excerpt=json.dumps(
                    {"voted_majority": majority.get(key), "voted_final": final.get(key)},
                    sort_keys=True,
                ),
                detail=(
                    f"{key}: vote value is a list with differing members, so it has no "
                    f"single winner. Stored as JSON rather than picking one."
                ),
            )

    ctx.conn.executemany(
        "INSERT INTO votes (se10, output_id, qnum, voted_majority, voted_final, differs) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _check_convenience_keys(ctx: _Ctx, record: dict) -> None:
    """Compare the duplicated merchant fields against input/. Never writes them.

    input/ is ingested first and is authoritative, so this is a pure check — which
    is also why there is no precedence logic to get wrong.
    """
    row = ctx.conn.execute(
        "SELECT " + ", ".join(CONVENIENCE_KEYS.values()) + " FROM merchants WHERE se10 = ?",
        (ctx.se10,),
    ).fetchone()
    if row is None:
        ctx.stats["out_se10_without_input"] += 1
        return

    clashes = [
        f"{out_key}: input={row[column]!r} output={record[out_key]!r}"
        for out_key, column in CONVENIENCE_KEYS.items()
        if record.get(out_key) not in (None, "")
        and row[column] is not None
        and _norm_text(record[out_key]) != _norm_text(row[column])
    ]
    if not clashes:
        return
    ctx.stats["out_field_conflict"] += 1
    ctx.rec.record(
        "INPUT_OUTPUT_FIELD_CONFLICT",
        se10=ctx.se10,
        src_file=ctx.src_file,
        src_line=ctx.src_line,
        detail="input/ wins and is kept; output/ differs on " + "; ".join(clashes),
    )


def _hash(raw: str) -> str:
    """Short content hash, used to tell a repeated record from a genuinely new one."""
    return sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
