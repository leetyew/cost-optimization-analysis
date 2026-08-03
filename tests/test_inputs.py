"""P2: merchant record storage and PII term explosion (PLAN.md §5).

Two layers, matching test_logs.py: unit tests over in-memory JSONL fragments,
then golden-count assertions over the generated corpus.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coa.anomalies import AnomalyRecorder
from coa.config import Config
from coa.db import connect
from coa.inputs import ingest_input, pii_terms_for

CFG = Config.load(Path(__file__).parent.parent / "config.yaml")

# The REAL input-schema spellings (operator-supplied 2026-08-04). Inventing tidy
# names here is what let every one of these tests pass against a corpus where only
# `se_toc_name` actually matched.
MERCHANT = {
    "se10": 1000000001,
    "se_toc_name": "Acme Widgets LLC",
    "sell_dba_nm": "Acme Widgets",
    "sell_lgl_nm": "Acme Widgets LLC, Limited",
    "Seller_City_Name": "Springfield",
    "Seller_Street_Address": "100 Widgets Street",
    "sell_pstl_cd": "70000",
    "Significant_Owner_Street_Address": "200 Elm Avenue",
    "Significant_Owner_City_Name": "Springfield",
    "Significant_Owner_Postal_Code": "60000",
    "Business_Phone_No": "(555) 200-0000",
    "Seller_Email_Address": "contact@acmewidgets.example",
    "Primary_Authorized_Signer_Name": "Pat Widgets",
    "Authorized_Signer_Physical_Address": "300 Cedar Court",
    "Significant_Owner_Name": "Casey Acme",
    "website": "https://acmewidgets.example",
    "unnamed_extra_key": {"nested": [1, 2]},
}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def run(conn: sqlite3.Connection, records: list) -> tuple[dict, AnomalyRecorder]:
    """Ingest in-memory JSONL; a raw string is passed through verbatim."""
    # ensure_ascii=False so a replacement char stays a character, as it is in a
    # UTF-8 export; the escaped spelling is covered by its own test.
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
    rec = AnomalyRecorder(conn, "inputs")
    stats = ingest_input(conn, rec, "input/t.jsonl", lines, CFG)
    rec.flush()
    return stats, rec


# --- pii term explosion (pure) --------------------------------------------


def test_phone_yields_a_digits_only_variant() -> None:
    """Queries write phones a fourth way; digits-only is what makes them match."""
    assert ("phone", "5552000000") in pii_terms_for(MERCHANT)


def test_email_yields_local_part_and_domain_separately() -> None:
    """Queries cite the bare domain far more often than a full address."""
    found = {v for f, v in pii_terms_for(MERCHANT) if f == "email"}
    assert {"contact@acmewidgets.example", "contact", "acmewidgets.example"} <= found


def test_two_keys_collapse_into_one_field() -> None:
    """street and owner_street both template to <STREET>."""
    streets = {v for f, v in pii_terms_for(MERCHANT) if f == "street"}
    assert {"100 widgets street", "200 elm avenue"} <= streets


def test_terms_are_casefolded_and_whitespace_collapsed() -> None:
    noisy = dict(MERCHANT, se_toc_name="  Acme   WIDGETS   LLC  ")
    assert ("name", "acme widgets llc") in pii_terms_for(noisy)


def test_short_values_are_dropped() -> None:
    """A two-character term would template half the corpus."""
    assert all(len(v) >= 3 for _, v in pii_terms_for(dict(MERCHANT, Seller_City_Name="IL")))


def test_missing_and_empty_fields_are_skipped() -> None:
    sparse = {
        "se10": "1",
        "se_toc_name": "Acme Widgets LLC",
        "Business_Phone_No": None,
        "Seller_Email_Address": "",
    }
    assert {f for f, _ in pii_terms_for(sparse)} == {"name"}


# --- record storage --------------------------------------------------------


def test_raw_json_retains_keys_the_schema_never_names(conn: sqlite3.Connection) -> None:
    """§5: extract the known keys, preserve the rest. reparse depends on it."""
    run(conn, [MERCHANT])
    raw = json.loads(conn.execute("SELECT raw_json FROM merchants").fetchone()["raw_json"])
    assert raw["unnamed_extra_key"] == {"nested": [1, 2]}


def test_se10_is_normalized_to_text(conn: sqlite3.Connection) -> None:
    """It arrives as int or str; TEXT everywhere or joins silently miss."""
    run(conn, [MERCHANT])
    assert conn.execute("SELECT se10 FROM merchants").fetchone()["se10"] == "1000000001"


def test_non_scalar_field_value_is_stored_not_fatal(conn: sqlite3.Connection) -> None:
    """A list where a string was expected must not take down the whole file."""
    stats, _ = run(conn, [dict(MERCHANT, Seller_City_Name=["Springfield", "Riverton"])])
    assert stats["in_records"] == 1
    assert json.loads(conn.execute("SELECT city FROM merchants").fetchone()["city"]) == [
        "Springfield",
        "Riverton",
    ]


def test_duplicate_se10_keeps_first_and_is_flagged(conn: sqlite3.Connection) -> None:
    stats, rec = run(conn, [MERCHANT, dict(MERCHANT, Seller_City_Name="Elsewhere")])
    assert stats["in_records"] == 1
    assert stats["in_dup_se10"] == 1
    assert rec.counts["DUP_INPUT_SE10"] == 1
    assert conn.execute("SELECT city FROM merchants").fetchone()["city"] == "Springfield"


def test_duplicate_anomaly_names_both_provenances(conn: sqlite3.Connection) -> None:
    """The operator needs both locations to tell us which exporter is authoritative."""
    run(conn, [MERCHANT, MERCHANT])
    detail = conn.execute("SELECT detail FROM anomalies WHERE code = 'DUP_INPUT_SE10'").fetchone()[
        "detail"
    ]
    assert "input/t.jsonl:1" in detail


def test_bad_json_is_recorded_and_the_file_continues(conn: sqlite3.Connection) -> None:
    stats, rec = run(conn, ['{"se10": "1", "trunc', MERCHANT])
    assert rec.counts["BAD_JSON_LINE"] == 1
    assert stats["in_records"] == 1  # the good record after it still lands


def test_record_without_se10_is_flagged_not_dropped_silently(conn: sqlite3.Connection) -> None:
    _, rec = run(conn, [{"se_toc_name": "Acme Widgets LLC"}])
    assert rec.counts["MERCHANT_NO_SE10"] == 1


def test_blank_lines_are_ignored(conn: sqlite3.Connection) -> None:
    stats, rec = run(conn, ["", "   ", MERCHANT])
    assert stats["in_records"] == 1
    assert not rec.counts


def test_replacement_char_is_reported(conn: sqlite3.Connection) -> None:
    """A bad byte inside a name still parses as JSON, so it must be flagged here."""
    _, rec = run(conn, [dict(MERCHANT, se_toc_name="Acme � Widgets")])
    assert rec.counts["ENCODING"] == 1


def test_ascii_escaped_replacement_char_is_also_reported(conn: sqlite3.Connection) -> None:
    """An exporter using ensure_ascii writes \\ufffd, which must not hide the damage."""
    _, rec = run(conn, [json.dumps(dict(MERCHANT, se_toc_name="Acme � Widgets"))])
    assert rec.counts["ENCODING"] == 1


def test_pii_terms_are_deduped_across_fields(conn: sqlite3.Connection) -> None:
    """city == owner_city is the common case; the primary key must absorb it."""
    run(conn, [MERCHANT])
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM pii_terms WHERE field = 'city' AND value_norm = 'springfield'"
    ).fetchone()
    assert rows["n"] == 1


# --- golden corpus ---------------------------------------------------------


def test_golden_input_counts(corpus, golden: dict) -> None:
    expected = golden["inputs"]
    n = corpus.conn.execute("SELECT COUNT(*) AS n FROM merchants").fetchone()["n"]
    assert n == expected["n_unique_se10"]
    assert corpus.totals["in_dup_se10"] == expected["dup_input_se10"]
    assert corpus.totals["lines"] >= expected["n_records"]


def test_every_merchant_has_pii_terms(corpus) -> None:
    """A merchant with no terms can never be templated, so it becomes unmasked PII."""
    orphaned = corpus.conn.execute(
        "SELECT COUNT(*) AS n FROM merchants m "
        "WHERE NOT EXISTS (SELECT 1 FROM pii_terms p WHERE p.se10 = m.se10)"
    ).fetchone()["n"]
    assert orphaned == 0


def test_every_merchant_row_carries_provenance(corpus) -> None:
    missing = corpus.conn.execute(
        "SELECT COUNT(*) AS n FROM merchants WHERE src_file IS NULL OR src_line IS NULL"
    ).fetchone()["n"]
    assert missing == 0
