"""PII templating and exact query archetypes (PLAN.md §6.1, §6.2 layer 1).

This module exists to bridge the gap that blocks every savings figure. P4 found
questions that are close to inert (Q22 answers nothing in 91.6% of runs), but
42.9% of cost is the per-call search fee and **nothing in the corpus attributes a
search call to a question** -- `web_search_call.action.sources` was never enabled.
So "Q22 is inert" cannot yet become "Q22 costs N searches".

Templating is the bridge, and it works because a templated query is *readable*:
`"<NAME> scam"` or `"what type of building is at <ZIP>"` maps onto one of the 48
questions by inspection. That mapping is human judgement -- it is the one
heuristic in the project worth keeping, and invariant 5 requires it be named as
such wherever it is reported. Everything this module itself produces is an exact
count over stored rows.

Two things shape the implementation more than anything else:

* **Matching is per merchant, via se10.** A global term list would be 236,776
  terms scanned against 592,710 billed queries. Each merchant has ~12 terms, and
  a query can only contain its own merchant's PII, so the join makes the work
  linear instead of quadratic.
* **`n_placeholders == 0` is a privacy control, not a statistic.** It means
  nothing matched, so `template` is the verbatim query text and may hold unmasked
  merchant names and addresses. It gates the head export; see `export_head`.

Unlike the parsers this takes a connection, not `(src_name, lines)`. It is an
analysis pass over what ingest already stored, so there is no file to stream.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .anomalies import AnomalyRecorder
from .config import Config
from .inputs import PII_FIELDS, _norm

# Rows updated per executemany. One merchant's queries (~100 at corpus rates)
# already batch naturally; this only caps a pathological outlier.
_UPDATE_CHUNK = 5000

# How many templates the head-coverage figure is measured over. Distinct from
# `Thresholds.head_templates_export` on purpose: that one sizes a file the
# operator hand-edits, this one sizes a statistic, and tying them together would
# make the reported coverage move whenever the export size was retuned.
HEAD_FOR_COVERAGE = 100


def placeholder_for(field: str) -> str:
    """`<NAME>`, `<STREET>`, ... for a `pii_terms.field` value.

    Placeholder text is UPPERCASE while every term is casefolded by `_norm`, and
    that asymmetry is load-bearing rather than cosmetic: it means a later term can
    never match the letters of a placeholder already written into the string, so
    masking can be applied term by term without tracking consumed spans.
    """
    return f"<{field.upper()}>"


def build_template(query_text: str, terms: Sequence[tuple[str, str]]) -> tuple[str, int]:
    """Mask one merchant's PII out of one query. Returns `(template, n_substitutions)`.

    `terms` are `(field, value_norm)` pairs for the merchant that issued this
    query -- never a global list, and never another merchant's.

    The query is normalized with `inputs._norm`, the same function that produced
    `pii_terms.value_norm`, so both sides land in one comparison space. Reusing it
    rather than writing a second normalizer is deliberate: two definitions of
    "the same text" is precisely how the four-way evidence classification went
    wrong earlier in this project.

    **Longest term first.** A street value usually contains the city value
    ("200 elm avenue" contains "elm"), so masking the short one first yields
    `200 <CITY> avenue` -- a template that groups with nothing. Sorting happens
    here rather than in the caller because it is a correctness property of this
    function, and a caller that forgot would produce quietly worse archetypes
    rather than an error.
    """
    out = _norm(query_text)
    n = 0
    for field, value in sorted(terms, key=lambda t: (-len(t[1]), t[1])):
        hits = out.count(value)
        if hits:
            out = out.replace(value, placeholder_for(field))
            n += hits
    return out, n


def load_archetype_groups(path: Path) -> dict[str, str]:
    """Read the hand-maintained `template -> archetype` map.

    A missing file is the normal first-run state, not an error: the operator
    builds it *from* the head export this module produces. Returns `{}` so the
    report can say so and templating still runs.
    """
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {
            row["template"]: row["archetype"]
            for row in csv.DictReader(fh)
            if row.get("template") and row.get("archetype")
        }


def template_queries(conn: sqlite3.Connection, rec: AnomalyRecorder, cfg: Config) -> Counter:
    """Populate `template` / `n_placeholders` / `archetype` for every query row.

    Walks the corpus merchant by merchant so each query is only ever compared
    against its own merchant's terms. One merchant's queries are buffered before
    their UPDATEs are issued -- ~100 rows at corpus rates -- which keeps the pass
    streaming and, just as importantly, avoids updating `query_instances` while a
    cursor is still stepping over it.

    Re-runnable by design: it UPDATEs in place, so a changed `archetype_groups.csv`
    only needs another `coa analyze`.
    """
    stats: Counter = Counter()
    groups = load_archetype_groups(cfg.archetype_groups)
    stats["tmpl_group_rows"] = len(groups)
    matched_groups: set[str] = set()

    # Materialized: ~19k short strings, and the alternative is holding a cursor
    # open on the table these UPDATEs write to.
    se10s = [r[0] for r in conn.execute("SELECT DISTINCT se10 FROM query_instances")]

    for se10 in se10s:
        terms = [
            (r["field"], r["value_norm"])
            for r in conn.execute("SELECT field, value_norm FROM pii_terms WHERE se10 = ?", (se10,))
        ]
        rows = conn.execute(
            "SELECT id, query_text FROM query_instances WHERE se10 IS ?", (se10,)
        ).fetchall()
        if not terms:
            _note_unmaskable(conn, rec, se10, len(rows), stats)

        updates = []
        for row in rows:
            template, n = build_template(row["query_text"], terms)
            archetype = groups.get(template)
            if archetype is not None:
                matched_groups.add(template)
            updates.append((template, n, archetype, row["id"]))
            stats["tmpl_rows"] += 1
            if n:
                stats["tmpl_masked"] += 1
            else:
                stats["tmpl_unmasked"] += 1

        for i in range(0, len(updates), _UPDATE_CHUNK):
            conn.executemany(
                "UPDATE query_instances SET template = ?, n_placeholders = ?, archetype = ? "
                "WHERE id = ?",
                updates[i : i + _UPDATE_CHUNK],
            )

    # A hand-written grouping that matches nothing is silently doing nothing --
    # the operator's most likely mistake is a template that has since changed.
    for template in sorted(set(groups) - matched_groups):
        stats["tmpl_group_unmatched"] += 1
        rec.record(
            "ARCHETYPE_GROUP_UNMATCHED",
            detail=(
                f"archetype_groups.csv maps a template to {groups[template]!r}, but no query "
                f"in the corpus templates to it; that row groups nothing"
            ),
        )
    return stats


def _note_unmaskable(
    conn: sqlite3.Connection, rec: AnomalyRecorder, se10: str, n_rows: int, stats: Counter
) -> None:
    """Record a merchant whose queries cannot be masked at all.

    One anomaly per merchant, never per query: this is a property of the merchant,
    and the per-query version would be tens of thousands of rows whose `detail`
    would have to carry raw query text to say anything at all.

    Expected to be silent on the current corpus -- log merchants are a subset of
    input merchants. Its job is to fire the moment a `pii_terms` regression makes
    a slice of the corpus unmaskable, which is exactly the failure that left every
    merchant with 1.000 terms and went unnoticed through a full real ingest.
    """
    stats["tmpl_no_terms_merchants"] += 1
    stats["tmpl_no_terms_rows"] += n_rows
    known = conn.execute("SELECT 1 FROM merchants WHERE se10 = ?", (se10,)).fetchone()
    reason = (
        "it has a merchants row, but no pii_terms were extracted from it"
        if known
        else "it has no merchants row at all (present in logs, absent from input/)"
    )
    rec.record(
        "TEMPLATE_NO_PII_TERMS",
        se10=se10,
        detail=(
            f"se10 {se10} issued {n_rows} query row(s) that cannot be masked: {reason}. "
            f"Their templates are verbatim query text and may contain merchant PII."
        ),
    )


@dataclass(frozen=True)
class TemplatingPicture:
    """What templating produced, as counts. No template text -- see `export_head`."""

    n_rows: int
    n_templated: int
    n_billed: int
    n_unmasked: int
    n_unmasked_billed: int
    n_distinct: int
    n_singleton: int
    head_covered: int
    head_size: int
    by_field: list[tuple[str, int]]
    n_mapped_billed: int
    n_groups: int

    @property
    def has_run(self) -> bool:
        return bool(self.n_templated)

    def _share(self, num: int, den: int) -> float | None:
        return num / den if den else None

    @property
    def head_coverage(self) -> float | None:
        return self._share(self.head_covered, self.n_billed)

    @property
    def head_is_whole(self) -> bool:
        """Whether the head IS every template, which makes its coverage vacuous.

        With fewer distinct templates than `HEAD_FOR_COVERAGE` the top-N covers
        100% by definition, and reading that as "exact grouping works well" would
        be circular. Small corpora (the fixtures) sit here; the real one does not.
        """
        return self.n_distinct <= HEAD_FOR_COVERAGE

    @property
    def unmasked_share(self) -> float | None:
        return self._share(self.n_unmasked_billed, self.n_billed)

    @property
    def mapped_share(self) -> float | None:
        return self._share(self.n_mapped_billed, self.n_billed)


def templating_picture(conn: sqlite3.Connection) -> TemplatingPicture:
    """Measure the templating pass, including the layer-2 trigger.

    `head_covered` is the layer-2 decision: if a small number of exact templates
    already covers most billed queries, fuzzy and semantic clustering are buying
    little and the stack rule's bar for adding `rapidfuzz` is not met. Reported
    either way rather than only when it looks bad -- the measurement is what
    licenses the decision, in both directions.
    """
    totals = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(template) AS templated, "
        "COALESCE(SUM(is_billed_query), 0) AS billed, "
        "COALESCE(SUM(n_placeholders = 0), 0) AS unmasked, "
        "COALESCE(SUM(n_placeholders = 0 AND is_billed_query = 1), 0) AS unmasked_billed, "
        "COALESCE(SUM(archetype IS NOT NULL AND is_billed_query = 1), 0) AS mapped_billed "
        "FROM query_instances"
    ).fetchone()

    # Billed only: archetype share is a cost statement, and sub-queries are not
    # billed. Counting them here would inflate every share by ~3x.
    head = conn.execute(
        "SELECT COALESCE(SUM(n), 0) AS n_singleton FROM ("
        "  SELECT template, COUNT(*) AS n FROM query_instances "
        "  WHERE template IS NOT NULL AND is_billed_query = 1 "
        "  GROUP BY template HAVING n = 1)"
    ).fetchone()
    n_distinct = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM query_instances "
        "WHERE template IS NOT NULL AND is_billed_query = 1 GROUP BY template)"
    ).fetchone()[0]
    covered = conn.execute(
        "SELECT COALESCE(SUM(n), 0) FROM ("
        "  SELECT COUNT(*) AS n FROM query_instances "
        "  WHERE template IS NOT NULL AND is_billed_query = 1 "
        "  GROUP BY template ORDER BY n DESC LIMIT ?)",
        (HEAD_FOR_COVERAGE,),
    ).fetchone()[0]

    return TemplatingPicture(
        n_rows=totals["n"],
        n_templated=totals["templated"],
        n_billed=totals["billed"],
        n_unmasked=totals["unmasked"],
        n_unmasked_billed=totals["unmasked_billed"],
        n_distinct=n_distinct,
        n_singleton=head["n_singleton"],
        head_covered=covered,
        head_size=min(HEAD_FOR_COVERAGE, n_distinct),
        by_field=_placeholders_by_field(conn),
        n_mapped_billed=totals["mapped_billed"],
        n_groups=conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM query_instances "
            "WHERE archetype IS NOT NULL GROUP BY archetype)"
        ).fetchone()[0],
    )


def _placeholders_by_field(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Billed queries containing each placeholder, worst first.

    The P3-side analogue of `coa doctor`'s `terms by field`. A field that never
    fires while its `pii_terms` bucket is populated means templating is not
    reaching it -- the same silent-mismatch shape that left 14 merchant columns
    NULL, one layer further downstream.
    """
    counts = [
        (
            field,
            conn.execute(
                "SELECT COUNT(*) FROM query_instances "
                "WHERE is_billed_query = 1 AND template LIKE ?",
                (f"%{placeholder_for(field)}%",),
            ).fetchone()[0],
        )
        for field in PII_FIELDS
    ]
    return sorted(counts, key=lambda kv: (-kv[1], kv[0]))


def export_head(
    conn: sqlite3.Connection, path: Path, cfg: Config, *, include_unmasked: bool = False
) -> tuple[int, int]:
    """Write the head-template CSV the operator hand-groups. Returns `(written, withheld)`.

    **The gate is the point of this function.** A row with `n_placeholders == 0`
    had nothing masked, so its `template` is the query verbatim -- merchant names,
    addresses, phone numbers. Those are withheld unless the caller explicitly asks
    for them, and the count of what was withheld is returned so the caller can say
    so rather than presenting a filtered list as the whole head.

    The gate is necessary, not sufficient: a query whose city masked but whose
    owner name never reached `pii_terms` has `n_placeholders == 1` and still
    carries a person's name. That caveat belongs in front of the operator every
    time, which is why `render_report` prints it rather than this docstring alone.

    Billed queries only. Sub-queries are not billed and cannot be recommended
    against -- the other team controls the questions, not the sub-queries the
    model generates from them.
    """
    withheld = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM query_instances "
        "WHERE template IS NOT NULL AND is_billed_query = 1 AND n_placeholders = 0 "
        "GROUP BY template)"
    ).fetchone()[0]

    gate = "" if include_unmasked else "AND n_placeholders > 0"
    rows = conn.execute(
        f"""
        SELECT template,
               COUNT(*)                 AS n_billed_calls,
               COUNT(DISTINCT se10)     AS n_merchants,
               MAX(archetype)           AS archetype
        FROM query_instances
        WHERE template IS NOT NULL AND is_billed_query = 1 {gate}
        GROUP BY template ORDER BY n_billed_calls DESC, template LIMIT ?
        """,
        (cfg.thresholds.head_templates_export,),
    ).fetchall()

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("template", "n_billed_calls", "n_merchants", "archetype"))
        for r in rows:
            writer.writerow((r["template"], r["n_billed_calls"], r["n_merchants"], r["archetype"]))

    return len(rows), (0 if include_unmasked else withheld)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_templating_report(p: TemplatingPicture, cfg: Config) -> str:
    """Format the templating picture. Counts and rates only -- never template text.

    Question text is never printed by the scorecard because it carries merchant
    values inline; template text is the same problem one step earlier. The head
    CSV is where template text goes, and it stays in the operator's environment.
    """
    if not p.n_rows:
        return "TEMPLATING\n  (no queries ingested)"
    if not p.has_run:
        return (
            "TEMPLATING\n"
            f"  {p.n_rows:,} query rows, none templated — templating has not run on this DB.\n"
            "  (`coa reparse` rebuilds query_instances and clears templates; re-run analyze.)"
        )

    out = [
        "TEMPLATING",
        f"  templated          {p.n_templated:,} of {p.n_rows:,} query rows "
        f"({p.n_billed:,} billed)",
        f"  unmasked           {p.n_unmasked_billed:,} billed ({_pct(p.unmasked_share)}), "
        f"{p.n_unmasked:,} overall",
        "                     unmasked = nothing matched, so the template IS the query text",
        "",
        "  placeholders fired (billed queries containing each)",
    ]
    out += [f"    {field:<10} {n:>9,}" for field, n in p.by_field]

    out += [
        "",
        "  EXACT TEMPLATE HEAD  (layer 1 only — the layer-2 decision rests on this)",
        f"    distinct         {p.n_distinct:,} templates over {p.n_billed:,} billed queries",
        f"    top {p.head_size:<12} {_pct(p.head_coverage)} of billed queries",
        f"    singletons       {p.n_singleton:,} templates matched exactly one query",
    ]
    if p.head_is_whole:
        out.append(
            f"    Fewer than {HEAD_FOR_COVERAGE} distinct templates exist, so the head IS all of"
        )
        out.append("    them and covers 100% by definition — that says NOTHING about whether")
        out.append("    exact grouping is working. Read `distinct` and `singletons` instead.")
    elif p.head_coverage is not None and p.head_coverage < 0.5:
        out.append("    A thin head: most billed queries are not collapsing on exact match, so")
        out.append("    archetype shares built on layer 1 alone would describe a minority of cost.")
        out.append("    This is the measurement that would justify discussing layer 2 (fuzzy).")
    else:
        out.append("    A thick head: exact grouping already covers the bulk of billed queries,")
        out.append("    so fuzzy/semantic clustering has little left to merge.")

    out += ["", "  ARCHETYPES  (hand-maintained map — a HUMAN JUDGEMENT, label it as one)"]
    if not p.n_groups:
        out += [
            f"    none mapped      {cfg.archetype_groups} "
            + ("is empty" if cfg.archetype_groups.exists() else "does not exist yet"),
            "    Build it from `coa analyze --export-templates`, then re-run analyze.",
        ]
    else:
        out.append(
            f"    mapped           {p.n_mapped_billed:,} billed queries "
            f"({_pct(p.mapped_share)}) into {p.n_groups:,} archetypes"
        )
        out.append(
            "    An archetype -> question mapping is read off the template by eye. It is the"
        )
        out.append(
            "    one heuristic in this analysis, and any figure derived from it must say so."
        )
    return "\n".join(out)


def render_export_note(path: Path, written: int, withheld: int) -> str:
    """Say what the export wrote, what it withheld, and what the gate does not cover."""
    out = [f"  wrote {written:,} template(s) to {path}"]
    if withheld:
        out += [
            f"  WITHHELD {withheld:,} template(s) with no placeholder — those are verbatim",
            "  query text and may contain merchant names, addresses and phone numbers.",
            "  Pass --include-unmasked to export them anyway.",
        ]
    else:
        out.append("  no unmasked templates were withheld")
    out += [
        "  The gate is necessary, not sufficient: a template CAN still carry PII that",
        "  never reached pii_terms. Treat this file as merchant data — it is gitignored.",
    ]
    return "\n".join(out)


def counters(stats: Iterable[tuple[str, int]]) -> str:
    """One-line summary of a templating pass, for the analyze header."""
    d = dict(stats)
    line = (
        f"  templated {d.get('tmpl_rows', 0):,} query row(s): "
        f"{d.get('tmpl_masked', 0):,} masked, {d.get('tmpl_unmasked', 0):,} with no placeholder"
    )
    if d.get("tmpl_no_terms_merchants"):
        line += (
            f"\n  {d['tmpl_no_terms_merchants']:,} merchant(s) had NO pii_terms — "
            f"{d.get('tmpl_no_terms_rows', 0):,} unmaskable query row(s)"
        )
    if d.get("tmpl_group_unmatched"):
        line += f"\n  {d['tmpl_group_unmatched']:,} archetype_groups.csv row(s) matched nothing"
    return line
