"""P0 acceptance tests: schema, anomaly framework, fixture determinism.

These are deliberately about the *scaffolding contracts* the later phases depend
on — the schema's cascade behaviour, the anomaly recorder's truncation, and the
generator's determinism. Parser behaviour is tested in P1/P2.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from coa.anomalies import AnomalyRecorder, render_samples, render_summary, samples, summary
from coa.cli import iter_source_files
from coa.config import Config
from coa.db import already_ingested, connect, forget_file, mark_ingested

from .fixtures import gen_fixtures

FIXTURE_ROOT = Path(gen_fixtures.FIXTURE_ROOT)
GOLDEN = FIXTURE_ROOT / "golden.json"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


# --- config ---------------------------------------------------------------


def test_config_loads_repo_yaml() -> None:
    cfg = Config.load(Path(__file__).parent.parent / "config.yaml")
    assert cfg.thresholds.head_templates_export == 200
    assert cfg.anomalies.max_excerpt_chars == 2000


def test_partly_filled_pricing_is_still_unverified() -> None:
    """One tier priced is not all tiers priced.

    `standard` carries operator-supplied rates; `flex` and `priority` are
    deliberately null rather than derived from published ratios. The banner must
    keep firing while any tier is unpriced, or a run on an unpriced tier would be
    silently valued at someone else's rate.
    """
    cfg = Config.load(Path(__file__).parent.parent / "config.yaml")
    assert not cfg.pricing.is_verified
    missing = cfg.pricing.missing()
    assert not any(m.startswith("standard.") for m in missing)
    assert "flex.input_per_mtok" in missing
    assert "priority.output_per_mtok" in missing


def test_standard_rates_load_with_the_cached_discount() -> None:
    """Cached input is 1/10th of input — a cheap guard against a fat-fingered rate."""
    rates = Config.load(Path(__file__).parent.parent / "config.yaml").pricing.for_tier("standard")
    assert rates.input_per_mtok == 2.50
    assert rates.cached_input_per_mtok == 0.25
    assert rates.output_per_mtok == 15.00
    assert rates.fee_per_1k_search_calls == 10.00
    assert rates.cached_input_per_mtok == rates.input_per_mtok / 10


def test_pricing_is_per_service_tier() -> None:
    """Flex bills near batch rates and priority ~2x standard — a single set of
    constants could be wrong by ~4x, so the tier is part of the key."""
    cfg = Config.load(Path(__file__).parent.parent / "config.yaml")
    assert {"standard", "flex", "priority"} <= set(cfg.pricing.tiers)
    assert cfg.pricing.for_tier("flex").output_per_mtok is None
    # An unknown tier must not raise; it falls back to empty rates.
    assert cfg.pricing.for_tier("nonesuch").missing()


def test_config_defaults_without_file(tmp_path: Path) -> None:
    """A missing config file is usable, not fatal."""
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.db == Path("coa.sqlite")


# --- schema ---------------------------------------------------------------


def test_schema_creates_all_tables(conn: sqlite3.Connection) -> None:
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    for expected in (
        "merchants",
        "pii_terms",
        "runs",
        "search_calls",
        "query_instances",
        "output_records",
        "questions",
        "answers",
        "citations",
        "votes",
        "anomalies",
        "labels",
        "ingested_files",
        "archetypes",
    ):
        assert expected in names, f"missing {expected}"


def test_every_foreign_key_child_column_is_indexed(conn: sqlite3.Connection) -> None:
    """SQLite does not index the child side of a foreign key for you.

    Without an index, ON DELETE CASCADE scans the whole child table once per
    deleted parent row, which is quadratic and makes `coa ingest --force`
    unusable at corpus scale. Asserted structurally so a future table cannot
    reintroduce it unnoticed.
    """
    tables = [
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    unindexed = []
    for table in tables:
        indexed = set()
        for idx in conn.execute(f"PRAGMA index_list({table})"):
            indexed |= {r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})")}
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if fk["from"] not in indexed:
                unindexed.append(f"{table}.{fk['from']}")
    assert not unindexed, f"foreign-key child columns without an index: {unindexed}"


def test_cascade_delete_uses_an_index_not_a_scan(conn: sqlite3.Connection) -> None:
    plan = [
        r["detail"]
        for r in conn.execute("EXPLAIN QUERY PLAN DELETE FROM answers WHERE output_id = 1")
    ]
    assert any("USING" in step and "INDEX" in step for step in plan), plan


def test_connect_is_idempotent(tmp_path: Path) -> None:
    """Re-opening an existing DB must not fail or wipe it."""
    p = tmp_path / "x.sqlite"
    c1 = connect(p)
    c1.execute("INSERT INTO questions (qnum, text) VALUES (1, 'q')")
    c1.commit()
    c1.close()
    c2 = connect(p)
    assert c2.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"] == 1
    c2.close()


def test_ingest_bookkeeping_round_trip(conn: sqlite3.Connection) -> None:
    assert not already_ingested(conn, "logs.zip!logs/a.log")
    mark_ingested(conn, "logs.zip!logs/a.log", "log", 42)
    assert already_ingested(conn, "logs.zip!logs/a.log")


def test_forget_file_cascades_to_children(conn: sqlite3.Connection) -> None:
    """`--force` must leave no orphaned children behind.

    This is the test that would catch foreign_keys=OFF regressing silently: the
    cascade is the only thing deleting answers/citations/votes.
    """
    conn.execute(
        "INSERT INTO output_records (id, se10, n_runs, raw_json_hash, src_file, src_line) "
        "VALUES (1, '1000000001', 2, 'h', 'output/a.jsonl', 1)"
    )
    conn.execute(
        "INSERT INTO answers (se10, output_id, run_id, qnum, answer_text) "
        "VALUES ('1000000001', 1, 0, 1, 'Yes')"
    )
    conn.execute(
        "INSERT INTO citations (se10, output_id, run_id, qnum, url, source) "
        "VALUES ('1000000001', 1, 0, 1, 'https://x.test', 'citation_evidence')"
    )
    conn.execute(
        "INSERT INTO votes (se10, output_id, qnum, voted_majority) "
        "VALUES ('1000000001', 1, 1, 'Yes')"
    )
    conn.execute(
        "INSERT INTO runs (id, se10, run_id, run_key, src_file, src_line) "
        "VALUES (3, '1000000001', 0, 'run_0', 'logs/jsonl/a.jsonl', 1)"
    )
    conn.execute(
        "INSERT INTO search_calls (id, se10, run_pk, call_index, action_type, raw_json, "
        "parse_conf, src_file, src_line) "
        "VALUES (7, '1000000001', 3, 0, 'search', '{}', 'clean', 'logs/jsonl/a.jsonl', 1)"
    )
    conn.execute(
        "INSERT INTO query_instances (search_call_id, se10, query_text) "
        "VALUES (7, '1000000001', 'q')"
    )
    conn.commit()

    forget_file(conn, "output/a.jsonl")
    forget_file(conn, "logs/jsonl/a.jsonl")
    conn.commit()

    for table in ("answers", "citations", "votes", "query_instances"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert n == 0, f"{table} left {n} orphaned row(s) after forget_file"


def test_forget_file_unmarks_files_whose_rows_it_cascades_away(
    conn: sqlite3.Connection,
) -> None:
    """A duplicate run makes one file's calls hang off another file's run row, so
    forgetting the owner cascades them away. Leaving the other file marked
    ingested would be permanent silent loss — no re-run would restore it."""
    conn.execute(
        "INSERT INTO runs (id, se10, run_id, run_key, src_file, src_line) "
        "VALUES (1, 'A', 0, 'run_0', 'f1.jsonl', 1)"
    )
    for src in ("f1.jsonl", "f2.jsonl"):
        conn.execute(
            "INSERT INTO search_calls (se10, run_pk, call_index, action_type, raw_json, "
            "parse_conf, src_file, src_line) VALUES ('A', 1, 0, 'search', '{}', 'clean', ?, 1)",
            (src,),
        )
        mark_ingested(conn, src, "weblog", 1)
    conn.commit()

    forget_file(conn, "f1.jsonl")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS n FROM search_calls").fetchone()["n"] == 0
    assert not already_ingested(conn, "f1.jsonl")
    assert not already_ingested(conn, "f2.jsonl"), (
        "f2's rows were cascaded away, so it must be re-ingestable"
    )


def test_archetype_view_rolls_up_billed_calls(conn: sqlite3.Connection) -> None:
    """Cost share must come from the billed (singular `query`) rows only."""
    conn.execute(
        "INSERT INTO search_calls (id, se10, call_index, action_type, raw_json, "
        "parse_conf, src_file, src_line) VALUES "
        "(1, '1', 0, 'search', '{}', 'clean', 'f', 1)"
    )
    conn.executemany(
        "INSERT INTO query_instances (search_call_id, se10, query_text, template, "
        "n_placeholders, archetype, is_billed_query) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "1", "Acme scam", "<NAME> scam", 1, "reputation", 1),
            (1, "1", "Acme reviews", "<NAME> reviews", 1, "reputation", 0),
        ],
    )
    conn.commit()
    row = conn.execute("SELECT * FROM archetypes WHERE archetype_id = 'reputation'").fetchone()
    assert row["n_queries"] == 2
    assert row["n_billed_calls"] == 1, "only the singular query is billable"


# --- anomalies ------------------------------------------------------------


def test_recorder_buffers_then_flushes(conn: sqlite3.Connection) -> None:
    rec = AnomalyRecorder(conn, "logs")
    rec.record(
        "CALL_FIELD_MISSING",
        src_file="logs/jsonl/a.jsonl",
        src_line=5,
        raw_excerpt='{"action_type": "search"}',
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM anomalies").fetchone()["n"] == 0
    rec.flush()
    assert conn.execute("SELECT COUNT(*) AS n FROM anomalies").fetchone()["n"] == 1
    assert rec.counts["CALL_FIELD_MISSING"] == 1


def test_recorder_flush_is_repeatable(conn: sqlite3.Connection) -> None:
    rec = AnomalyRecorder(conn, "logs")
    rec.record("X", detail="d")
    rec.flush()
    rec.flush()
    assert conn.execute("SELECT COUNT(*) AS n FROM anomalies").fetchone()["n"] == 1


def test_excerpt_truncation_is_marked(conn: sqlite3.Connection) -> None:
    """A silent truncation would send someone chasing a phantom parse bug."""
    rec = AnomalyRecorder(conn, "logs", max_excerpt=50)
    rec.record("BIG", raw_excerpt="x" * 500)
    rec.flush()
    stored = conn.execute("SELECT raw_excerpt FROM anomalies").fetchone()["raw_excerpt"]
    assert "truncated" in stored and "500 chars total" in stored


def test_render_summary_flags_empty_as_suspicious(conn: sqlite3.Connection) -> None:
    out = render_summary(summary(conn))
    assert "warning sign" in out


def test_render_samples_is_fenced_and_capped(conn: sqlite3.Connection) -> None:
    rec = AnomalyRecorder(conn, "logs")
    for i in range(20):
        rec.record(
            "CALL_FIELD_MISSING", src_file=f"f{i % 3}.jsonl", src_line=i, context=["a", ">> b", "c"]
        )
    rec.flush()
    rows = samples(conn, "CALL_FIELD_MISSING", 4)
    out = render_samples("CALL_FIELD_MISSING", rows, 20)
    assert out.count("```") == 2, "output must be a single fenced block, ready to paste"
    assert "showing 4 of 20" in out
    assert len(rows) == 4


def test_samples_spread_across_files(conn: sqlite3.Connection) -> None:
    """One sample per file beats N from the same file for diagnosing a surprise."""
    rec = AnomalyRecorder(conn, "logs")
    for f in ("a.log", "b.log", "c.log"):
        for i in range(5):
            rec.record("CODE", src_file=f, src_line=i)
    rec.flush()
    got = samples(conn, "CODE", 3)
    assert len({r["src_file"] for r in got}) == 3


# --- fixtures -------------------------------------------------------------


def test_fixture_tree_exists(golden: dict) -> None:
    root = gen_fixtures.DATA_ROOT
    assert len(list((root / "logs" / "jsonl").glob("*.jsonl"))) == 2
    assert len(list((root / "input").glob("*.jsonl"))) == 2
    assert len(list((root / "output").glob("*.jsonl"))) == 2


def test_generator_is_deterministic(golden: dict) -> None:
    """Same seed, same bytes — otherwise golden counts mean nothing.

    Regenerating in a subprocess rather than in-process is deliberate: the zip
    member timestamps are the classic source of drift here, and an in-process
    rerun can mask it by landing in the same wall-clock second.
    """
    before = gen_fixtures.fingerprint()
    subprocess.run(
        [sys.executable, str(Path(gen_fixtures.__file__))], check=True, capture_output=True
    )
    assert gen_fixtures.fingerprint() == before
    assert golden["fingerprint"] == before


def test_golden_file_matches_generator(golden: dict) -> None:
    assert json.loads(GOLDEN.read_text()) == golden


def test_fixtures_plant_every_hazard(golden: dict) -> None:
    """Every hazard the parsers defend against is present and counted."""
    weblogs = golden["weblogs"]
    for hazard in (
        "planted_unknown_action",
        "planted_query_not_in_queries",
        "planted_status_not_completed",
        "planted_field_missing",
        "planted_token_sum_mismatch",
        "planted_no_usage_metadata",
        "planted_run_key_unparsed",
    ):
        assert weblogs.get(hazard, 0) >= 1, f"hazard {hazard} not planted"
    assert weblogs["bad_json_lines"] == 1
    assert golden["outputs"]["bad_json_lines"] == 1
    assert golden["inputs"]["dup_input_se10"] == 1


def test_fixture_action_mix_matches_the_real_corpus_shape(golden: dict) -> None:
    """~85/13/2 search/open_page/find_in_page. Position-in-run analysis depends on
    the mix being realistic, not uniform."""
    w = golden["weblogs"]
    assert w["action_search"] / w["calls"] > 0.7
    assert w["action_open_page"] > 0 and w["action_find_in_page"] > 0


def test_weblog_has_bad_byte_and_survives_decode() -> None:
    """The non-UTF8 byte must produce a replacement char, not an exception."""
    raw = (gen_fixtures.DATA_ROOT / "logs" / "jsonl" / "calls_001.jsonl").read_bytes()
    assert b"\xff" in raw
    assert "�" in raw.decode("utf-8", errors="replace")


def test_output_records_parse_as_json_except_planted_bad_line() -> None:
    """Exactly one malformed line, and it is malformed for the expected reason."""
    bad = 0
    for p in sorted((gen_fixtures.DATA_ROOT / "output").glob("*.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                bad += 1
    assert bad == 1


def test_zip_members_are_matched_by_subdir_but_root_members_still_load(
    tmp_path: Path,
) -> None:
    """Two source kinds share `.jsonl`, so nested members must match their subdir —
    but a member at the archive root has no subdir to disagree with, and dropping
    it would ingest nothing while still exiting 0."""
    with zipfile.ZipFile(tmp_path / "logs.zip", "w") as z:
        z.writestr("flat.log", "a\n")
        z.writestr("logs/nested.log", "b\n")
        z.writestr("output/other.log", "c\n")
    found = {name for name, _ in iter_source_files(tmp_path, "logs", ".log")}
    assert found == {"logs.zip!flat.log", "logs.zip!logs/nested.log"}


def test_source_lines_are_streamed_not_materialized(tmp_path: Path) -> None:
    """A member must never be read whole.

    `output/*.jsonl` extrapolates to 1-2 GB on a real corpus and materializing
    costs several times that in peak memory. Resumable ingest does not save us:
    it skips *completed* files, so a member too large to materialize fails the
    same way on every retry. Asserted as "not a Sequence", which is the property
    a `.read().splitlines()` regression would break.
    """
    with zipfile.ZipFile(tmp_path / "d.zip", "w") as z:
        z.writestr("output/a.jsonl", "one\ntwo\nthree\n")
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "b.jsonl").write_text("four\r\nfive\n")

    seen = {}
    for name, lines in iter_source_files(tmp_path, "output", ".jsonl"):
        assert not isinstance(lines, Sequence), f"{name} was materialized"
        seen[name] = list(lines)

    assert seen["d.zip!output/a.jsonl"] == ["one", "two", "three"]
    # \r\n must arrive stripped, exactly as splitlines() used to deliver it.
    assert seen["output/b.jsonl"] == ["four", "five"]


def test_question_set_is_48_and_extractable() -> None:
    """The extraction regex from PLAN.md §4 must recover all 48 questions."""
    qs = gen_fixtures.build_questions({"owner_postal": "60000"})
    prompt = gen_fixtures.user_prompt(qs)
    found = re.findall(r"Q(\d+)\.\s*(.*?)(?=\nQ\d+\.|\Z)", prompt, re.S)
    assert len(found) == 48
    assert [int(n) for n, _ in found] == list(range(1, 49))
