#!/usr/bin/env python3
"""Locate the output runs whose prose parse yielded nothing.

`answers.parsed_from` records which of the three parses supplied each answer, so a
run whose rows are ALL `answer_dict` is one the block regex could not read at all --
the 1,047 runs the scorecard has to exclude from its evidence-dependent rates. The
DB keeps no raw output text (`output_records` stores `raw_json_hash`, not
`raw_json`), so the failing prose exists only in the corpus; what this prints is the
`src_file` / `src_line` pointer to it.

Runs where SOME answers came from the dict are reported too. Nothing counts those
today, and they may well outnumber the total failures.

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coa.config import Config  # noqa: E402
from coa.db import connect  # noqa: E402

# One pass over `answers` yields both populations: `HAVING n_dict > 0` catches any
# run the prose parse missed part of, and `total_fail` marks the subset it missed
# entirely. Two queries would scan the table twice for the same rows.
QUERY = """
WITH failed AS (
    SELECT output_id,
           run_id,
           COUNT(*)                          AS n_answers,
           SUM(parsed_from = 'answer_dict')  AS n_dict
    FROM answers
    WHERE run_id IS NOT NULL
    GROUP BY output_id, run_id
    HAVING n_dict > 0
)
SELECT o.se10, o.src_file, o.src_line, f.run_id, f.n_dict, f.n_answers,
       (f.n_dict = f.n_answers) AS total_fail
FROM failed f JOIN output_records o ON o.id = f.output_id
ORDER BY total_fail DESC, o.src_file, o.src_line, f.run_id
"""

FIELDS = ("se10", "src_file", "src_line", "run_id", "n_dict", "n_answers", "total_fail")


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
        print("no runs with dict-sourced answers -- every run's prose parsed")
        return 0

    cfg.reports.mkdir(parents=True, exist_ok=True)
    out = cfg.reports / "unparsed_runs.csv"
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        writer.writerows([tuple(r[f] for f in FIELDS) for r in rows])

    total = sum(1 for r in rows if r["total_fail"])
    merchants = len({r["se10"] for r in rows if r["total_fail"]})
    by_file: Counter[tuple[str, bool]] = Counter(
        (r["src_file"], bool(r["total_fail"])) for r in rows
    )

    print(f"unparsed runs     {total:,} total-failure, {len(rows) - total:,} partial")
    print(f"merchants         {merchants:,} with at least one total failure")
    print("\nby file:")
    for src in sorted({f for f, _ in by_file}):
        print(f"  {src:<48} {by_file[(src, True)]:>7,} total  {by_file[(src, False)]:>7,} partial")

    # The pointer, spelled out: the operator inspects the corpus, not the DB, and
    # `src_line` is 1-based so it feeds sed directly.
    # `src_file` is stored relative to data_root, so the hint has to rejoin it or
    # the path it prints will not open.
    first = rows[0]
    print(f"\ndetail (contains se10, gitignored): {out}")
    print(f"read one: sed -n '{first['src_line']}p' {cfg.data_root / first['src_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
