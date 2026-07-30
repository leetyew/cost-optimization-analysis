"""Fixtures shared across the test modules.

`golden` regenerates the fixture tree and returns the counts of what was planted.
It lives here rather than being redefined per module because those counts are the
project's single source of truth about the corpus, and two loaders is two places
for them to drift. Session-scoped, so the tree is written once per run.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

from coa import anomalies as anom
from coa.cli import SOURCE_KINDS, iter_source_files
from coa.config import Config

from .fixtures import gen_fixtures

REPO_ROOT = Path(__file__).resolve().parents[1]
CFG = Config.load(REPO_ROOT / "config.yaml")


class Corpus(NamedTuple):
    """An ingested fixture corpus plus the merged stats from every parser."""

    conn: sqlite3.Connection
    totals: Counter


@pytest.fixture(scope="session")
def golden() -> dict:
    """Regenerate the fixture tree once; return its golden counts."""
    return gen_fixtures.write_fixtures()


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory, golden: dict) -> Corpus:
    """The whole tree ingested in the real `cmd_ingest` order.

    Ingest order is load-bearing, not incidental: input/ must land before output/
    so that INPUT_OUTPUT_FIELD_CONFLICT has merchant rows to compare against.
    Driving it from SOURCE_KINDS keeps this honest — if the CLI's order changes,
    the tests follow rather than silently testing a different pipeline.
    """
    from coa.db import connect

    conn = connect(tmp_path_factory.mktemp("corpus") / "coa.sqlite")
    totals: Counter = Counter()
    for kind, subdir, suffix, parser in SOURCE_KINDS:
        for src_name, lines in iter_source_files(gen_fixtures.DATA_ROOT, subdir, suffix):
            rec = anom.AnomalyRecorder(conn, f"{kind}s", CFG.anomalies.max_excerpt_chars)
            totals.update(parser(conn, rec, src_name, lines, CFG))
            rec.flush()
    conn.commit()
    yield Corpus(conn, totals)
    conn.close()
