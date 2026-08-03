"""`coa doctor` — the air gap's primary diagnostic surface.

Tested for the property that matters: it must distinguish "clean" from "the
detector never ran", and must not raise on an empty or partial database, since
the operator runs it precisely when something looks wrong.
"""

from __future__ import annotations

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
    assert len(out.splitlines()) < 80, "doctor output must stay pasteable"
    assert "canonical set          48" in out
