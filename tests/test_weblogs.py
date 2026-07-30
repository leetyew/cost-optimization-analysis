"""P1: web-search log parsing from `logs/jsonl/*.jsonl`.

This replaced a 460-line text-log parser and its 424-line test module. Almost
everything those tested — adjacency pairing, orphan classification, comma-split
repair, wrapped-line detection — tested *recovery* of facts this format states
outright. What remains worth testing is the shape tolerance: the parser must
never crash on a malformed record and never drop one silently, and the token
subset relationship must be checked rather than assumed, because the entire cost
model rests on it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coa.anomalies import AnomalyRecorder
from coa.config import Config
from coa.db import connect
from coa.weblogs import ingest_weblog, parse_call, reparse

CFG = Config.load(Path(__file__).parent.parent / "config.yaml")

SEARCH = {
    "id": "ws_0001",
    "status": "completed",
    "action_type": "search",
    "query": "acme widgets scam",
    "queries": ["acme widgets scam", "acme widgets fraud", "acme widgets reviews"],
}
USAGE = {
    "service_tier": "standard",
    "input_tokens": 5000,
    "output_tokens": 1200,
    "total_tokens": 6200,
    "cache_read": 4000,
    "reasoning": 300,
}


def record(se10: str = "1000000001", **run_over) -> dict:
    """One merchant with a single run; override any run-level key."""
    run = {"usage_metadata": dict(USAGE), "web_search_calls": [dict(SEARCH)]}
    run.update(run_over)
    return {se10: {"run_0": run}}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def run(conn: sqlite3.Connection, records: list) -> tuple[dict, AnomalyRecorder]:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    rec = AnomalyRecorder(conn, "weblogs")
    stats = ingest_weblog(conn, rec, "logs/jsonl/t.jsonl", lines, CFG)
    rec.flush()
    return stats, rec


def rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


# --- call extraction (pure, no DB) ----------------------------------------


def test_search_call_keeps_query_and_queries_apart() -> None:
    """The singular query is the billed unit; the plural list is sub-queries."""
    pc = parse_call(SEARCH)
    assert pc.action_type == "search"
    assert pc.query_raw == "acme widgets scam"
    assert len(pc.queries) == 3
    assert pc.parse_conf == "clean"


def test_open_page_carries_url_and_find_in_page_carries_details() -> None:
    op = parse_call(
        {
            "id": "o",
            "status": "completed",
            "action_type": "open_page",
            "url": "https://example.test/x",
        }
    )
    fp = parse_call(
        {
            "id": "f",
            "status": "completed",
            "action_type": "find_in_page",
            "details": "pattern: chargeback",
        }
    )
    assert (op.url, op.details) == ("https://example.test/x", None)
    assert (fp.details, fp.url) == ("pattern: chargeback", None)
    assert not op.notes and not fp.notes


def test_unknown_action_type_is_kept_verbatim_and_flagged() -> None:
    pc = parse_call({"action_type": "summarize_page", "status": "completed"})
    assert pc.action_type == "summarize_page"
    assert pc.parse_conf == "heuristic"
    assert any(c == "UNKNOWN_ACTION_TYPE" for c, _ in pc.notes)


def test_query_absent_from_queries_is_flagged() -> None:
    """The operator believes this never happens; verify rather than trust."""
    pc = parse_call(dict(SEARCH, queries=["something else"]))
    assert any(c == "QUERY_NOT_IN_QUERIES" for c, _ in pc.notes)


def test_incomplete_status_is_flagged() -> None:
    """A non-completed call raises a cost question: was it still billed?"""
    pc = parse_call(dict(SEARCH, status="failed"))
    assert any(c == "CALL_STATUS_NOT_COMPLETED" for c, _ in pc.notes)


def test_search_without_query_is_flagged_not_dropped() -> None:
    pc = parse_call({k: v for k, v in SEARCH.items() if k != "query"})
    assert pc.parse_conf == "heuristic"
    assert any(c == "CALL_FIELD_MISSING" for c, _ in pc.notes)


def test_non_dict_call_never_raises() -> None:
    pc = parse_call("not an object")
    assert pc.parse_conf == "failed"
    assert any(c == "CALL_NOT_AN_OBJECT" for c, _ in pc.notes)


def test_queries_of_wrong_type_is_flagged() -> None:
    pc = parse_call(dict(SEARCH, queries="a, b, c"))
    assert pc.parse_conf == "heuristic"
    assert any(c == "QUERIES_NOT_A_LIST" for c, _ in pc.notes)


# --- ingest ----------------------------------------------------------------


def test_run_and_call_are_attributed_exactly(conn: sqlite3.Connection) -> None:
    """se10 and run are structural keys, so there is no pairing and no orphan."""
    run(conn, [record()])
    (r,) = rows(conn, "runs")
    (c,) = rows(conn, "search_calls")
    assert (r["se10"], r["run_id"], r["run_key"]) == ("1000000001", 0, "run_0")
    assert (c["se10"], c["run_id"], c["run_pk"], c["call_index"]) == ("1000000001", 0, r["id"], 0)


def test_usage_metadata_is_stored_with_subset_fields(conn: sqlite3.Connection) -> None:
    run(conn, [record()])
    (r,) = rows(conn, "runs")
    assert (r["input_tokens"], r["output_tokens"], r["total_tokens"]) == (5000, 1200, 6200)
    assert (r["cache_read"], r["reasoning"]) == (4000, 300)
    assert r["service_tier"] == "standard"


def test_token_sum_mismatch_is_flagged(conn: sqlite3.Connection) -> None:
    """If cache_read/reasoning were addends rather than subsets, every cost
    figure would be wrong — so the arithmetic is checked, not assumed."""
    bad = dict(USAGE, total_tokens=USAGE["total_tokens"] + USAGE["reasoning"])
    _, rec = run(conn, [record(usage_metadata=bad)])
    assert rec.counts["TOKEN_SUM_MISMATCH"] == 1


def test_missing_usage_metadata_is_flagged_but_calls_still_land(
    conn: sqlite3.Connection,
) -> None:
    stats, rec = run(conn, [{"1": {"run_0": {"web_search_calls": [dict(SEARCH)]}}}])
    assert rec.counts["MISSING_USAGE_METADATA"] == 1
    assert stats["wl_calls"] == 1


def test_billed_query_is_exactly_one_per_call(conn: sqlite3.Connection) -> None:
    """Billing is per call, so archetype shares must sum to 100%."""
    run(conn, [record()])
    qs = rows(conn, "query_instances")
    assert sum(q["is_billed_query"] for q in qs) == 1
    assert len(qs) == 3  # billed + 2 distinct sub-queries
    billed = [q for q in qs if q["is_billed_query"]][0]
    assert billed["query_text"] == "acme widgets scam"


def test_call_index_records_position_in_run(conn: sqlite3.Connection) -> None:
    """Position comes from array order — there are no timestamps in this source."""
    calls = [dict(SEARCH, id=f"ws_{i}") for i in range(3)]
    run(conn, [record(web_search_calls=calls)])
    assert [c["call_index"] for c in rows(conn, "search_calls")] == [0, 1, 2]


def test_multiple_runs_per_merchant(conn: sqlite3.Connection) -> None:
    rec_ = {
        "1": {
            f"run_{i}": {"usage_metadata": dict(USAGE), "web_search_calls": [dict(SEARCH)]}
            for i in range(4)
        }
    }
    stats, _ = run(conn, [rec_])
    assert stats["wl_runs"] == 4
    assert [r["run_id"] for r in rows(conn, "runs")] == [0, 1, 2, 3]


def test_unparseable_run_key_is_flagged_not_silently_nulled(
    conn: sqlite3.Connection,
) -> None:
    _, rec = run(
        conn, [{"1": {"retry_final": {"usage_metadata": dict(USAGE), "web_search_calls": []}}}]
    )
    assert rec.counts["RUN_KEY_UNPARSED"] == 1
    assert rows(conn, "runs")[0]["run_id"] is None


def test_bad_json_is_recorded_and_the_file_continues(conn: sqlite3.Connection) -> None:
    stats, rec = run(conn, ['{"1": {"run_0": {trunc', record()])
    assert rec.counts["BAD_JSON_LINE"] == 1
    assert stats["wl_merchants"] == 1


def test_unexpected_shapes_never_crash(conn: sqlite3.Connection) -> None:
    stats, rec = run(
        conn,
        [
            json.dumps([1, 2, 3]),  # top level not an object
            json.dumps({"1": "not runs"}),  # runs not an object
            json.dumps({"1": {"run_0": "not a run"}}),  # run not an object
            json.dumps({"1": {"run_0": {"web_search_calls": "nope"}}}),
            record(),  # still ingests after all that
        ],
    )
    assert rec.counts["WEBLOG_SHAPE_UNEXPECTED"] == 4
    # Only 3: a record whose top level is not an object, or whose runs are not an
    # object, is rejected before the merchant counter — there is no merchant to
    # count. The two deeper failures do have one.
    assert stats["wl_merchants"] == 3


def test_raw_json_is_retained_for_reparse(conn: sqlite3.Connection) -> None:
    run(conn, [record()])
    stored = json.loads(rows(conn, "search_calls")[0]["raw_json"])
    assert stored == SEARCH


def test_reparse_reproduces_rows_without_source_files(conn: sqlite3.Connection) -> None:
    """The operator loop depends on this: a parser fix costs seconds."""
    run(conn, [record()])
    before = [dict(r) for r in rows(conn, "search_calls")]
    rec = AnomalyRecorder(conn, "reparse")
    stats = reparse(conn, rec, CFG)
    rec.flush()
    assert stats["reparsed"] == 1
    assert [dict(r) for r in rows(conn, "search_calls")] == before


def test_encoding_damage_is_reported(conn: sqlite3.Connection) -> None:
    damaged = json.dumps(record()).replace("scam", "sc�am")
    _, rec = run(conn, [damaged])
    assert rec.counts["ENCODING"] == 1


# --- golden corpus ---------------------------------------------------------


def test_golden_weblog_counts(corpus, golden: dict) -> None:
    expected = golden["weblogs"]
    assert corpus.totals["wl_merchants"] == expected["merchants"]
    assert corpus.totals["wl_runs"] == expected["runs"]
    assert corpus.totals["wl_calls"] == expected["calls"]


def test_golden_action_type_mix(corpus, golden: dict) -> None:
    expected = golden["weblogs"]
    for action in ("search", "open_page", "find_in_page", "summarize_page"):
        assert corpus.totals[f"wl_action_{action}"] == expected[f"action_{action}"], action


def test_golden_token_totals(corpus, golden: dict) -> None:
    expected = golden["weblogs"]
    row = corpus.conn.execute(
        "SELECT SUM(input_tokens) i, SUM(output_tokens) o, SUM(cache_read) c, "
        "SUM(reasoning) r FROM runs"
    ).fetchone()
    assert row["i"] == expected["input_tokens"]
    assert row["o"] == expected["output_tokens"]
    assert row["c"] == expected["cache_read"]
    assert row["r"] == expected["reasoning"]


def test_token_subsets_hold_across_the_corpus(corpus) -> None:
    """cache_read <= input and reasoning <= output, everywhere except the one
    deliberately planted mismatch."""
    bad = corpus.conn.execute(
        "SELECT COUNT(*) n FROM runs WHERE cache_read > input_tokens OR reasoning > output_tokens"
    ).fetchone()["n"]
    assert bad == 0


def test_every_billed_call_is_a_search(corpus) -> None:
    """open_page and find_in_page consume tokens but carry no per-call fee."""
    row = corpus.conn.execute(
        "SELECT COUNT(*) n FROM query_instances q JOIN search_calls c "
        "ON c.id = q.search_call_id WHERE q.is_billed_query = 1 AND c.action_type != 'search'"
    ).fetchone()
    assert row["n"] == 0


def test_every_call_row_carries_provenance(corpus) -> None:
    missing = corpus.conn.execute(
        "SELECT COUNT(*) n FROM search_calls WHERE src_file IS NULL OR src_line IS NULL"
    ).fetchone()["n"]
    assert missing == 0


def test_every_planted_weblog_hazard_fired(corpus) -> None:
    codes = {
        r["code"]
        for r in corpus.conn.execute("SELECT DISTINCT code FROM anomalies WHERE stage = 'weblogs'")
    }
    assert {
        "UNKNOWN_ACTION_TYPE",
        "QUERY_NOT_IN_QUERIES",
        "CALL_STATUS_NOT_COMPLETED",
        "CALL_FIELD_MISSING",
        "TOKEN_SUM_MISMATCH",
        "MISSING_USAGE_METADATA",
        "RUN_KEY_UNPARSED",
        "BAD_JSON_LINE",
        "ENCODING",
    } <= codes
