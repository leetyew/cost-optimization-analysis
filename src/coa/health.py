"""`coa doctor` — one paste-ready screen describing what actually landed.

This exists because of the air gap. Real data lives where Claude Code cannot
look, so every question about it costs a round-trip in which the operator hand-
types SQL and pastes results back. That friction is the binding constraint on
the whole project, so the diagnostics that keep getting asked for live here as
one short command instead.

The organising principle is **positive confirmation**. An absent anomaly is
ambiguous: it can mean the data was clean, or that the detector never ran
because the thing it inspects was missing. Row counts disambiguate — zero
`citation_evidence` citations with non-zero prose citations says the field is
absent, which no anomaly count could have told you.
"""

from __future__ import annotations

import json
import sqlite3

# Wide enough for a label, narrow enough that the block still pastes cleanly.
_LABEL = 22


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return conn.execute(sql).fetchall()


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return (row[0] if row and row[0] is not None else 0) if row else 0


def _line(label: str, value: object) -> str:
    return f"  {label:<{_LABEL}} {value}"


def _breakdown(conn: sqlite3.Connection, sql: str, label: str, empty: str = "none") -> str:
    parts = [f"{r[0]} {r[1]}" for r in _rows(conn, sql)]
    return _line(label, ", ".join(parts) if parts else empty)


# Enough records to see optional keys without reading the whole table. The input
# schema has "many more keys" than the parser names, but it is one schema — a key
# absent from 200 records is not one the templating can rely on either.
_SCHEMA_SAMPLE = 200

# The key list can be long, and this block has to stay pasteable.
_MAX_KEYS = 60


def _pii_schema_lines(conn: sqlite3.Connection) -> list[str]:
    """Why `pii_terms` is thin: which PII_FIELDS keys the real input actually has.

    On the real corpus this comes out at exactly 1.000 term per merchant against
    ~11.7 on fixtures, which means all but one `PII_FIELDS` key misses the real
    schema. That is a PRIVACY defect rather than a metrics one — P3 templating
    masks a query by matching these terms, so nine missing keys means merchant
    names, streets and phones stay unmasked in anything the report quotes.

    Two lines settle it without a schema round-trip: which field buckets produced
    terms, and which key spellings the records actually carry.

    **Key NAMES only, never values.** The names are schema and safe to paste; the
    values are merchant PII and must not cross the air gap. `_capped(sorted(record))`
    in the outputs parser already relies on the same distinction.
    """
    from .inputs import PII_FIELDS

    # Positive confirmation: "no merchants ingested" and "merchants ingested whose
    # raw_json will not parse" are different failures with different fixes, and
    # this block reported both as the latter.
    if not _scalar(conn, "SELECT COUNT(*) FROM merchants"):
        return [_line("(no merchants)", "ingest input/ before this block means anything")]

    out = [
        _breakdown(
            conn,
            "SELECT field, COUNT(*) FROM pii_terms GROUP BY field ORDER BY 2 DESC",
            "terms by field",
            empty="NONE — no merchant PII can be templated out",
        )
    ]

    wanted = {key for keys in PII_FIELDS.values() for key in keys}
    present: set[str] = set()
    n_sampled = 0
    for row in conn.execute("SELECT raw_json FROM merchants LIMIT ?", (_SCHEMA_SAMPLE,)):
        try:
            record = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            n_sampled += 1
            present.update(record)

    if not n_sampled:
        return out + [_line("input schema", "no parseable merchant raw_json to sample")]

    matched, missing = sorted(wanted & present), sorted(wanted - present)
    out.append(_line("PII_FIELDS found", f"{len(matched)} of {len(wanted)}: {', '.join(matched)}"))
    out.append(
        _line(
            "PII_FIELDS missing", ", ".join(missing) if missing else "none — schema fully matched"
        )
    )
    keys = sorted(present)
    shown = ", ".join(keys[:_MAX_KEYS])
    extra = f" ... (+{len(keys) - _MAX_KEYS} more)" if len(keys) > _MAX_KEYS else ""
    out.append(_line("input keys seen", f"{len(keys)} over {n_sampled} records"))
    out.append(f"    {shown}{extra}")
    return out


def _answer_source_lines(conn: sqlite3.Connection, n_a: int) -> list[str]:
    """Which of the three parses reached each answer, and where they disagree.

    Four questions live here, all of them previously unanswerable without an
    ad-hoc SQL round-trip, and all of them load-bearing for the scorecard:

    1. **Does `citation_evidence` cover every question?** `answer_dict` is proven
       complete — it rescued 1,047 whole runs at 48 answers each. Coverage for
       `citation_evidence` is inferred from "one entry per answer" and has never
       been counted. If it is thin, preferring it swaps a measured guarantee for
       an assumed one.
    2. **How much evidence does it repair?** Answers where our prose parse found
       no Evidence line but theirs did. Every one of those was being scored as a
       default-3 on our failure rather than on their pipeline's finding.
    3. **Do their two parses agree with each other?** On rows sourced from
       `answer_dict`, `answer_text` IS the dict's answer — so `agree_with_ce`
       there is a direct dict-vs-citation_evidence comparison. If they never
       disagree they are one source wearing two names, and agreement between them
       proves nothing.
    4. **Do ours and theirs agree?** The rate that decides whether
       `--answer-source` changes any conclusion or is merely available.
    """
    have_ce = _scalar(conn, "SELECT COUNT(*) FROM answers WHERE ce_answer IS NOT NULL")
    out = [_line("ce_answer present", f"{have_ce:,} of {n_a:,} ({have_ce / n_a:.1%})")]

    # Per (record, run): does citation_evidence carry all 48, or only some?
    cov = conn.execute(
        "SELECT COUNT(*) AS runs, SUM(c = 48) AS full, SUM(c = 0) AS none, "
        "COALESCE(AVG(c), 0) AS avg FROM ("
        "  SELECT output_id, run_id, COUNT(ce_answer) AS c FROM answers "
        "  GROUP BY output_id, run_id)"
    ).fetchone()
    out.append(
        _line(
            "ce coverage / run",
            f"{cov['full'] or 0:,} of {cov['runs']:,} runs have all 48 "
            f"({cov['none'] or 0:,} have none, mean {cov['avg']:.1f})",
        )
    )

    repaired = _scalar(
        conn,
        "SELECT COUNT(*) FROM answer_facts "
        "WHERE evidence_shape != 'present' AND ce_evidence_shape = 'present'",
    )
    out.append(
        _line("evidence repaired", f"{repaired:,} answers where only THEIR parse found evidence")
    )

    # Their dict vs their citation_evidence, isolated on the rows where
    # answer_text is the dict's own answer.
    dictrows = conn.execute(
        "SELECT SUM(agree_with_ce = 1) a, SUM(agree_with_ce = 0) d, COUNT(*) n "
        "FROM answers WHERE parsed_from = 'answer_dict'"
    ).fetchone()
    if dictrows["n"]:
        out.append(
            _line(
                "dict vs ce",
                f"{dictrows['a'] or 0:,} agree, {dictrows['d'] or 0:,} differ "
                f"(of {dictrows['n']:,} dict-sourced answers)",
            )
        )
    else:
        out.append(_line("dict vs ce", "no answer_dict-sourced rows to compare on"))

    ours = conn.execute(
        "SELECT SUM(agree_with_ce = 1) a, SUM(agree_with_ce = 0) d, "
        "SUM(agree_with_ce IS NULL) n FROM answers"
    ).fetchone()
    comparable = (ours["a"] or 0) + (ours["d"] or 0)
    rate = f"{(ours['d'] or 0) / comparable:.1%} differ" if comparable else "n/a"
    out.append(
        _line(
            "ours vs ce",
            f"{ours['a'] or 0:,} agree, {ours['d'] or 0:,} differ, "
            f"{ours['n'] or 0:,} not comparable ({rate})",
        )
    )
    return out


def health_report(conn: sqlite3.Connection) -> str:
    """Everything worth knowing about an ingest, in one screen.

    Ordered so the load-bearing facts come first: if `questions` is not 48 or
    `answers` is 0, nothing below it means anything.
    """
    out = ["```", "COA DOCTOR"]

    out += ["", "CORPUS"]
    out.append(_line("merchants (input)", _scalar(conn, "SELECT COUNT(*) FROM merchants")))
    out.append(_line("merchants (logs)", _scalar(conn, "SELECT COUNT(DISTINCT se10) FROM runs")))
    out.append(
        _line(
            "merchants (outputs)",
            _scalar(conn, "SELECT COUNT(DISTINCT se10) FROM output_records"),
        )
    )
    out.append(_line("runs", _scalar(conn, "SELECT COUNT(*) FROM runs")))
    out.append(_line("output records", _scalar(conn, "SELECT COUNT(*) FROM output_records")))
    out.append(_line("pii terms", _scalar(conn, "SELECT COUNT(*) FROM pii_terms")))

    out += ["", "INPUT SCHEMA / PII"]
    out += _pii_schema_lines(conn)

    # 48 is the whole premise of the per-question scorecard. Anything else and
    # the primary deliverable has no stable denominator.
    n_q = _scalar(conn, "SELECT COUNT(*) FROM questions")
    out += ["", "QUESTIONS"]
    out.append(_line("canonical set", f"{n_q}" + ("" if n_q == 48 else "   <-- EXPECTED 48")))

    out += ["", "SEARCH CALLS"]
    out.append(
        _breakdown(
            conn,
            "SELECT action_type, COUNT(*) FROM search_calls GROUP BY action_type ORDER BY 2 DESC",
            "by action_type",
        )
    )
    out.append(
        _breakdown(
            conn,
            "SELECT status, COUNT(*) FROM search_calls GROUP BY status ORDER BY 2 DESC",
            "by status",
        )
    )
    billed = _scalar(conn, "SELECT COUNT(*) FROM query_instances WHERE is_billed_query = 1")
    total_q = _scalar(conn, "SELECT COUNT(*) FROM query_instances")
    out.append(_line("query rows", f"{total_q} ({billed} billed, {total_q - billed} sub-queries)"))

    out += ["", "TOKENS / TIER"]
    out.append(
        _breakdown(
            conn,
            "SELECT COALESCE(service_tier,'<unset>'), COUNT(*) FROM runs "
            "GROUP BY 1 ORDER BY 2 DESC",
            "service tier",
        )
    )
    tok = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
        "COALESCE(SUM(cache_read),0) c, COALESCE(SUM(reasoning),0) r FROM runs"
    ).fetchone()
    hit = f"{tok['c'] / tok['i']:.1%}" if tok["i"] else "n/a"
    out.append(_line("input tokens", f"{tok['i']:,} (cache_read {tok['c']:,}, hit {hit})"))
    out.append(_line("output tokens", f"{tok['o']:,} (reasoning {tok['r']:,})"))

    out += ["", "ANSWERS"]
    n_a = _scalar(conn, "SELECT COUNT(*) FROM answers")
    out.append(_line("total", n_a))
    if n_a:
        out.append(
            _line("null answers", _scalar(conn, "SELECT COUNT(*) FROM answers WHERE is_null"))
        )
        out.append(
            _breakdown(
                conn,
                "SELECT parsed_from, COUNT(*) FROM answers GROUP BY parsed_from ORDER BY 2 DESC",
                "parsed from",
            )
        )
        # The three Evidence renderings. Classified by the `answer_facts` view so
        # this and the scorecard cannot drift apart — a second definition of
        # "no evidence" is precisely how `evidence_text IS NULL` came to be
        # treated as the whole story when it covers only 4% of the cases.
        out.append(
            _breakdown(
                conn,
                "SELECT evidence_shape, COUNT(*) FROM answer_facts GROUP BY 1 ORDER BY 2 DESC",
                "evidence shape",
            )
        )
        out.append(
            _breakdown(
                conn,
                "SELECT ce_evidence_shape, COUNT(*) FROM answer_facts GROUP BY 1 ORDER BY 2 DESC",
                "ce evidence shape",
            )
        )
        # Decides whether agree_with_dict is a usable signal or just their
        # normalization differing from ours.
        agree = conn.execute(
            "SELECT SUM(agree_with_dict = 1) a, SUM(agree_with_dict = 0) d, "
            "SUM(agree_with_dict IS NULL) n FROM answers"
        ).fetchone()
        comparable = (agree["a"] or 0) + (agree["d"] or 0)
        rate = f"{(agree['d'] or 0) / comparable:.1%} differ" if comparable else "n/a"
        out.append(
            _line(
                "vs answer_dict",
                f"{agree['a'] or 0} agree, {agree['d'] or 0} differ, "
                f"{agree['n'] or 0} not comparable ({rate})",
            )
        )

        out += ["", "ANSWER SOURCES"]
        out += _answer_source_lines(conn, n_a)

    # Settles whether PROSE_CITATION_RE is too strict without moving any evidence
    # text across the air gap. The regex requires the markdown link to be wrapped
    # in its own parens -- `([t](u))`. If `any link` greatly exceeds `paren-wrapped`
    # the bare `[t](u)` form is being dropped, and the prose-vs-dict gap is our bug
    # rather than a finding about their post-processing.
    if n_a:
        links = conn.execute(
            "SELECT COUNT(*) a, SUM(evidence_text LIKE '%([%](%') w FROM answers "
            "WHERE evidence_text LIKE '%](%'"
        ).fetchone()
        any_link, wrapped = links["a"] or 0, links["w"] or 0
        out.append(
            _line(
                "evidence md links",
                f"{any_link:,} answers contain one; {wrapped:,} paren-wrapped, "
                f"{any_link - wrapped:,} bare",
            )
        )

    out += ["", "CITATIONS / VOTES"]
    out.append(
        _breakdown(
            conn,
            "SELECT source, COUNT(*) FROM citations GROUP BY source ORDER BY 2 DESC",
            "citations by source",
            empty="NONE — check whether citation_evidence exists in the records",
        )
    )
    out.append(
        _line(
            "empty placeholders",
            _scalar(conn, "SELECT COUNT(*) FROM citations WHERE empty_placeholder = 1"),
        )
    )
    n_v = _scalar(conn, "SELECT COUNT(*) FROM votes")
    differs = _scalar(conn, "SELECT COUNT(*) FROM votes WHERE differs = 1")
    out.append(_line("votes", f"{n_v} ({differs} majority/final differ)"))

    out += ["", "ANOMALIES"]
    rows = _rows(conn, "SELECT code, COUNT(*) FROM anomalies GROUP BY code ORDER BY 2 DESC, code")
    if not rows:
        out.append(_line("(none)", "suspicious on real data — usually a detector not wired up"))
    for r in rows:
        out.append(_line(r[0], f"{r[1]:,}"))

    out.append("```")
    return "\n".join(out)
