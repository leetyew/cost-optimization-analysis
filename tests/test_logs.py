"""P1: log line classification, pairing state machine, action-field extraction.

Two layers of test:

* Unit tests over six-line in-memory lists. Possible because parsers take
  `(src_name, lines)` rather than a path, and it is how the odd branches get
  covered without threading a hazard through the whole fixture tree.
* Golden-count assertions over the full generated corpus, which prove the planted
  hazards land in the intended table or anomaly code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coa.anomalies import AnomalyRecorder
from coa.config import Config
from coa.db import connect, mark_ingested
from coa.logs import ingest_log, parse_action_line, reparse

from .fixtures import gen_fixtures

CFG = Config.load(Path(__file__).parent.parent / "config.yaml")

TS = "2026-07-30 10:00:01,000 | INFO | app.search | [1000000001] "
WS = TS + "Response tool type - web_search_call, id - ws_abc123"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def run(conn: sqlite3.Connection, lines: list[str]) -> tuple[dict, AnomalyRecorder]:
    """Ingest an in-memory log fragment; return (stats, recorder)."""
    rec = AnomalyRecorder(conn, "logs")
    stats = ingest_log(conn, rec, "t.log", lines, CFG)
    rec.flush()
    return stats, rec


def calls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM search_calls ORDER BY id").fetchall()


# --- field extraction (pure, no DB) ---------------------------------------


def test_search_splits_query_and_queries() -> None:
    pa = parse_action_line("action type - search, query - acme scam, queries - acme scam")
    assert pa.action_type == "search"
    assert pa.query_raw == "acme scam"
    assert pa.queries == ["acme scam"]
    assert pa.parse_conf == "clean"


def test_comma_inside_query_is_repaired_using_query_as_ground_truth() -> None:
    """The redundant singular `query` is the only honest signal for this repair."""
    pa = parse_action_line(
        "action type - search, query - Acme LLC, Springfield reviews, "
        "queries - Acme LLC, Springfield reviews , Acme scam"
    )
    assert pa.query_raw == "Acme LLC, Springfield reviews"
    # Naive comma splitting yields 3 items; the repair puts it back to 2.
    assert pa.queries == ["Acme LLC, Springfield reviews", "Acme scam"]
    assert pa.parse_conf == "heuristic"
    assert any(c == "COMMA_IN_QUERY" for c, _ in pa.notes)


def test_double_quotes_are_content_not_delimiters() -> None:
    """A quote-aware CSV parse would corrupt this field — plain splitting is right."""
    pa = parse_action_line(
        'action type - search, query - "acme" complaints, '
        'queries - "acme" complaints , "asdasd" asdasd'
    )
    assert pa.query_raw == '"acme" complaints'
    assert pa.queries == ['"acme" complaints', '"asdasd" asdasd']


def test_last_queries_marker_wins_and_is_flagged() -> None:
    pa = parse_action_line(
        "action type - search, query - acme , queries - refund, queries - acme , queries - refund"
    )
    assert pa.queries_raw == "refund"
    assert any(c == "MULTI_QUERIES_MARKER" for c, _ in pa.notes)


def test_query_absent_from_queries_is_flagged() -> None:
    """The operator believes this never happens; the detector proves it either way."""
    pa = parse_action_line("action type - search, query - acme liens, queries - something else")
    assert any(c == "QUERY_NOT_IN_QUERIES" for c, _ in pa.notes)
    assert pa.parse_conf == "heuristic"


def test_search_without_queries_field_keeps_the_single_query() -> None:
    pa = parse_action_line("action type - search, query - acme scam")
    assert pa.queries == ["acme scam"]
    assert pa.parse_conf == "clean"


def test_overlong_item_downgrades_confidence() -> None:
    long = "x" * (CFG.thresholds.max_sane_query_chars + 1)
    pa = parse_action_line(f"action type - search, query - acme, queries - acme , {long}")
    assert pa.parse_conf == "heuristic"


@pytest.mark.parametrize("spelling", ["find in page", "find_in_page"])
def test_both_find_in_page_spellings_normalize(spelling: str) -> None:
    pa = parse_action_line(f"action type - {spelling}, url - https://x.test, pattern - chargeback")
    assert pa.action_type == "find_in_page"
    assert pa.url == "https://x.test"
    assert pa.pattern == "chargeback"


def test_open_page_without_url_is_flagged_but_kept() -> None:
    """No comma, no fields — must still classify as an action, not as noise."""
    pa = parse_action_line("action type - open_page")
    assert pa.action_type == "open_page"
    assert pa.url is None
    assert any(c == "ACTION_FIELD_MISSING" for c, _ in pa.notes)


def test_unknown_action_type_is_kept_verbatim() -> None:
    pa = parse_action_line("action type - summarize_page, url - https://x.test")
    assert pa.action_type == "summarize_page"
    assert pa.url == "https://x.test"
    assert any(c == "UNKNOWN_ACTION_TYPE" for c, _ in pa.notes)


# --- pairing state machine -------------------------------------------------


def test_action_directly_after_web_search_call_is_strict(conn: sqlite3.Connection) -> None:
    run(conn, [WS, "action type - search, query - acme, queries - acme"])
    row = calls(conn)[0]
    assert row["pairing"] == "strict"
    assert row["se10"] == "1000000001"
    assert row["ws_id"] == "ws_abc123"


def test_action_with_no_preceding_web_search_call_is_orphan(conn: sqlite3.Connection) -> None:
    _, rec = run(conn, ["cache warm complete", "action type - search, query - a, queries - a"])
    row = calls(conn)[0]
    assert row["pairing"] == "orphan"
    assert row["se10"] is None
    assert rec.counts["ORPHAN_ACTION"] == 1


def test_interleaved_line_breaks_pairing(conn: sqlite3.Connection) -> None:
    """The async race the operator described: a concurrent worker cuts in."""
    _, rec = run(
        conn,
        [
            WS,
            TS + "heartbeat from concurrent worker",
            "action type - search, query - a, queries - a",
        ],
    )
    assert calls(conn)[0]["pairing"] == "orphan"
    assert rec.counts["ORPHAN_ACTION"] == 1


def test_second_consecutive_action_is_orphan(conn: sqlite3.Connection) -> None:
    """One web_search_call pairs with one action; the next must not inherit it."""
    run(
        conn,
        [
            WS,
            "action type - search, query - a, queries - a",
            "action type - search, query - b, queries - b",
        ],
    )
    got = calls(conn)
    assert [r["pairing"] for r in got] == ["strict", "orphan"]
    assert got[1]["se10"] is None


def test_orphan_anomaly_carries_surrounding_context(conn: sqlite3.Connection) -> None:
    """Context is what lets the operator judge whether a new pairing rule exists."""
    run(conn, ["noise a", "noise b", "action type - search, query - a, queries - a", "noise c"])
    ctx = conn.execute("SELECT context FROM anomalies WHERE code='ORPHAN_ACTION'").fetchone()[0]
    assert ">> action type - search" in ctx
    assert "noise b" in ctx and "noise c" in ctx


def test_non_noise_line_after_action_is_a_suspected_wrap(conn: sqlite3.Connection) -> None:
    _, rec = run(
        conn, [WS, "action type - search, query - a, queries - a", "filings record continued"]
    )
    row = calls(conn)[0]
    assert row["possible_wrap"] == 1
    assert row["raw_wrap_line"] == "filings record continued"
    # raw_action_line must stay pristine so reparse stays byte-stable.
    assert row["raw_action_line"] == "action type - search, query - a, queries - a"
    assert rec.counts["POSSIBLE_WRAPPED_ACTION"] == 1


def test_configured_noise_after_action_is_not_a_wrap(conn: sqlite3.Connection) -> None:
    """noise_patterns is the lever that keeps this heuristic from drowning in noise."""
    _, rec = run(conn, [WS, "action type - search, query - a, queries - a", "cache warm complete"])
    assert calls(conn)[0]["possible_wrap"] == 0
    assert "POSSIBLE_WRAPPED_ACTION" not in rec.counts


def test_replacement_char_is_recorded_and_line_still_parsed(conn: sqlite3.Connection) -> None:
    """A bad byte must degrade one character, never kill the file."""
    damaged = "action type - search, query - ac�me, queries - ac�me"
    _, rec = run(conn, [WS, damaged])
    assert rec.counts["ENCODING"] == 1
    assert len(calls(conn)) == 1, "damaged line must still produce a row"


def test_pairing_delta_is_reported(conn: sqlite3.Connection) -> None:
    """web_search_call lines with no adjacent action are the pairing-loss KPI."""
    stats, _ = run(conn, [WS, TS + "unrelated", WS, "action type - search, query - a, queries - a"])
    assert stats["web_search_call"] == 2
    assert stats["strict"] == 1
    assert stats["pairing_delta"] == 1


def test_billed_query_is_exactly_one_per_search_call(conn: sqlite3.Connection) -> None:
    """Billing is per call, so archetype cost shares must sum to 100%."""
    run(conn, [WS, "action type - search, query - a, queries - a , b , c"])
    rows = conn.execute("SELECT query_text, is_billed_query FROM query_instances").fetchall()
    assert sum(r["is_billed_query"] for r in rows) == 1
    assert {r["query_text"] for r in rows} == {"a", "b", "c"}


# --- golden corpus ---------------------------------------------------------


@pytest.fixture(scope="module")
def golden() -> dict:
    return gen_fixtures.write_fixtures()


@pytest.fixture
def ingested(tmp_path: Path, golden: dict) -> sqlite3.Connection:
    """Full corpus ingested, exactly as `coa ingest` would."""
    from coa.cli import iter_source_files

    c = connect(tmp_path / "corpus.sqlite")
    for src, lines in iter_source_files(gen_fixtures.DATA_ROOT, "logs", ".log"):
        rec = AnomalyRecorder(c, "logs")
        stats = ingest_log(c, rec, src, lines, CFG)
        rec.flush()
        mark_ingested(c, src, "log", stats["lines"])
    c.commit()
    yield c
    c.close()


def test_golden_strict_and_orphan_counts(ingested, golden: dict) -> None:
    n = lambda sql: ingested.execute(sql).fetchone()[0]  # noqa: E731
    assert n("SELECT COUNT(*) FROM search_calls") == golden["logs"]["total_action_lines"]
    assert (
        n("SELECT COUNT(*) FROM search_calls WHERE pairing='strict'")
        == (golden["logs"]["total_strict"])
    )
    assert (
        n("SELECT COUNT(*) FROM search_calls WHERE pairing='orphan'")
        == (golden["logs"]["total_orphan"])
    )


def test_clean_log_has_no_orphans(ingested) -> None:
    """The baseline file must stay fully paired or pairing loss is unmeasurable."""
    n = ingested.execute(
        "SELECT COUNT(*) FROM search_calls WHERE pairing='orphan' AND src_file LIKE '%clean%'"
    ).fetchone()[0]
    assert n == 0


def test_every_planted_hazard_produced_its_anomaly(ingested) -> None:
    got = {r[0] for r in ingested.execute("SELECT DISTINCT code FROM anomalies")}
    expected_from_logs = {
        "ENCODING",
        "ORPHAN_ACTION",
        "UNKNOWN_ACTION_TYPE",
        "POSSIBLE_WRAPPED_ACTION",
        "ACTION_FIELD_MISSING",
        "COMMA_IN_QUERY",
        "MULTI_QUERIES_MARKER",
        "QUERY_NOT_IN_QUERIES",
    }
    assert expected_from_logs <= got, f"never fired: {expected_from_logs - got}"


def test_no_action_row_is_left_unclassified(ingested) -> None:
    """PLAN.md §3 invariant: every action row is strict-paired or orphan-flagged."""
    assert (
        ingested.execute(
            "SELECT COUNT(*) FROM search_calls WHERE pairing NOT IN ('strict','orphan')"
        ).fetchone()[0]
        == 0
    )
    assert (
        ingested.execute(
            "SELECT COUNT(*) FROM search_calls WHERE pairing='strict' AND se10 IS NULL"
        ).fetchone()[0]
        == 0
    )


def test_parse_conf_distribution_has_no_failures(ingested) -> None:
    rows = dict(
        ingested.execute("SELECT parse_conf, COUNT(*) FROM search_calls GROUP BY parse_conf")
    )
    assert rows.get("failed", 0) == 0
    assert rows["clean"] > rows.get("heuristic", 0), "corpus should be mostly clean"


def test_reparse_reproduces_rows_without_source_files(ingested) -> None:
    """The operator loop depends on this: a parser fix must not cost a re-ingest."""
    cols = "id,action_type,query_raw,queries_raw,queries_json,url,pattern,parse_conf"
    before = ingested.execute(f"SELECT {cols} FROM search_calls ORDER BY id").fetchall()
    qi_before = ingested.execute(
        "SELECT search_call_id,query_text,is_billed_query FROM query_instances "
        "ORDER BY search_call_id,query_text"
    ).fetchall()

    rec = AnomalyRecorder(ingested, "reparse")
    stats = reparse(ingested, rec, CFG)
    rec.flush()

    after = ingested.execute(f"SELECT {cols} FROM search_calls ORDER BY id").fetchall()
    qi_after = ingested.execute(
        "SELECT search_call_id,query_text,is_billed_query FROM query_instances "
        "ORDER BY search_call_id,query_text"
    ).fetchall()

    assert stats["reparsed"] == len(before)
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert [tuple(r) for r in qi_after] == [tuple(r) for r in qi_before]


def test_reparse_leaves_pairing_untouched(ingested) -> None:
    """Pairing is positional; its inputs are gone at reparse time, so it must not move."""
    before = ingested.execute(
        "SELECT id, pairing, se10, ts, ws_id FROM search_calls ORDER BY id"
    ).fetchall()
    rec = AnomalyRecorder(ingested, "reparse")
    reparse(ingested, rec, CFG)
    after = ingested.execute(
        "SELECT id, pairing, se10, ts, ws_id FROM search_calls ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_action_prefix_that_fails_the_pattern_is_never_silently_dropped(
    conn: sqlite3.Connection,
) -> None:
    """Core invariant: never crash on malformed input, never silently drop it.

    A hyphenated action type does not match ACTION_RE. Before this was handled the
    line fell through to OTHER and vanished into the noise tally with no anomaly.
    """
    _, rec = run(conn, [WS, "action type - find-in-page, url - https://x.test"])
    rows = calls(conn)
    assert len(rows) == 1, "the line must still be stored"
    assert rows[0]["parse_conf"] == "failed"
    assert rows[0]["raw_action_line"] == "action type - find-in-page, url - https://x.test"
    assert rec.counts["ACTION_UNPARSEABLE"] == 1


def test_degenerate_action_line_with_no_fields_is_kept(conn: sqlite3.Connection) -> None:
    """`action type - ` alone still announces an action; strip() must not lose it."""
    _, rec = run(conn, [WS, "action type - "])
    assert len(calls(conn)) == 1
    assert rec.counts["ACTION_UNPARSEABLE"] == 1
