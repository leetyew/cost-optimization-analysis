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
        # The three Evidence renderings, derived rather than remembered: which one
        # the corpus uses was an open question the data settles.
        out.append(
            _breakdown(
                conn,
                "SELECT CASE WHEN evidence_text IS NULL THEN 'absent' "
                "WHEN TRIM(evidence_text) = '' THEN 'empty' "
                "WHEN UPPER(TRIM(evidence_text)) = 'NULL' THEN 'null' ELSE 'present' END, "
                "COUNT(*) FROM answers GROUP BY 1 ORDER BY 2 DESC",
                "evidence shape",
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
