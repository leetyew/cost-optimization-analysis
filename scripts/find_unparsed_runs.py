#!/usr/bin/env python3
"""Locate the broken output runs, and test whether the two defects are one.

Two populations of roughly the same size have been measured, and whether they are
the SAME runs is the open question:

* **~1,047 prose-unreadable runs** -- every answer came from `answer_dict`, so the
  block regex could not read the prose at all. These are what `coa scorecard`
  excludes from its evidence-dependent rates.
* **~1,039 output-only runs** -- present in `output/` but absent from
  `logs/jsonl/`, so they carry no `usage_metadata` and contribute no tokens. Every
  cost figure is a floor by however many of these there are.

If they turn out to be the same runs, their pipeline lost the prose and the log
together and there is ONE defect. If they are independent, there are two, and the
counts being similar is a coincidence. The cross-tab below settles it; nothing
short of it can, which is why counting either alone was never enough.

`answers.parsed_from` records which parse supplied each answer. Runs where only
SOME answers came from the dict are reported separately: a run the regex read 40 of
48 answers from is a different defect from one it could not read at all, and only
the total failures are excluded anywhere today.

The DB keeps no raw output text (`output_records` stores `raw_json_hash`, not
`raw_json`), so the failing prose exists only in the corpus; what this prints is the
`src_file` / `src_line` pointer to it.

Two outputs, split by what is safe to send across the air gap:

* stdout -- per-file counts only, no merchant data, paste-able into a session.
* `<reports>/unparsed_runs.csv` -- the full list including `se10`, which is why it
  lands under the gitignored reports dir rather than being printed.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from coa.config import Config
from coa.db import connect

# EVERY output run, not just the broken ones: the cross-tab needs the healthy
# cells too, or "these two defects coincide" cannot be distinguished from "this
# corpus is mostly broken". One pass, because both classifications come off the
# same grouping.
#
# `has_log` is EXISTS against `runs` on (se10, run_id) -- the same join key
# `coa doctor` reconciles on, so the two cannot disagree. A run whose key did not
# parse has a NULL run_id and no join key at all; those are excluded from the log
# side there and cannot appear on the output side here either.
QUERY = """
WITH per_run AS (
    SELECT output_id,
           run_id,
           COUNT(*)                          AS n_answers,
           SUM(parsed_from = 'answer_dict')  AS n_dict
    FROM answers
    WHERE run_id IS NOT NULL
    GROUP BY output_id, run_id
)
SELECT o.se10, o.src_file, o.src_line, p.run_id, p.n_dict, p.n_answers,
       (p.n_dict = p.n_answers)  AS total_fail,
       (p.n_dict > 0)            AS any_dict,
       EXISTS (SELECT 1 FROM runs r
               WHERE r.se10 = o.se10 AND r.run_id = p.run_id) AS has_log
FROM per_run p JOIN output_records o ON o.id = p.output_id
ORDER BY has_log, total_fail DESC, o.src_file, o.src_line, p.run_id
"""

FIELDS = (
    "se10",
    "src_file",
    "src_line",
    "run_id",
    "n_dict",
    "n_answers",
    "total_fail",
    "has_log",
)


# How a run's prose parsed, for the cross-tab rows.
def _prose_state(row) -> str:
    if row["total_fail"]:
        return "prose UNREADABLE"
    if row["any_dict"]:
        return "prose partial"
    return "prose ok"


def main() -> int:
    """Print the per-file rollup, write the per-run detail, return an exit code."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Mirrors `coa --db`: a schema change means ingesting into a second DB beside
    # the old one, and this has to be pointable at either.
    ap.add_argument("--db", type=Path, help="override the configured DB path")
    args = ap.parse_args()

    cfg = Config.load()
    db = args.db or cfg.db
    if not db.exists():
        print(f"no database at {db} -- run `coa ingest` first", file=sys.stderr)
        return 1

    conn: sqlite3.Connection = connect(db)
    rows = conn.execute(QUERY).fetchall()
    if not rows:
        print("no output runs with answers -- ingest output/ first")
        return 0

    # The whole point of the script: do the two defects coincide?
    grid: Counter[tuple[str, str]] = Counter(
        (_prose_state(r), "in logs" if r["has_log"] else "OUTPUT-ONLY") for r in rows
    )
    states = ("prose ok", "prose partial", "prose UNREADABLE")
    print(f"{len(rows):,} output runs\n")
    print(f"  {'':<18}{'in logs':>12}{'OUTPUT-ONLY':>14}")
    for state in states:
        print(f"  {state:<18}{grid[(state, 'in logs')]:>12,}{grid[(state, 'OUTPUT-ONLY')]:>14,}")

    unreadable = sum(
        grid[(s, w)] for s in ("prose UNREADABLE",) for w in ("in logs", "OUTPUT-ONLY")
    )
    outonly = sum(grid[(s, "OUTPUT-ONLY")] for s in states)
    both = grid[("prose UNREADABLE", "OUTPUT-ONLY")]
    print(f"\n  prose-unreadable  {unreadable:,}")
    print(f"  output-only       {outonly:,}")
    print(f"  BOTH              {both:,}")
    if unreadable and outonly:
        # Independence is the null hypothesis; a corner holding nearly all of both
        # is what would say the two failures are one.
        overlap = both / min(unreadable, outonly)
        verdict = (
            "SAME defect -- the pipeline lost prose and log together"
            if overlap > 0.9
            else "INDEPENDENT -- two separate defects, similar counts are coincidence"
            if overlap < 0.1
            else "PARTIAL overlap -- neither explanation is clean"
        )
        print(f"  overlap           {overlap:.1%} of the smaller set -> {verdict}")

    # Only the defective rows go to the CSV; the healthy majority is noise there.
    bad = [r for r in rows if r["any_dict"] or not r["has_log"]]
    cfg.reports.mkdir(parents=True, exist_ok=True)
    out = cfg.reports / "unparsed_runs.csv"
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        writer.writerows([tuple(r[f] for f in FIELDS) for r in bad])

    if not bad:
        print("\nno defective runs -- nothing written")
        return 0

    by_file: Counter[str] = Counter(r["src_file"] for r in bad)
    print("\nby file (defective runs):")
    for src, n in sorted(by_file.items()):
        print(f"  {src:<48} {n:>7,}")

    # The pointer, spelled out: the operator inspects the corpus, not the DB, and
    # `src_line` is 1-based so it feeds sed directly.
    # `src_file` is stored relative to data_root, so the hint has to rejoin it or
    # the path it prints will not open.
    #
    # ONE POINTER PER DEFECT, not one for the list. The rows are ordered log-side
    # first, so a single pointer lands on an OUTPUT-ONLY run -- whose prose is
    # usually perfectly readable, because its defect is the missing log. Sending
    # someone to inspect prose that was never the problem makes the corpus look
    # fine and the parser look wrong.
    print(f"\ndetail ({len(bad):,} rows, contains se10, gitignored): {out}")
    for label, match in (
        ("prose UNREADABLE (inspect the prose)", lambda r: r["total_fail"]),
        ("OUTPUT-ONLY (prose is fine; the LOG is missing)", lambda r: not r["has_log"]),
    ):
        row = next((r for r in bad if match(r)), None)
        if row is None:
            print(f"  {label}: none in this corpus")
            continue
        path = cfg.data_root / row["src_file"]
        print(f"  {label}:\n    sed -n '{row['src_line']}p' {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
