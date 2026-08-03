"""Cache economics: the two levers, and the floor that decides whether either works."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coa.config import Pricing, TierRates
from coa.db import connect
from coa.metrics import (
    CACHE_MIN_TOKENS,
    TierUsage,
    cache_picture,
    open_page_overlap,
    render_cache_report,
    render_cost_report,
    render_open_page_report,
    tier_usage,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def add_run(conn: sqlite3.Connection, se10: str, run_id: int, tokens: int, cached: int) -> None:
    conn.execute(
        "INSERT INTO runs (se10, run_id, run_key, input_tokens, cache_read, src_file, src_line) "
        "VALUES (?, ?, ?, ?, ?, 'f.jsonl', 1)",
        (se10, run_id, f"run_{run_id}", tokens, cached),
    )


def add_prompt(conn: sqlite3.Connection, se10: str, prompt: str) -> None:
    conn.execute(
        "INSERT INTO output_records (se10, question_user_prompt, src_file, src_line) "
        "VALUES (?, ?, 'o.jsonl', 1)",
        (se10, prompt),
    )


def test_hit_rate_splits_by_run_index(conn: sqlite3.Connection) -> None:
    """The split is the whole point: run_0 cache can only come from a shared
    prefix, run_1+ from the same merchant's earlier run. They have different fixes."""
    add_run(conn, "A", 0, 1000, 0)
    add_run(conn, "A", 1, 1000, 900)
    add_run(conn, "B", 0, 1000, 0)
    picture = cache_picture(conn)
    by_run = {r[0]: r for r in picture.by_run}
    assert by_run[0][3] == 0  # no cross-merchant caching
    assert by_run[1][3] == 900  # repeat-run caching works
    assert picture.hit_rate == pytest.approx(900 / 3000)


def test_inline_merchant_values_collapse_the_shared_prefix(conn: sqlite3.Connection) -> None:
    """Placeholders filled inside the questions end the prefix at the first one,
    which is why cross-merchant caching is structurally unavailable here."""
    add_run(conn, "A", 0, 1000, 0)
    add_prompt(conn, "A", "Preamble.\nQ1. Is Acme Widgets legitimate?\nQ2. more")
    add_prompt(conn, "B", "Preamble.\nQ1. Is Blue Harbor legitimate?\nQ2. more")
    picture = cache_picture(conn)
    assert picture.prefix_chars == len("Preamble.\nQ1. Is ")
    assert not picture.prefix_reaches_floor


def test_static_questions_first_gives_a_long_shared_prefix(conn: sqlite3.Connection) -> None:
    """The restructured shape: identical questions, merchant values in a tail block."""
    add_run(conn, "A", 0, 1000, 0)
    questions = "Q1. Is the merchant legitimate?\n" * 300  # comfortably over the floor
    add_prompt(conn, "A", questions + "\nMerchant: Acme Widgets")
    add_prompt(conn, "B", questions + "\nMerchant: Blue Harbor")
    picture = cache_picture(conn)
    assert picture.prefix_reaches_floor
    assert picture.prefix_tokens >= CACHE_MIN_TOKENS


def test_report_names_the_floor_when_the_prefix_is_short(conn: sqlite3.Connection) -> None:
    """A prefix under the floor caches nothing at all — there is no partial credit,
    so the report must not let it read as 'a little caching'."""
    add_run(conn, "A", 0, 1000, 0)
    add_prompt(conn, "A", "short prompt for A")
    add_prompt(conn, "B", "short prompt for B")
    out = render_cache_report(cache_picture(conn))
    assert "BELOW" in out and "caches NOTHING" in out


def test_empty_db_does_not_divide_by_zero(conn: sqlite3.Connection) -> None:
    picture = cache_picture(conn)
    assert picture.hit_rate == 0.0
    assert "no runs ingested" in render_cache_report(picture)


def test_runs_without_prompts_still_report(conn: sqlite3.Connection) -> None:
    """weblogs and output/ are separate sources; one can be ingested without the other."""
    add_run(conn, "A", 0, 1000, 100)
    out = render_cache_report(cache_picture(conn))
    assert "no prompts stored" in out


def test_corpus_cache_picture_is_computable(corpus) -> None:
    picture = cache_picture(corpus.conn)
    assert picture.n_runs == 60
    assert picture.n_prompts == 31
    assert 0.0 <= picture.hit_rate <= 1.0


def add_citation(conn: sqlite3.Connection, se10: str, url: str) -> None:
    conn.execute(
        "INSERT INTO output_records (id, se10, src_file, src_line) VALUES (1, ?, 'o', 1) "
        "ON CONFLICT DO NOTHING",
        (se10,),
    )
    conn.execute(
        "INSERT INTO citations (se10, output_id, url, source) VALUES (?, 1, ?, 'prose')",
        (se10, url),
    )


def add_open_page(conn: sqlite3.Connection, se10: str, url: str) -> None:
    conn.execute(
        "INSERT INTO search_calls (se10, call_index, action_type, url, raw_json, parse_conf, "
        "src_file, src_line) VALUES (?, 0, 'open_page', ?, '{}', 'clean', 'f', 1)",
        (se10, url),
    )


def test_open_page_overlap_is_scoped_per_merchant(conn: sqlite3.Connection) -> None:
    """The same URL cited for two merchants is two facts, not one."""
    add_citation(conn, "A", "https://x.test/1")
    add_open_page(conn, "A", "https://x.test/1")
    add_open_page(conn, "B", "https://x.test/1")  # opened for B, not cited by B
    overlap = open_page_overlap(conn)
    assert (overlap.cited_urls, overlap.both) == (1, 1)
    assert overlap.opened_urls == 2


def test_citation_never_opened_is_not_counted(conn: sqlite3.Connection) -> None:
    """Models cite from result snippets without opening; that is the expected case."""
    add_citation(conn, "A", "https://cited-only.test/1")
    add_open_page(conn, "A", "https://opened-only.test/2")
    overlap = open_page_overlap(conn)
    assert overlap.both == 0
    assert "cited them straight from" in render_open_page_report(overlap)


def test_full_coverage_still_disclaims_search_attribution(conn: sqlite3.Connection) -> None:
    """Even at 100%, this links a citation to a page open, never to the search."""
    add_citation(conn, "A", "https://x.test/1")
    add_open_page(conn, "A", "https://x.test/1")
    out = render_open_page_report(open_page_overlap(conn))
    assert "does NOT reach the search" in out


def test_no_citations_does_not_divide_by_zero(conn: sqlite3.Connection) -> None:
    assert open_page_overlap(conn).covered == 0.0
    assert "no citations ingested" in render_open_page_report(open_page_overlap(conn))


# --- cost ------------------------------------------------------------------

STANDARD = TierRates(
    input_per_mtok=2.50,
    cached_input_per_mtok=0.25,
    output_per_mtok=15.00,
    fee_per_1k_search_calls=10.00,
)


def usage(**over) -> TierUsage:
    base = dict(
        tier="standard",
        n_runs=1,
        input_tokens=1_000_000,
        cache_read=0,
        output_tokens=1_000_000,
        n_search_calls=1000,
        n_query_entries=4000,
    )
    return TierUsage(**{**base, **over})


def test_cost_applies_each_rate_to_its_own_volume() -> None:
    # 1M input @2.50 + 1M output @15.00 + 1000 calls @10/1k = 2.50 + 15.00 + 10.00
    assert usage().cost(STANDARD) == pytest.approx(27.50)


def test_cached_tokens_are_discounted_not_added() -> None:
    """cache_read is INSIDE input_tokens: full-rate input is the difference.

    Treating it as an addend would charge for 1.5M input tokens when only 1M were
    used — inflating the baseline, which is the worst way to be wrong in a
    document arguing another team should spend less.
    """
    all_cached = usage(cache_read=1_000_000)  # every input token cached
    # 1M @0.25 + 1M output @15 + 10 fee = 0.25 + 15 + 10
    assert all_cached.cost(STANDARD) == pytest.approx(25.25)
    # and it must be strictly cheaper than the uncached case, never more
    assert all_cached.cost(STANDARD) < usage().cost(STANDARD)


def test_reasoning_has_no_term_of_its_own() -> None:
    """reasoning is inside output_tokens, so cost depends only on output_tokens."""
    assert usage(output_tokens=1_000_000).cost(STANDARD) == pytest.approx(27.50)


def test_sub_queries_do_not_affect_cost() -> None:
    """Billing is per visible call; `queries` entries are volume, never money.

    Settled against the dashboard: $6,946.95 == 694,695 calls x $10/1K. Reading
    the sub-queries as billable would have inflated the corpus by ~3.4x.
    """
    assert usage(n_query_entries=4000).cost(STANDARD) == usage(n_query_entries=1).cost(STANDARD)


def test_unpriced_tier_returns_none_rather_than_zero() -> None:
    """A missing rate must not silently cost nothing."""
    assert usage().cost(TierRates()) is None


def test_report_excludes_unpriced_tiers_and_says_so(conn: sqlite3.Connection) -> None:
    priced = Pricing(tiers={"standard": STANDARD})
    out = render_cost_report([usage(), usage(tier="flex")], priced)
    assert "UNPRICED" in out and "EXCLUDES" in out


def test_report_states_the_settled_billing_unit_without_a_range(
    conn: sqlite3.Connection,
) -> None:
    """The low—high range straddled an open question that the dashboard closed."""
    out = render_cost_report([usage()], Pricing(tiers={"standard": STANDARD}))
    assert "ALL action types" in out
    assert "if billed per" not in out


def test_tier_usage_bills_every_action_type(conn: sqlite3.Connection) -> None:
    """open_page and find_in_page are billed exactly like search.

    Settled against the dashboard: $6,946.95 == 694,695 calls at $10/1K, and
    694,695 is the total across all three action types. Counting only `search`
    (592,710) understated the corpus fee by $1,019.85 — 17.2%.
    """
    conn.execute(
        "INSERT INTO runs (id,se10,run_id,run_key,service_tier,input_tokens,"
        "output_tokens,cache_read,src_file,src_line) "
        "VALUES (1,'A',0,'run_0','standard',100,50,10,'f',1)"
    )
    for action, queries in (
        ("search", '["a","b"]'),
        ("open_page", None),
        ("find_in_page", None),
        ("search", '["c"]'),
    ):
        conn.execute(
            "INSERT INTO search_calls (se10,run_pk,call_index,action_type,"
            "queries_json,raw_json,parse_conf,src_file,src_line) "
            "VALUES ('A',1,0,?,?,'{}','clean','f',1)",
            (action, queries),
        )
    conn.commit()
    (u,) = tier_usage(conn)
    assert u.n_search_calls == 4  # every action type, not just the 2 searches
    # open_page/find_in_page carry no `queries`, so they floor at one entry each.
    assert u.n_query_entries == 5  # 2 + 1 + 1 + 1


def test_unset_tier_is_costed_at_standard_but_labelled(conn: sqlite3.Connection) -> None:
    """Absent service_tier means the API default was used, so standard is the right
    inference — but it is an inference, and the report has to say so."""
    priced = Pricing(tiers={"standard": STANDARD})
    assert usage(tier="<unset>").cost(priced.for_tier(None)) is not None
    out = render_cost_report([usage(tier="<unset>")], priced)
    assert "costed at STANDARD rates" in out and "an inference" in out


def test_named_but_unconfigured_tier_borrows_nothing() -> None:
    """flex must not silently inherit standard's numbers."""
    priced = Pricing(tiers={"standard": STANDARD, "flex": TierRates()})
    assert usage(tier="flex").cost(priced.for_tier("flex")) is None


def test_call_with_no_run_is_still_billed(conn: sqlite3.Connection) -> None:
    """An inner join would drop it, understating cost — the wrong way to be wrong."""
    conn.execute(
        "INSERT INTO runs (id,se10,run_id,run_key,service_tier,input_tokens,"
        "output_tokens,cache_read,src_file,src_line) "
        "VALUES (1,'A',0,'run_0','standard',100,50,10,'f',1)"
    )
    for run_pk in (1, None):
        conn.execute(
            "INSERT INTO search_calls (se10,run_pk,call_index,action_type,"
            "queries_json,raw_json,parse_conf,src_file,src_line) "
            "VALUES ('A',?,0,'search','[\"a\"]','{}','clean','f',1)",
            (run_pk,),
        )
    conn.commit()
    by_tier = {u.tier: u for u in tier_usage(conn)}
    assert sum(u.n_search_calls for u in by_tier.values()) == 2
    assert by_tier["<unset>"].n_search_calls == 1  # orphan bucketed, not dropped


def test_empty_queries_array_still_counts_one_entry(conn: sqlite3.Connection) -> None:
    """Reported volume must never fall below the billed call count."""
    conn.execute(
        "INSERT INTO runs (id,se10,run_id,run_key,service_tier,input_tokens,"
        "output_tokens,cache_read,src_file,src_line) "
        "VALUES (1,'A',0,'run_0','standard',0,0,0,'f',1)"
    )
    conn.execute(
        "INSERT INTO search_calls (se10,run_pk,call_index,action_type,queries_json,"
        "raw_json,parse_conf,src_file,src_line) "
        "VALUES ('A',1,0,'search','[]','{}','clean','f',1)"
    )
    conn.commit()
    (u,) = tier_usage(conn)
    assert (u.n_search_calls, u.n_query_entries) == (1, 1)
