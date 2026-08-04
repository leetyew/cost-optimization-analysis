"""`coa doctor` — the air gap's primary diagnostic surface.

Tested for the property that matters: it must distinguish "clean" from "the
detector never ran", and must not raise on an empty or partial database, since
the operator runs it precisely when something looks wrong.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coa.db import connect
from coa.health import health_report


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def test_empty_database_reports_without_raising(conn: sqlite3.Connection) -> None:
    """Run on a failed ingest, this must still print rather than traceback."""
    out = health_report(conn)
    assert "COA DOCTOR" in out
    assert out.startswith("```") and out.rstrip().endswith("```")


def test_wrong_question_count_is_called_out(conn: sqlite3.Connection) -> None:
    """48 is the premise of the per-question scorecard; anything else must shout."""
    conn.execute("INSERT INTO questions (qnum, text) VALUES (1, 'only one')")
    conn.commit()
    assert "EXPECTED 48" in health_report(conn)

    conn.executemany(
        "INSERT INTO questions (qnum, text) VALUES (?, 'q')", [(i,) for i in range(2, 49)]
    )
    conn.commit()
    assert "EXPECTED 48" not in health_report(conn)


def test_absent_citation_source_is_distinguishable_from_clean(
    conn: sqlite3.Connection,
) -> None:
    """Zero mismatches can mean agreement OR that citation_evidence is missing.

    The row counts are what separate those, which no anomaly tally could.
    """
    out = health_report(conn)
    assert "check whether citation_evidence exists" in out

    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'A','o',1)")
    conn.execute(
        "INSERT INTO citations (se10, output_id, url, source) "
        "VALUES ('A', 1, 'https://x.test', 'markdown_prose')"
    )
    conn.commit()
    out = health_report(conn)
    assert "markdown_prose 1" in out
    assert "check whether citation_evidence exists" not in out


def test_evidence_shapes_are_derived_from_stored_text(conn: sqlite3.Connection) -> None:
    """Which rendering the corpus uses was an open question; the data answers it."""
    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'A','o',1)")
    for qnum, ev in ((1, None), (2, ""), (3, "NULL"), (4, "real evidence")):
        conn.execute(
            "INSERT INTO answers (se10, output_id, qnum, answer_text, evidence_text) "
            "VALUES ('A', 1, ?, '3', ?)",
            (qnum, ev),
        )
    conn.commit()
    out = health_report(conn)
    for shape in ("absent 1", "empty 1", "null 1", "present 1"):
        assert shape in out, shape


def test_agreement_rate_is_reported(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'A','o',1)")
    for qnum, agree in ((1, 1), (2, 0), (3, None)):
        conn.execute(
            "INSERT INTO answers (se10, output_id, qnum, answer_text, agree_with_dict) "
            "VALUES ('A', 1, ?, '3', ?)",
            (qnum, agree),
        )
    conn.commit()
    out = health_report(conn)
    assert "1 agree, 1 differ, 1 not comparable (50.0% differ)" in out


def test_no_anomalies_is_labelled_suspicious(conn: sqlite3.Connection) -> None:
    """On real data an empty anomaly table usually means a detector is unwired."""
    assert "suspicious on real data" in health_report(conn)


def test_corpus_report_is_paste_sized(corpus) -> None:
    out = health_report(corpus.conn)
    # One terminal screen. The ANSWER SOURCES block earns its six lines: it is the
    # only way the operator can tell whether citation_evidence covers every answer,
    # and the alternative is an ad-hoc SQL round-trip across the air gap.
    assert len(out.splitlines()) < 90, "doctor output must stay one screen"
    assert "canonical set          48" in out


def test_pii_schema_block_names_the_keys_that_missed(corpus) -> None:
    """The line that explains a thin `pii_terms` without a schema round-trip.

    Real data yields exactly 1.000 term per merchant against ~11.7 here, meaning
    nine of ten `PII_FIELDS` keys miss the real input schema. Knowing WHICH is the
    whole diagnostic, and it is unavailable from any count already printed.
    """
    from coa.inputs import PII_FIELDS

    out = health_report(corpus.conn)
    assert "INPUT SCHEMA / PII" in out
    wanted = {key for keys in PII_FIELDS.values() for key in keys}
    assert f"{len(wanted)} of {len(wanted)}" in out, "fixtures match PII_FIELDS by construction"
    for key in wanted:
        assert key in out


def test_pii_schema_block_prints_key_names_never_values(corpus) -> None:
    """Schema is safe to paste across the air gap; merchant values are not."""
    out = health_report(corpus.conn)
    for row in corpus.conn.execute(
        "SELECT street, email, phone, owner_name, owner_postal FROM merchants LIMIT 10"
    ):
        for value in row:
            assert value, "fixture merchants must carry PII for this to prove anything"
            assert str(value) not in out


def test_pii_schema_block_survives_unparseable_raw_json(conn: sqlite3.Connection) -> None:
    """`raw_json` is retained verbatim, so it can hold anything a source line did."""
    conn.execute("INSERT INTO merchants (se10, raw_json) VALUES (?, ?)", ("1", "{truncated..."))
    out = health_report(conn)
    assert "no parseable merchant raw_json to sample" in out


def test_pii_schema_block_distinguishes_no_merchants_from_bad_json(
    conn: sqlite3.Connection,
) -> None:
    """Positive confirmation: an empty table is not a parse failure.

    Reporting "no parseable raw_json" on a DB with no merchants at all sends the
    operator hunting a JSON bug that does not exist — the ambiguity this module
    was written to remove.
    """
    empty = health_report(conn)
    assert "no parseable merchant raw_json to sample" not in empty
    assert "(no merchants)" in empty


def test_label_candidates_report_fill_rate_and_coded_values(corpus) -> None:
    """ "Mainly null" and "entirely null" are different answers; only a rate says which.

    The operator flagged `se_not_good_seller_ind` as possibly-a-label but could not
    tell whether every row is null. No `merchants` column names it, so without this
    block it is invisible outside raw_json.
    """
    out = health_report(corpus.conn)
    assert "LABEL CANDIDATES" in out
    assert "se_not_good_seller_ind" in out
    # Low-cardinality coded flags show their values; the rate is always present.
    assert "non-null" in out and "distinct" in out


def test_label_candidate_absent_from_schema_says_so(conn: sqlite3.Connection) -> None:
    """0% and "the key does not exist" are different findings, per the module docstring."""
    conn.execute(
        "INSERT INTO merchants (se10, raw_json) VALUES (?, ?)",
        ("1", json.dumps({"se10": "1", "se_toc_name": "Acme"})),
    )
    out = health_report(conn)
    assert "key absent from every merchant record" in out


def test_label_candidate_present_only_past_the_schema_sample_is_still_counted(
    conn: sqlite3.Connection,
) -> None:
    """Presence is tracked over every record, not the 200-record schema sample.

    A mainly-null key is exactly the kind that appears first at record 5,000, and
    calling it absent while reporting a non-zero count contradicts itself.
    """
    for i in range(250):
        record = {"se10": str(i)}
        if i == 240:
            record["se_not_good_seller_ind"] = "Y"
        conn.execute(
            "INSERT INTO merchants (se10, raw_json) VALUES (?, ?)", (str(i), json.dumps(record))
        )
    out = health_report(conn)
    assert (
        "key absent from every merchant record"
        not in out.split("LABEL CANDIDATES")[1].split("\n")[1]
    )
    assert "1 of 250 non-null" in out


def test_high_cardinality_label_values_are_not_printed(conn: sqlite3.Connection) -> None:
    """A free-text reason field could carry merchant specifics; only the count leaves."""
    for i in range(12):
        conn.execute(
            "INSERT INTO merchants (se10, raw_json) VALUES (?, ?)",
            (str(i), json.dumps({"se10": str(i), "se_not_good_reason": f"case detail {i}"})),
        )
    out = health_report(conn)
    assert "12 distinct" in out
    assert "case detail 0" not in out


def test_prose_unreadable_counts_runs_not_answers(conn: sqlite3.Connection) -> None:
    """`parsed from` is per ANSWER and cannot see whole runs falling back to the dict.

    1,047 runs where every answer came from `answer_dict` and 50,256 dict answers
    scattered across many runs give the same per-answer breakdown, but only the
    first is the population `coa scorecard` excludes from its evidence-dependent
    rates. The per-run figure is the one that has to be checkable.
    """
    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'m','x',1)")
    conn.executemany(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text, parsed_from) "
        "VALUES ('m', 1, ?, ?, '3', ?)",
        [
            (0, 1, "answer_dict"),  # run 0: every answer from the dict
            (0, 2, "answer_dict"),
            (1, 1, "answers_text"),  # run 1: mixed
            (1, 2, "answer_dict"),
            (2, 1, "answers_text"),  # run 2: prose read it all
            (2, 2, "answers_text"),
        ],
    )
    out = health_report(conn)
    assert "1 of 3 runs" in out, "one run is all-dict, not two and not six answers"
    assert "1 part-dict" in out


def test_run_reconciliation_reports_both_directions(conn: sqlite3.Connection) -> None:
    """A run with answers but no log carries no tokens, so every cost is a floor.

    The real corpus has 49,381 log runs against 50,420 output runs; naming which
    side each orphan sits on is what turns "~1,039 unexplained" into something
    that can be chased.
    """
    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'m','x',1)")
    conn.executemany(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text) VALUES ('m',1,?,1,'3')",
        [(0,), (1,)],  # output knows runs 0 and 1
    )
    conn.execute(
        "INSERT INTO runs (se10, run_id, run_key, src_file, src_line) "
        "VALUES ('m', 0, 'run_0', 'x', 1)"  # ...logs know only run 0
    )
    out = health_report(conn)
    assert "1 / 2 —" in out
    assert "1 output-only" in out
    assert "cost is a FLOOR" in out
    assert "0 log-only" in out


def test_run_reconciliation_is_quiet_when_the_sides_agree(conn: sqlite3.Connection) -> None:
    """The line must be evidence of a real gap, not permanent noise."""
    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'m','x',1)")
    conn.execute(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text) VALUES ('m',1,0,1,'3')"
    )
    conn.execute(
        "INSERT INTO runs (se10, run_id, run_key, src_file, src_line) VALUES ('m',0,'run_0','x',1)"
    )
    assert "identical run sets" in health_report(conn)


def test_run_reconciliation_names_the_runs_it_had_to_exclude(conn: sqlite3.Connection) -> None:
    """A NULL run_id has no join key, so it is excluded — but silently it contradicts.

    The `runs` line directly above counts every run; this one can only count runs
    with a parseable run_id. Left unlabelled the block reads `runs 60` then
    `59 / 69`, which the operator cannot investigate from a pasted screen.
    """
    conn.execute("INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1,'m','x',1)")
    conn.execute(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text) VALUES ('m',1,0,1,'3')"
    )
    conn.executemany(
        "INSERT INTO runs (se10, run_id, run_key, src_file, src_line) VALUES ('m',?,?,'x',1)",
        [(0, "run_0"), (None, "retry_final")],  # one parseable, one not
    )
    out = health_report(conn)
    assert "runs                   2" in out, "the runs line counts both"
    assert "1 / 1 [excl. 1 unparsed run key(s)]" in out, "this line must say why it counts one"
