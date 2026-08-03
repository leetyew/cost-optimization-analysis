"""Input parser: `input/*.jsonl` -> `merchants` + `pii_terms` (PLAN.md §5).

Two jobs, and the second is the one that matters downstream:

1. Store the merchant record, keeping the **whole** JSON line in `raw_json`. The
   real schema has "many more keys" than anyone has enumerated, so naming a fixed
   list and discarding the rest would quietly lose fields nobody has asked for
   yet — and `coa reparse` could never get them back.
2. Explode the PII-bearing fields into normalized `pii_terms` variants. P3's
   templating replaces these with `<FIELD>` placeholders, so a value that fails to
   normalize here becomes a query that fails to template there, which shows up as
   unmasked text that cannot safely leave the air-gapped environment. Under-
   matching is therefore a privacy problem, not just a metrics one.

Like every parser here this takes `(src_name, lines)` and never a path; `cli.py`
owns all filesystem and zip walking.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator

from .anomalies import AnomalyRecorder, note_encoding_damage
from .config import Config

# PLAN.md §4 caps bad-JSON excerpts at 500 chars. A malformed line is often a
# truncated megabyte, and the operator only needs the head to recognize it.
BAD_JSON_EXCERPT = 500

# `merchants` column -> the record key that fills it.
#
# A mapping rather than a tuple because the two genuinely differ. The real input
# schema (operator-supplied 2026-08-04) shares exactly ONE spelling with the names
# this table uses: `website`. The other fourteen columns were NULL for all 19,349
# merchants, and nothing said so — `coa doctor`'s per-column fill rates exist now
# for precisely that reason.
#
# `Primary_Auhorized_Signer_Name` is spelled as the schema spells it. The missing
# 't' is theirs; correcting it here would silently blank the column. If the fill
# rate for `signer_name` comes back 0% while its neighbours are populated, the
# typo was a transcription slip and this is the line to fix.
MERCHANT_KEY_BY_COLUMN: dict[str, str] = {
    "opening_date": "merchant_opening_date",
    "city": "Seller_City_Name",
    "industry_tagged": "wwic_industry_tagged",
    "sub_category": "merchant_sub_category",
    "email": "Seller_Email_Address",
    "phone": "Business_Phone_No",
    "street": "Seller_Street_Address",
    "signer_name": "Primary_Auhorized_Signer_Name",
    "owner_name": "Significant_Owner_Name",
    "owner_city": "Significant_Owner_City_Name",
    "owner_postal": "Significant_Owner_Postal_Code",
    "owner_street": "Significant_Owner_Street_Address",
    "website": "website",
    "country": "sell_ctry_cd",
    "state": "state_name",
}

# pii_terms.field -> the record keys that feed it. Several keys collapse into one
# field on purpose: a query saying "123 Elm Avenue" should template to <STREET>
# whether that address came from the business or its owner.
#
# Real spellings, operator-supplied 2026-08-04. Only `se_toc_name` matched before,
# which is why the corpus produced exactly 1.000 term per merchant: every street,
# phone, email and owner name in 19,349 records would have gone unmasked.
#
# Three keys here have no `merchants` column and are new to this map. They are
# PII regardless of whether any column names them, and templating is the only
# thing standing between them and a report:
#   * `sell_dba_nm` / `sell_lgl_nm` — the doing-business-as and legal names, which
#     a search query is at least as likely to use as `se_toc_name`.
#   * `sell_pstl_cd` — the SELLER postal code. Only the owner's was mapped before.
#   * `Authorized_Signer_Physical_Address` — a person's home address.
PII_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("se_toc_name", "sell_dba_nm", "sell_lgl_nm"),
    "street": (
        "Seller_Street_Address",
        "Significant_Owner_Street_Address",
        "Authorized_Signer_Physical_Address",
    ),
    "city": ("Seller_City_Name", "Significant_Owner_City_Name"),
    "zip": ("Significant_Owner_Postal_Code", "sell_pstl_cd"),
    "phone": ("Business_Phone_No",),
    "email": ("Seller_Email_Address",),
    "owner": ("Significant_Owner_Name", "Primary_Auhorized_Signer_Name"),
}

# Shorter than this and a "PII term" starts matching ordinary English inside a
# query — a 2-letter state code would template half the corpus.
MIN_TERM_CHARS = 3

# Stripped from both ends only. Interior punctuation is content: removing it would
# turn "o'brien" into "obrien" and stop it matching the query text it came from.
_EDGE_PUNCT = " \t\r\n.,;:!?\"'`()[]{}<>|"


def _norm(value: object) -> str:
    """Casefold, collapse internal whitespace, strip edge punctuation."""
    return re.sub(r"\s+", " ", str(value)).strip(_EDGE_PUNCT).casefold()


def _variants(field: str, value: object) -> Iterator[str]:
    """Every form of one value a query might plausibly contain.

    Phones get a digits-only form because the corpus writes them three different
    ways and a query will use a fourth. Emails yield their local-part and domain
    separately because queries cite the domain far more often than the full
    address ("acmewidgets.example reviews").
    """
    base = _norm(value)
    if base:
        yield base
    if field == "phone":
        digits = re.sub(r"\D", "", str(value))
        if digits:
            yield digits
    elif field == "email" and "@" in base:
        local, _, domain = base.partition("@")
        yield local
        yield domain


def pii_terms_for(record: dict) -> set[tuple[str, str]]:
    """`(field, value_norm)` pairs for one merchant, deduped and length-filtered."""
    terms: set[tuple[str, str]] = set()
    for field, keys in PII_FIELDS.items():
        for key in keys:
            value = record.get(key)
            if value in (None, ""):
                continue
            for variant in _variants(field, value):
                if len(variant) >= MIN_TERM_CHARS:
                    terms.add((field, variant))
    return terms


def ingest_input(
    conn: sqlite3.Connection,
    rec: AnomalyRecorder,
    src_name: str,
    lines: Iterable[str],
    cfg: Config,
) -> Counter:
    """Parse one `input/*.jsonl` file into merchants + pii_terms."""
    stats: Counter = Counter()

    for i, raw in enumerate(lines):
        line_no = i + 1
        stats["lines"] += 1
        if not raw.strip():
            continue
        if note_encoding_damage(
            rec, src_name, line_no, raw, detail="undecodable byte in merchant record"
        ):
            stats["encoding_damaged"] += 1

        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            stats["in_bad_json"] += 1
            rec.record(
                "BAD_JSON_LINE",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail=f"input record did not parse as JSON: {exc}",
            )
            continue

        # se10 arrives as int or str depending on the exporter; TEXT everywhere is
        # the schema-wide convention so joins cannot silently miss.
        se10 = record.get("se10")
        if se10 in (None, ""):
            stats["in_no_se10"] += 1
            rec.record(
                "MERCHANT_NO_SE10",
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail="record has no se10; it cannot be joined to logs or outputs",
            )
            continue
        se10 = str(se10)

        prior = conn.execute(
            "SELECT src_file, src_line FROM merchants WHERE se10 = ?", (se10,)
        ).fetchone()
        if prior is not None:
            # Keep the first. Both provenances go in the anomaly so the operator can
            # diff them and tell us which exporter is authoritative.
            stats["in_dup_se10"] += 1
            rec.record(
                "DUP_INPUT_SE10",
                se10=se10,
                src_file=src_name,
                src_line=line_no,
                raw_excerpt=raw[:BAD_JSON_EXCERPT],
                detail=(
                    f"se10 {se10} already ingested from "
                    f"{prior['src_file']}:{prior['src_line']}; keeping the first, "
                    f"this copy is recorded but not stored"
                ),
            )
            continue

        columns = tuple(MERCHANT_KEY_BY_COLUMN)
        conn.execute(
            "INSERT INTO merchants (se10, raw_json, src_file, src_line, "
            + ", ".join(columns)
            + ") VALUES (?, ?, ?, ?, "
            + ", ".join("?" * len(columns))
            + ")",
            (
                se10,
                raw,
                src_name,
                line_no,
                *(_scalar(record.get(MERCHANT_KEY_BY_COLUMN[c])) for c in columns),
            ),
        )
        stats["in_records"] += 1
        # Per-column fill, so a key that stops matching shows up as a number rather
        # than as a table of NULLs nobody queries. Fourteen of these were empty for
        # the whole real corpus and the only trace was `pii terms 1.000 per
        # merchant`, two layers away.
        for column, key in MERCHANT_KEY_BY_COLUMN.items():
            if record.get(key) not in (None, ""):
                stats[f"in_col_{column}"] += 1

        terms = pii_terms_for(record)
        if not terms:
            # Not an error — but a merchant with no usable terms can never have its
            # queries templated, so it silently becomes unmaskable PII in P3.
            stats["in_no_pii_terms"] += 1
        conn.executemany(
            "INSERT OR IGNORE INTO pii_terms (se10, field, value_norm) VALUES (?, ?, ?)",
            [(se10, field, value) for field, value in sorted(terms)],
        )
        stats["in_pii_terms"] += len(terms)

    return stats


def _scalar(value: object) -> object:
    """Flatten a non-scalar field so sqlite3 can bind it.

    Named columns are declared TEXT, but nothing guarantees the exporter agrees —
    a list or dict where a string was expected would raise InterfaceError and take
    down the whole file. JSON-encoding it keeps the row, and `raw_json` still holds
    the original either way.
    """
    if value is None or isinstance(value, (str, int, float)):
        return value
    return json.dumps(value, sort_keys=True)
