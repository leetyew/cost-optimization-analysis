"""Per-question scorecard tests.

Every trap guarded here has already cost this project a wrong number once, so the
assertions are written against the planted fixture counts rather than against the
scorecard's own output — a test that restates the implementation cannot fail when
the implementation is wrong.
"""

from __future__ import annotations

import sqlite3

import pytest

from coa.scorecard import ANSWER_SOURCES, question_scorecard, render_scorecard


@pytest.fixture(scope="module")
def sc(corpus):
    return question_scorecard(corpus.conn)


def test_one_row_per_question(sc, golden: dict) -> None:
    assert len(sc.rows) == golden["n_questions"]
    assert [r.qnum for r in sc.rows] == list(range(1, golden["n_questions"] + 1))


def test_null_rate_keys_on_the_answer_not_the_evidence(sc, golden: dict) -> None:
    """Trap 1, the one that would invert the headline.

    Evidence being NULL is the SPECIFIED outcome for a scale answer above 3 — the
    search found nothing adverse, which is a good result. The fixture plants far
    more NULL-evidence answers than NULL answers, so a scorecard reading evidence
    as the null signal fails here by a wide margin rather than subtly.
    """
    total_null = sum(r.n_null for r in sc.rows)
    assert total_null == golden["outputs"]["null_answers"]
    assert golden["outputs"]["evidence_null"] > total_null


def test_default3_requires_absent_evidence_in_all_three_renderings(sc, golden: dict) -> None:
    """Trap 2: `evidence_text IS NULL` alone catches a small minority of them.

    The fixture renders "no evidence" three ways — omitted line, bare label and
    literal NULL — and the planted default-3 answers are spread across all of
    them. Only a scorecard classifying all three as "no evidence" reaches the
    planted total.
    """
    assert sum(r.n_default3 for r in sc.rows) == golden["outputs"]["scale_default_3_no_evidence"]


def test_default3_excludes_threes_that_carried_evidence(sc, golden: dict) -> None:
    """A 3 the search supported is not a drop candidate; only a defaulted one is."""
    supported = (
        golden["outputs"]["scale_default_3"] - (golden["outputs"]["scale_default_3_no_evidence"])
    )
    assert supported > 0, "fixture must contain both kinds of 3 for this to mean anything"
    assert sum(r.n_default3 for r in sc.rows) < golden["outputs"]["scale_default_3"]


def test_noinfo_unifies_both_answer_regimes(sc) -> None:
    """The drop signal is NULL answers plus defaulted 3s, and nothing else."""
    for r in sc.rows:
        assert r.n_noinfo == r.n_null + r.n_default3, f"Q{r.qnum}"
        assert r.n_noinfo <= r.n


def test_citation_rate_counts_answers_not_citation_rows(corpus, sc) -> None:
    """Trap 3: `citations` holds BOTH sources for the same fact.

    Prose and citation_evidence each contribute a row per cited answer, so a
    scorecard counting rows double-counts and can exceed 100%. It also has to
    filter on `url` — citation_evidence carries one entry per answer with a NULL
    url most of the time, and counting entries would score every question at 100%.
    """
    rows = corpus.conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
    assert rows > sum(r.n_cited for r in sc.rows), "fixture must have both sources to test this"
    for r in sc.rows:
        assert r.n_cited <= r.n, f"Q{r.qnum} cited more often than it was answered"
        assert 0.0 <= (r.citation_rate or 0.0) <= 1.0


def test_dict_recovered_answers_are_scored_not_dropped(corpus, sc, golden: dict) -> None:
    """Trap 6, and the reason the denominator is every answer.

    One planted run's prose is unreadable, so all 48 of its answers come from
    answer_dict. They still carry real ANSWERS, so dropping them from the NULL
    rate would discard measurements rather than protect them — only the
    evidence-dependent half narrows, which
    `test_default3_excludes_answers_whose_evidence_was_unobservable` covers.
    """
    n_dict = corpus.conn.execute(
        "SELECT COUNT(*) FROM answers WHERE parsed_from = 'answer_dict'"
    ).fetchone()[0]
    assert n_dict == golden["outputs"]["answers_from_dict"]
    assert sc.n_answers == sum(r.n for r in sc.rows)
    assert sc.n_answers == corpus.conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]


def test_unreadable_runs_have_no_evidence_from_any_source(corpus, golden: dict) -> None:
    """citation_evidence is co-derived with the prose, so it dies with it.

    Measured on the real corpus 2026-08-04: it covers exactly the 49,373 runs
    whose prose parsed (x48 = 2,369,904 answers, to the unit) and is ABSENT for
    the other 1,047. It is therefore NOT the repair path for the runs that need
    one — only `answer_dict` survives them, and it carries no evidence at all.
    """
    blind = corpus.conn.execute(
        "SELECT COUNT(*) FROM answers WHERE parsed_from = 'answer_dict' AND ce_answer IS NULL"
    ).fetchone()[0]
    assert blind == golden["outputs"]["evidence_unobservable"]


def test_default3_excludes_answers_whose_evidence_was_unobservable(sc, golden: dict) -> None:
    """Invariant 5: a 3 we cannot check is not a measured default-3.

    Their prose exists for those runs; nothing here could read it. Scoring them
    as evidence-less would charge our own parse failure to the other team's
    question set — and it is 50,256 answers on the real corpus.
    """
    blind = golden["outputs"]["evidence_unobservable"]
    assert blind > 0, "fixture must contain an unreadable run for this to mean anything"
    assert sum(r.n_ev_seen for r in sc.rows) == sc.n_answers - blind
    for r in sc.rows:
        assert r.n_default3 <= r.n_ev_seen


def test_kind_is_derived_from_answers_never_from_question_text(sc) -> None:
    """Trap 7: question text varies per merchant and carries PII."""
    kinds = {r.kind for r in sc.rows}
    assert kinds <= {"scale", "text", "mixed", "-"}
    assert "scale" in kinds and "text" in kinds
    for r in sc.rows:
        assert r.n_scale + r.n_text + r.n_null >= r.n - r.n_null


def test_scale_detection_accepts_a_digit_followed_by_prose(corpus) -> None:
    """Whether a scale answer is a bare digit is UNCONFIRMED on real data.

    `'3'` and `'3 - no adverse media'` must classify alike, while `'35'` must not
    read as a scale answer at all.
    """
    got = corpus.conn.execute(
        "SELECT ? GLOB '[1-5]' OR ? GLOB '[1-5][^0-9]*', "
        "       ? GLOB '[1-5]' OR ? GLOB '[1-5][^0-9]*', "
        "       ? GLOB '[1-5]' OR ? GLOB '[1-5][^0-9]*'",
        ("3", "3", "3 - no adverse media", "3 - no adverse media", "35", "35"),
    ).fetchone()
    assert tuple(got) == (1, 1, 0)


def test_inter_run_agreement_needs_two_runs(sc) -> None:
    """A single-run merchant cannot agree or disagree with itself."""
    for r in sc.rows:
        assert r.n_runs_multi <= r.n
        assert r.n_runs_agree <= r.n_runs_multi


def test_answer_source_switches_which_parse_is_scored(corpus, golden: dict) -> None:
    """The whole point of storing three parses instead of choosing at ingest."""
    prose = question_scorecard(corpus.conn, "prose")
    theirs = question_scorecard(corpus.conn, "ce")
    assert prose.source == "prose" and theirs.source == "ce"
    assert prose.n_answers == theirs.n_answers
    # One answer is planted to differ between the parses, so the two scorecards
    # must not be identical -- otherwise the flag is decoration.
    assert golden["outputs"]["ce_answer_mismatch"] > 0
    assert [r.n_null for r in prose.rows] != [r.n_null for r in theirs.rows] or any(
        r.n_ce_differ for r in prose.rows
    )


def test_unknown_answer_source_is_refused(corpus) -> None:
    with pytest.raises(ValueError, match="unknown answer source"):
        question_scorecard(corpus.conn, "whatever")


def test_render_never_prints_question_text(corpus, sc) -> None:
    """Trap 4: `questions.text` carries merchant PII inline.

    The scorecard is designed to be pasted back across the air gap, so a leak here
    is a privacy incident rather than a formatting slip.
    """
    out = render_scorecard(sc)
    for row in corpus.conn.execute("SELECT text FROM questions"):
        stem = row["text"].split("?")[0][:40]
        if stem:
            assert stem not in out
    for merchant in corpus.conn.execute(
        "SELECT street, owner_name, owner_postal FROM merchants LIMIT 5"
    ):
        for value in merchant:
            assert value and str(value) not in out


def test_render_lists_every_question_and_flags_the_capped_section(sc) -> None:
    out = render_scorecard(sc, top=5)
    assert "QUESTION SCORECARD" in out
    for r in sc.rows:
        assert f"  {r.qnum:>4} " in out
    assert f"{len(sc.rows) - 5} more questions" in out


def test_render_reports_null_rate_for_every_source(sc) -> None:
    """The headline moves with whose parse is primary, so it is never printed alone."""
    out = render_scorecard(sc)
    assert "NULL RATE BY SOURCE" in out
    for name in sc.by_source_null:
        assert name in out


def test_subquery_picture_names_its_denominators(sc) -> None:
    """Trap 5: log runs and output runs are different populations.

    19,269 merchants have logs and 19,349 have outputs, so dividing a query total
    by the wrong run count turns a coincidence into a finding. The fixture has
    60 log runs against 70 output runs, so the warning path must fire.
    """
    s = sc.subqueries
    assert s.n_log_runs and s.n_output_runs
    assert s.n_log_runs != s.n_output_runs
    assert "DIFFERENT populations" in render_scorecard(sc)


def test_empty_db_renders_without_crashing() -> None:
    from coa.db import connect

    conn = connect(":memory:")
    try:
        out = render_scorecard(question_scorecard(conn))
        assert "no answers ingested" in out
    finally:
        conn.close()


def test_schema_guard_rejects_a_db_predating_the_ce_columns() -> None:
    """An old DB must fail with an instruction, not with a cryptic INSERT error."""

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE answers (id INTEGER PRIMARY KEY, answer_text TEXT)")
    with pytest.raises(RuntimeError, match="Re-ingest"):
        from coa.db import _check_schema_current

        _check_schema_current(conn)
    conn.close()
    assert set(ANSWER_SOURCES) == {"prose", "ce"}
