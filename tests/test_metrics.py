"""Cache economics: the two levers, and the floor that decides whether either works."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coa.db import connect
from coa.metrics import CACHE_MIN_TOKENS, cache_picture, render_cache_report


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
