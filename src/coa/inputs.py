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
from collections.abc import Iterator, Sequence

from .anomalies import AnomalyRecorder, note_encoding_damage
from .config import Config

# PLAN.md §4 caps bad-JSON excerpts at 500 chars. A malformed line is often a
# truncated megabyte, and the operator only needs the head to recognize it.
BAD_JSON_EXCERPT = 500

# Columns `merchants` names explicitly. Everything else survives in raw_json.
MERCHANT_KEYS: tuple[str, ...] = (
    "opening_date",
    "city",
    "industry_tagged",
    "sub_category",
    "email",
    "phone",
    "street",
    "signer_name",
    "owner_name",
    "owner_city",
    "owner_postal",
    "owner_street",
    "website",
    "country",
    "state",
)

# pii_terms.field -> the record keys that feed it. Several keys collapse into one
# field on purpose: a query saying "123 Elm Avenue" should template to <STREET>
# whether that address came from the business or its owner.
PII_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("se_toc_name",),
    "street": ("street", "owner_street"),
    "city": ("city", "owner_city"),
    "zip": ("owner_postal",),
    "phone": ("phone",),
    "email": ("email",),
    "owner": ("signer_name", "owner_name"),
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
    lines: Sequence[str],
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

        conn.execute(
            "INSERT INTO merchants (se10, raw_json, src_file, src_line, "
            + ", ".join(MERCHANT_KEYS)
            + ") VALUES (?, ?, ?, ?, "
            + ", ".join("?" * len(MERCHANT_KEYS))
            + ")",
            (se10, raw, src_name, line_no, *(_scalar(record.get(k)) for k in MERCHANT_KEYS)),
        )
        stats["in_records"] += 1

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
