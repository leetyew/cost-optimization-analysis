"""The run-count lever (`coa runs`).

The arithmetic here decides whether a fifth of the bill is cuttable, so the tests
are built around the two ways the figure could be wrong in the *unsafe*
direction: dropping a billed call from the cost of a marginal run, and reporting
a third run as redundant when it actually decided the answer.

The third-run classification is asserted against hand-built answers rather than
the fixture corpus, because the corpus's answers are random — its rates are
meaningless, and only the partition arithmetic can be checked there.
"""

from __future__ import annotations

import sqlite3

import pytest

from coa.config import Pricing, TierRates
from coa.db import connect
from coa.runs import (
    KEEP_RUNS,
    render_run_economics,
    render_third_run,
    run_count_distribution,
    third_run_picture,
    usage_by_run,
)
from coa.scorecard import ANSWER_SOURCES

PROSE = ANSWER_SOURCES["prose"]

# Standard rates only, matching config.yaml, so cost assertions stay arithmetic.
PRICED = Pricing(tiers={"standard": TierRates(2.50, 0.25, 15.00, 10.00)})


@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "runs.sqlite")
    yield conn
    conn.close()


def _run(conn: sqlite3.Connection, se10: str, run_id: int, **kw) -> int:
    cur = conn.execute(
        "INSERT INTO runs (se10, run_id, run_key, service_tier, input_tokens, output_tokens, "
        "total_tokens, cache_read, reasoning, src_file, src_line) "
        "VALUES (?, ?, ?, 'standard', ?, ?, ?, ?, 0, 'x', 1)",
        (
            se10,
            run_id,
            f"run_{run_id}",
            kw.get("input_tokens", 1000),
            kw.get("output_tokens", 100),
            kw.get("input_tokens", 1000) + kw.get("output_tokens", 100),
            kw.get("cache_read", 0),
        ),
    )
    return cur.lastrowid


def _call(conn: sqlite3.Connection, se10: str, run_pk: int | None, run_id: int | None) -> None:
    conn.execute(
        "INSERT INTO search_calls (se10, run_pk, run_id, call_index, action_type, raw_json, "
        "parse_conf, src_file, src_line) VALUES (?, ?, ?, 0, 'search', '{}', 'clean', 'x', 1)",
        (se10, run_pk, run_id),
    )


def _answers(conn: sqlite3.Connection, output_id: int, qnum: int, texts: list[str]) -> None:
    """One answer per run for a (record, question), in run order."""
    conn.executemany(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text) VALUES (?, ?, ?, ?, ?)",
        [("1", output_id, i, qnum, t) for i, t in enumerate(texts)],
    )


def _record(conn: sqlite3.Connection, output_id: int) -> None:
    conn.execute(
        "INSERT INTO output_records (id, se10, src_file, src_line) VALUES (?, '1', 'x', 1)",
        (output_id,),
    )


# ---------------------------------------------------------------------------
# Marginal-run cost
# ---------------------------------------------------------------------------


def test_run_slices_split_by_index(db: sqlite3.Connection) -> None:
    """Cost has to be attributable to a run index or the lever cannot be sized."""
    for run_id in (0, 1, 2):
        pk = _run(db, "1", run_id)
        _call(db, "1", pk, run_id)

    slices = usage_by_run(db)
    assert [s.run_id for s in slices] == [0, 1, 2]
    assert all(s.n_runs == 1 and s.n_calls == 1 for s in slices)


def test_marginal_run_is_cheapest_on_input_but_not_on_fee(db: sqlite3.Connection) -> None:
    """The whole caveat, as arithmetic: run_2's input is cached, its fee is not.

    If this ever inverts, the report would claim a saving that the caching lever
    has already taken.
    """
    _call(db, "1", _run(db, "1", 0, input_tokens=10_000, cache_read=0), 0)
    _call(db, "1", _run(db, "1", 2, input_tokens=10_000, cache_read=9_000), 2)

    by_id = {s.run_id: s for s in usage_by_run(db)}
    assert by_id[0].cost(PRICED) > by_id[2].cost(PRICED), "cached run must cost less"
    # ...but the per-call fee is identical, which is the part that cuts linearly.
    assert by_id[0].fee(PRICED) == by_id[2].fee(PRICED)


def test_calls_with_no_run_are_not_dropped(db: sqlite3.Connection) -> None:
    """An orphaned billed call vanishing from the cost is the wrong way to be wrong."""
    _call(db, "1", None, None)
    assert sum(s.n_calls for s in usage_by_run(db)) == 1


def test_slice_is_unpriced_when_any_of_its_tiers_is(db: sqlite3.Connection) -> None:
    """A partial sum would understate the cost of the run we propose to cut."""
    _run(db, "1", 0)
    assert usage_by_run(db)[0].cost(Pricing(tiers={})) is None


def test_run_count_distribution_counts_merchants(db: sqlite3.Connection) -> None:
    """The denominator for `2.6 runs each`, from the LOG side that carries tokens."""
    for run_id in (0, 1, 2):
        _run(db, "three", run_id)
    for run_id in (0, 1):
        _run(db, "two", run_id)

    assert run_count_distribution(db) == [(2, 1), (3, 1)]


def test_economics_report_names_the_cut(db: sqlite3.Connection) -> None:
    for run_id in (0, 1, 2):
        _call(db, "1", _run(db, "1", run_id), run_id)

    out = render_run_economics(usage_by_run(db), run_count_distribution(db), PRICED)
    assert f"CUTTING TO {KEEP_RUNS} RUNS PER MERCHANT" in out
    assert "LINEAR in runs" in out
    # One run of three is removed.
    assert "removes          1 runs (33.3% of all runs)" in out


def test_economics_report_says_when_there_is_nothing_to_cut(db: sqlite3.Connection) -> None:
    """Reporting a saving on a corpus with no third run would be fiction."""
    for run_id in (0, 1):
        _run(db, "1", run_id)
    out = render_run_economics(usage_by_run(db), run_count_distribution(db), PRICED)
    assert "no marginal run to cut" in out


# ---------------------------------------------------------------------------
# Third-run value — the classification that inverts the intuition
# ---------------------------------------------------------------------------


def test_third_run_is_redundant_when_the_first_two_agree(db: sqlite3.Connection) -> None:
    """If run_0 == run_1 the majority is fixed and run_2 cannot move it."""
    _record(db, 1)
    _answers(db, 1, 1, ["4", "4", "2"])

    r = third_run_picture(db, PROSE)[0]
    assert (r.n_three, r.n_redundant, r.n_decisive, r.n_no_majority) == (1, 1, 0, 0)
    assert r.redundant_rate == 1.0


def test_third_run_is_decisive_when_the_first_two_differ(db: sqlite3.Connection) -> None:
    """The inversion: low agreement makes run_2 decisive OFTEN, not rarely."""
    _record(db, 1)
    _answers(db, 1, 1, ["4", "2", "2"])

    r = third_run_picture(db, PROSE)[0]
    assert (r.n_redundant, r.n_decisive, r.n_no_majority) == (0, 1, 0)


def test_three_different_answers_leave_no_majority(db: sqlite3.Connection) -> None:
    """Three runs, three answers — there is nothing for a vote to resolve."""
    _record(db, 1)
    _answers(db, 1, 1, ["1", "3", "5"])

    r = third_run_picture(db, PROSE)[0]
    assert (r.n_decisive, r.n_no_majority) == (0, 1)
    assert r.no_majority_rate == 1.0


def test_buckets_partition_the_three_run_cases(db: sqlite3.Connection) -> None:
    """redundant + decisive + no_majority must account for every 3-run case."""
    _record(db, 1)
    _answers(db, 1, 1, ["4", "4", "2"])  # redundant
    _answers(db, 1, 2, ["4", "2", "2"])  # decisive
    _answers(db, 1, 3, ["1", "3", "5"])  # no majority

    for r in third_run_picture(db, PROSE):
        assert r.n_redundant + r.n_decisive + r.n_no_majority == r.n_three


def test_pair_agreement_is_conditioned_on_exactly_two_runs(db: sqlite3.Connection) -> None:
    """The clean figure the scorecard's `agree` is often mistaken for.

    `scorecard.agreement_rate` requires EVERY run to match, so it decays as run
    count rises. Conditioning on exactly two runs makes it comparable.
    """
    _record(db, 1)
    _record(db, 2)
    _answers(db, 1, 1, ["4", "4"])  # 2 runs, agree
    _answers(db, 2, 1, ["4", "2"])  # 2 runs, differ

    r = third_run_picture(db, PROSE)[0]
    assert (r.n_pairs, r.n_pairs_agree) == (2, 1)
    assert r.pair_agreement == 0.5
    assert r.n_three == 0, "2-run merchants must not leak into the 3-run buckets"


def test_two_null_answers_count_as_agreeing(db: sqlite3.Connection) -> None:
    """Both runs saying nothing IS agreement — it is a reproducible non-answer."""
    _record(db, 1)
    _answers(db, 1, 1, ["NULL", "NULL"])

    r = third_run_picture(db, PROSE)[0]
    assert r.n_pairs_agree == 1


def test_comparison_uses_the_three_lowest_run_ids(db: sqlite3.Connection) -> None:
    """A gap in run numbering must not silently drop a merchant from the analysis."""
    _record(db, 1)
    conn_rows = [(1, 0, "4"), (1, 5, "4"), (1, 9, "2")]
    db.executemany(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text) VALUES ('1', ?, ?, 1, ?)",
        conn_rows,
    )
    r = third_run_picture(db, PROSE)[0]
    assert r.n_three == 1
    assert r.n_redundant == 1, "run_0 and run_5 agree, so the third run is redundant"


def test_third_run_report_says_when_nothing_has_three_runs(db: sqlite3.Connection) -> None:
    _record(db, 1)
    _answers(db, 1, 1, ["4", "4"])
    assert "no third run to remove" in render_third_run(third_run_picture(db, PROSE))


def test_reports_run_on_the_corpus_without_raising(corpus) -> None:
    """Fixture answers are random, so only that it runs and partitions is assertable."""
    out = render_run_economics(
        usage_by_run(corpus.conn), run_count_distribution(corpus.conn), PRICED
    )
    assert "RUN ECONOMICS" in out
    rows = third_run_picture(corpus.conn, PROSE)
    assert rows
    for r in rows:
        assert r.n_redundant + r.n_decisive + r.n_no_majority == r.n_three
    assert "THIRD-RUN VALUE" in render_third_run(rows)


def test_saving_share_is_withheld_when_any_slice_is_unpriced(db: sqlite3.Connection) -> None:
    """A "% of total" against a partial denominator would inflate the lever.

    Summing only the priced slices and dividing by that shrinks the denominator,
    so the cut looks like a larger share of the bill than it is — the one
    direction this module must never err in.
    """
    for run_id in (0, 1, 2):
        _call(db, "1", _run(db, "1", run_id), run_id)
    db.execute("UPDATE runs SET service_tier = 'flex' WHERE run_id = 0")

    priced_standard_only = Pricing(tiers={"standard": TierRates(2.50, 0.25, 15.00, 10.00)})
    out = render_run_economics(usage_by_run(db), run_count_distribution(db), priced_standard_only)
    assert "of total" not in out or "needs every tier priced" in out
    assert "*** UNVERIFIED PRICING ***" in out


def test_saving_share_is_shown_when_everything_is_priced(db: sqlite3.Connection) -> None:
    """The real corpus is 100% `default`, so this is the path that will actually run."""
    for run_id in (0, 1, 2):
        _call(db, "1", _run(db, "1", run_id), run_id)

    out = render_run_economics(usage_by_run(db), run_count_distribution(db), PRICED)
    assert "of total)" in out


def test_orphan_call_keeps_its_own_run_index(db: sqlite3.Connection) -> None:
    """Bucketing on `search_calls.run_id`, not the joined `runs.run_id`.

    The two diverge exactly when `run_pk` failed to resolve. Grouping on the
    joined column would drop such a call into `unparsed`, understating the run it
    actually belongs to — and the cost of a marginal run is the whole argument.
    Its tier comes from the run row it could not reach, so it lands under
    `<unset>` and is costed at standard — the documented inference for an
    absent tier, and the safe direction for a cost figure.
    """
    _run(db, "1", 2)
    _call(db, "1", None, 2)  # run_pk unresolved, but the call knows its index

    by_id = {s.run_id: s for s in usage_by_run(db)}
    assert 2 in by_id and by_id[2].n_calls == 1, "call must land in run_2, not `unparsed`"
    assert None not in by_id
    assert "<unset>" in {u.tier for u in by_id[2].by_tier}
    # `<unset>` costs at STANDARD by design (Pricing.for_tier(None)): absent means
    # the API default was used. So the orphan is costed, never dropped — the same
    # direction `tier_usage` errs in, because a billed call missing from a cost
    # figure understates it.
    assert by_id[2].cost(PRICED) is not None


def test_third_run_block_flags_a_population_mismatch(db: sqlite3.Connection) -> None:
    """Cost is the log side, agreement the output side — never silently adjacent.

    On the real corpus 10,943 merchants have 3 log runs while 11,720 records have
    3 output runs. Printing a rate from one population next to a cost from the
    other invites multiplying them, which is the denominator error the scorecard's
    SubqueryPicture already guards against elsewhere.
    """
    _record(db, 1)
    _answers(db, 1, 1, ["4", "4", "2"])  # 1 output record with 3 runs
    for run_id in (0, 1):
        _run(db, "1", run_id)  # ...but only 2 LOG runs exist

    out = render_third_run(third_run_picture(db, PROSE), run_count_distribution(db))
    assert "POPULATION" in out
    assert "crosses populations" in out


def test_no_population_warning_when_the_two_agree(db: sqlite3.Connection) -> None:
    """The warning must be evidence of a real gap, not permanent noise."""
    _record(db, 1)
    _answers(db, 1, 1, ["4", "4", "2"])
    for run_id in (0, 1, 2):
        _run(db, "1", run_id)

    out = render_third_run(third_run_picture(db, PROSE), run_count_distribution(db))
    assert "POPULATION" not in out


def test_record_count_is_not_derived_by_dividing_by_question_count(
    db: sqlite3.Connection,
) -> None:
    """A record missing answers for some questions must not deflate the population.

    Dividing total cases by the question count floors it whenever coverage is
    uneven — the fixture corpus reports 431 cases over 48 questions, where the
    quotient (8) is not the record count at all.
    """
    _record(db, 1)
    _record(db, 2)
    _answers(db, 1, 1, ["4", "4", "2"])  # record 1 answers q1 and q2
    _answers(db, 1, 2, ["4", "4", "2"])
    _answers(db, 2, 1, ["4", "4", "2"])  # record 2 answers only q1

    out = render_third_run(third_run_picture(db, PROSE))
    assert "3-run cases        3 (record, question) pairs" in out
    assert "over 2 records" in out, "2 records, not 3//2 = 1"
    assert "not every record answers all" in out


def test_population_check_counts_every_bucket_at_three_or_more(db: sqlite3.Connection) -> None:
    """A 4-run merchant belongs in the log-side 3+ total, not a separate bucket.

    `n_records` counts records with >= 3 runs, so the log side must be summed the
    same way. Taking only the exactly-3 bucket compares different definitions and
    invents a gap. The real corpus tops out at 3 runs, which would have hidden this
    until a corpus with four did not.
    """
    for output_id, (se10, n) in enumerate([("a", 3), ("b", 4)], start=1):
        _record(db, output_id)
        _answers(db, output_id, 1, ["4"] * n)
        for run_id in range(n):
            _run(db, se10, run_id)

    assert run_count_distribution(db) == [(3, 1), (4, 1)]
    # Both records have >= 3 runs on both sides, so there is no gap to report.
    out = render_third_run(third_run_picture(db, PROSE), run_count_distribution(db))
    assert "POPULATION" not in out
