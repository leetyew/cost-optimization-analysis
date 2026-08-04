"""Templating and exact archetypes (P3).

Two layers of test, and the split is deliberate. `build_template` is pure over a
query and a term list, so the rules that decide whether a template groups at all
-- longest match first, casefolding, the zero-placeholder case -- are asserted
directly on it rather than inferred from corpus totals.

The corpus tests then assert the one property that matters at scale and that no
hand-built case can prove: **no template contains any of its merchant's live PII
terms**. That is the privacy invariant the whole module exists to uphold, and it
is checked over every row rather than over a chosen example.
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from coa import anomalies as anom
from coa.config import Config
from coa.db import connect
from coa.health import health_report
from coa.inputs import _norm
from coa.normalize import (
    build_template,
    export_head,
    load_archetype_groups,
    masking_diagnostic,
    template_queries,
    templating_picture,
)

from .conftest import CFG

# ---------------------------------------------------------------------------
# build_template — the rules that decide whether a template groups
# ---------------------------------------------------------------------------


def test_longest_term_wins_when_one_contains_another() -> None:
    """A street value contains the city value; masking the short one first mangles it.

    This is the single most consequential ordering rule in the module: the wrong
    order yields `200 <CITY> avenue`, which groups with nothing, instead of
    `<STREET>`, which groups with every other merchant's street query.
    """
    terms = [("city", "elm"), ("street", "200 elm avenue")]
    template, n = build_template("200 Elm Avenue reviews", terms)
    assert template == "<STREET> reviews"
    assert n == 1

    # Order in the input must not matter — the function sorts, not the caller.
    assert build_template("200 Elm Avenue reviews", list(reversed(terms)))[0] == "<STREET> reviews"


def test_generic_query_keeps_no_placeholder() -> None:
    """A query with no merchant PII survives verbatim and is flagged as unmasked."""
    terms = [("name", "acme widgets llc"), ("city", "springfield")]
    template, n = build_template("BBB complaints database", terms)
    assert template == "bbb complaints database"
    assert n == 0


def test_phone_digits_only_variant_matches_a_reformatted_query() -> None:
    """`pii_terms` carries a digits-only phone form precisely for this case.

    The corpus writes phone numbers three ways and a query will use a fourth, so
    the digits-only variant is what makes the match survive reformatting.
    """
    terms = [("phone", "5552000000")]
    template, n = build_template("5552000000 business listing", terms)
    assert template == "<PHONE> business listing"
    assert n == 1


def test_placeholder_text_is_immune_to_later_terms() -> None:
    """An emitted placeholder cannot be re-matched by a term that spells it.

    Terms are casefolded by `_norm` and placeholders are uppercase, which is what
    makes repeated replacement safe without tracking consumed spans. A term
    literally spelling `name` must not touch the `<NAME>` already written.
    """
    terms = [("name", "acme"), ("city", "name")]
    template, n = build_template("Acme reviews", terms)
    assert template == "<NAME> reviews"
    assert n == 1


def test_repeated_occurrences_are_all_counted() -> None:
    """n_placeholders counts substitutions, not distinct terms."""
    template, n = build_template("Acme reviews and Acme scam", [("name", "acme")])
    assert template == "<NAME> reviews and <NAME> scam"
    assert n == 2


def test_no_terms_leaves_the_query_normalized_but_unmasked() -> None:
    """A merchant with no pii_terms yields a verbatim template, never a crash."""
    template, n = build_template("  Acme   Widgets  scam ", [])
    assert template == "acme widgets scam"
    assert n == 0


# ---------------------------------------------------------------------------
# Hand-built DB — cases the fixture corpus structurally cannot produce
# ---------------------------------------------------------------------------


@pytest.fixture
def bare_db(tmp_path) -> sqlite3.Connection:
    """An empty DB with one search call, for the paths the corpus cannot reach."""
    conn = connect(tmp_path / "bare.sqlite")
    conn.execute(
        "INSERT INTO search_calls (id, se10, call_index, action_type, raw_json, parse_conf, "
        "src_file, src_line) VALUES (1, '9999999999', 0, 'search', '{}', 'clean', 'x', 1)"
    )
    yield conn
    conn.close()


def _query_row(conn: sqlite3.Connection, se10: str, text: str) -> None:
    conn.execute(
        "INSERT INTO query_instances (search_call_id, se10, query_text, is_billed_query) "
        "VALUES (1, ?, ?, 1)",
        (se10, text),
    )


def test_merchant_without_pii_terms_raises_one_anomaly_per_merchant(
    bare_db: sqlite3.Connection, tmp_path
) -> None:
    """The regression detector for the bug that left every merchant at 1.000 terms.

    Every fixture weblog record is built from a merchant that HAS terms
    (`gen_fixtures.build_weblogs` iterates `merchants`), so this path is
    unreachable from the corpus fixture and needs a DB built by hand.

    One anomaly per merchant, not per query: the defect is a property of the
    merchant, and the per-query form would be tens of thousands of rows.
    """
    for text in ("acme scam", "acme reviews", "acme fraud"):
        _query_row(bare_db, "9999999999", text)
    rec = anom.AnomalyRecorder(bare_db, "analyze")

    stats = template_queries(bare_db, rec, Config(archetype_groups=tmp_path / "absent.csv"))
    rec.flush()

    assert stats["tmpl_no_terms_merchants"] == 1
    assert stats["tmpl_no_terms_rows"] == 3
    rows = bare_db.execute(
        "SELECT detail FROM anomalies WHERE code = 'TEMPLATE_NO_PII_TERMS'"
    ).fetchall()
    assert len(rows) == 1
    # Distinguishes "absent from input/" from "present but yielded nothing" — the
    # two have different fixes, and the anomaly has to name which it saw.
    assert "no merchants row at all" in rows[0]["detail"]
    # All three rows are still stored and templated; nothing is dropped.
    assert (
        bare_db.execute(
            "SELECT COUNT(*) FROM query_instances WHERE template IS NOT NULL"
        ).fetchone()[0]
        == 3
    )


def test_unmatched_archetype_group_row_is_reported(bare_db: sqlite3.Connection, tmp_path) -> None:
    """A hand-written grouping that matches nothing is silently doing nothing."""
    _query_row(bare_db, "9999999999", "bbb complaints database")
    groups = tmp_path / "groups.csv"
    groups.write_text(
        "template,archetype\n"
        "bbb complaints database,bbb-lookup\n"
        "a template that no longer exists,stale-group\n"
    )
    rec = anom.AnomalyRecorder(bare_db, "analyze")

    stats = template_queries(bare_db, rec, Config(archetype_groups=groups))
    rec.flush()

    assert stats["tmpl_group_rows"] == 2
    assert stats["tmpl_group_unmatched"] == 1
    detail = bare_db.execute(
        "SELECT detail FROM anomalies WHERE code = 'ARCHETYPE_GROUP_UNMATCHED'"
    ).fetchone()["detail"]
    assert "stale-group" in detail
    # The row that DID match is applied.
    assert (
        bare_db.execute("SELECT archetype FROM query_instances").fetchone()["archetype"]
        == "bbb-lookup"
    )


def test_missing_archetype_csv_is_normal(tmp_path) -> None:
    """The operator builds the CSV from the export, so absent is the first-run state."""
    assert load_archetype_groups(tmp_path / "nope.csv") == {}


# ---------------------------------------------------------------------------
# Corpus — the properties that only hold at scale
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def templated(corpus, tmp_path_factory) -> sqlite3.Connection:
    """The fixture corpus with templating applied. Module-scoped: it is a full pass."""
    rec = anom.AnomalyRecorder(corpus.conn, "analyze")
    template_queries(corpus.conn, rec, CFG)
    rec.flush()
    corpus.conn.commit()
    return corpus.conn


def test_every_query_row_is_templated(templated: sqlite3.Connection) -> None:
    """No row is skipped — an untemplated row would vanish from the archetypes view."""
    n_rows, n_templated = templated.execute(
        "SELECT COUNT(*), COUNT(template) FROM query_instances"
    ).fetchone()
    assert n_rows > 0
    assert n_templated == n_rows


def test_no_template_contains_a_live_pii_term(templated: sqlite3.Connection) -> None:
    """The privacy invariant, over every row rather than a chosen example.

    A template still containing one of its own merchant's `pii_terms` values means
    masking silently failed for that row — and templates are what the head export
    writes to a file. Checked against the terms as stored, which is the same space
    `build_template` matches in.
    """
    leaked = [
        (row["se10"], row["field"])
        for row in templated.execute(
            "SELECT q.se10 AS se10, p.field AS field FROM query_instances q "
            "JOIN pii_terms p ON p.se10 = q.se10 "
            "WHERE q.template LIKE '%' || p.value_norm || '%'"
        )
    ]
    assert leaked == []


def test_generic_query_is_the_unmasked_case(templated: sqlite3.Connection) -> None:
    """The generator plants a PII-free query precisely to exercise this bucket."""
    row = templated.execute(
        "SELECT COUNT(*) AS n, MIN(n_placeholders) AS lo, MAX(n_placeholders) AS hi "
        "FROM query_instances WHERE template = ?",
        (_norm("BBB complaints database"),),
    ).fetchone()
    assert row["n"] > 0
    assert (row["lo"], row["hi"]) == (0, 0)


def test_masked_queries_dominate_the_corpus(templated: sqlite3.Connection) -> None:
    """Most fixture queries carry merchant PII, so most must mask.

    A pass that templated everything to zero placeholders would satisfy every
    other assertion here while masking nothing at all.
    """
    p = templating_picture(templated)
    assert p.n_unmasked_billed < p.n_billed
    assert p.unmasked_share < 0.5


def test_every_pii_field_fires_at_least_once(templated: sqlite3.Connection) -> None:
    """A field that never fires is the silent-mismatch shape, one layer downstream.

    The fixture builds queries from name, city, phone and email, and the merchant
    name feeds `name` while owner/signer feed `owner` — so a zero anywhere among
    the fields the queries actually use means templating is not reaching it.
    """
    fired = {field for field, n in templating_picture(templated).by_field if n}
    assert {"name", "city", "phone", "email"} <= fired


def test_archetypes_view_accounts_for_every_billed_call(templated: sqlite3.Connection) -> None:
    """The view is the deliverable's shape: billed calls must partition across it.

    `n_billed_calls` summed over archetypes has to equal the billed-query count
    exactly, or archetype cost shares do not sum to 100%.
    """
    rows = templated.execute("SELECT n_billed_calls, is_unmapped FROM archetypes").fetchall()
    assert rows
    billed = templated.execute(
        "SELECT COUNT(*) FROM query_instances WHERE is_billed_query = 1"
    ).fetchone()[0]
    assert sum(r["n_billed_calls"] for r in rows) == billed
    # No archetype_groups.csv in the fixture config, so everything is unmapped.
    assert all(r["is_unmapped"] for r in rows)


def test_templating_is_idempotent(templated: sqlite3.Connection, tmp_path) -> None:
    """Re-running must not double anomalies or change any count.

    `coa analyze` is meant to be re-run after every edit to archetype_groups.csv,
    so a second pass that drifted would make the workflow unusable.
    """
    before = templated.execute(
        "SELECT COUNT(*), COUNT(template), COALESCE(SUM(n_placeholders), 0) FROM query_instances"
    ).fetchone()
    rec = anom.AnomalyRecorder(templated, "analyze")
    template_queries(templated, rec, CFG)
    rec.flush()
    after = templated.execute(
        "SELECT COUNT(*), COUNT(template), COALESCE(SUM(n_placeholders), 0) FROM query_instances"
    ).fetchone()
    assert tuple(before) == tuple(after)


# ---------------------------------------------------------------------------
# The export gate — the boundary where templates leave the environment
# ---------------------------------------------------------------------------


def test_export_withholds_unmasked_templates_by_default(
    templated: sqlite3.Connection, tmp_path
) -> None:
    """Zero-placeholder templates are verbatim query text and must not leave by default."""
    dest = tmp_path / "nested" / "head.csv"
    written, withheld = export_head(templated, dest, CFG)

    assert withheld > 0  # the fixture plants a PII-free query
    rows = list(csv.DictReader(dest.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == written
    assert rows
    assert _norm("BBB complaints database") not in {r["template"] for r in rows}
    # Directly editable into archetype_groups.csv: the column exists and is blank.
    assert all(r["archetype"] == "" for r in rows)


def test_export_includes_unmasked_only_when_asked(templated: sqlite3.Connection, tmp_path) -> None:
    """The flag is the whole gate, so it has to actually change what is written."""
    dest = tmp_path / "head_all.csv"
    written, withheld = export_head(templated, dest, CFG, include_unmasked=True)

    assert withheld == 0
    templates = {r["template"] for r in csv.DictReader(dest.read_text().splitlines())}
    assert _norm("BBB complaints database") in templates
    assert written == len(templates)


def test_export_covers_only_billed_queries(templated: sqlite3.Connection, tmp_path) -> None:
    """Sub-queries are not billed and cannot be recommended against.

    The other team controls the 48 questions, not the sub-queries the model
    generates from them, so an export ranked by sub-query volume would rank by
    something nobody can act on.
    """
    dest = tmp_path / "head.csv"
    export_head(templated, dest, CFG, include_unmasked=True)
    rows = list(csv.DictReader(dest.read_text().splitlines()))

    for row in rows:
        expected = templated.execute(
            "SELECT COUNT(*) FROM query_instances WHERE template = ? AND is_billed_query = 1",
            (row["template"],),
        ).fetchone()[0]
        assert int(row["n_billed_calls"]) == expected


# ---------------------------------------------------------------------------
# `coa doctor` — the section is only useful if it distinguishes its two zeros
# ---------------------------------------------------------------------------


def test_doctor_distinguishes_not_run_from_nothing_matched(bare_db: sqlite3.Connection) -> None:
    """An untemplated DB and a DB that masked nothing both look like zeros downstream.

    `coa reparse` clears templates, so "has not run" is a state a working corpus
    lands in routinely. Reporting it as a plain 0 would send the operator hunting
    for a masking bug that does not exist.
    """
    _query_row(bare_db, "9999999999", "acme scam")
    out = health_report(bare_db)
    assert "TEMPLATING" in out
    assert "templating has not run" in out


def test_doctor_reports_templating_coverage_and_unmasked_count(
    templated: sqlite3.Connection,
) -> None:
    """The two figures the operator needs: how much templated, and how much is unmasked."""
    p = templating_picture(templated)
    out = health_report(templated)

    assert f"{p.n_templated:,} of" in out
    assert f"{p.n_unmasked_billed:,} of {p.n_billed:,} billed" in out
    # The privacy line has to say WHY an unmasked row matters, not just count it.
    assert "template IS the query text" in out
    # Placeholders by field is the P3-side "terms by field" — a field stuck at 0
    # while its pii_terms bucket is populated means templating is not reaching it.
    assert "placeholders" in out
    assert len(out.splitlines()) < 90, "doctor output must stay one screen"


# ---------------------------------------------------------------------------
# Masking diagnostic — generic query vs PII we failed to match
# ---------------------------------------------------------------------------


def test_near_miss_fires_on_a_partial_name(bare_db: sqlite3.Connection, tmp_path) -> None:
    """The detector must FIRE, not just return 0 on a corpus that has no misses.

    `acme widgets llc` is stored whole, so a query saying `Acme Widgets reviews`
    matches no term and comes back unmasked — yet it plainly contains the
    merchant. That is a pii_terms variant to add, and it is the failure the real
    corpus's 89%-singleton head is suspected of.
    """
    bare_db.executemany(
        "INSERT INTO pii_terms (se10, field, value_norm) VALUES ('9999999999', ?, ?)",
        [("name", "acme widgets llc"), ("city", "springfield")],
    )
    _query_row(bare_db, "9999999999", "Acme Widgets reviews")  # near miss
    _query_row(bare_db, "9999999999", "BBB complaints database")  # genuinely generic
    rec = anom.AnomalyRecorder(bare_db, "analyze")
    template_queries(bare_db, rec, Config(archetype_groups=tmp_path / "absent.csv"))

    d = masking_diagnostic(bare_db)
    assert d.n_unmasked == 2
    assert d.n_near_miss == 1, "the partial-name query must be flagged"
    assert d.near_miss_by_field == [("name", 1)]
    assert d.near_miss_rate == 0.5


def test_generic_queries_collapse_and_missed_pii_does_not(corpus, tmp_path) -> None:
    """The fingerprint the whole diagnostic rests on, measured on the corpus.

    Fixture masking is complete, so its unmasked queries are the planted generic
    ones — they must collapse to very few templates. If this ratio ever
    approached 1.0 here it would mean the fixture had developed a masking gap.
    """
    rec = anom.AnomalyRecorder(corpus.conn, "analyze")
    template_queries(corpus.conn, rec, CFG)
    d = masking_diagnostic(corpus.conn)

    by_kind = {k: (q, t) for k, q, t in d.by_kind}
    q_unmasked, t_unmasked = by_kind["unmasked"]
    assert q_unmasked / t_unmasked > 5, "generic queries must collapse"
    assert d.n_near_miss == 0, "fixture masking is complete, so nothing should near-miss"
