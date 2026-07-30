"""Command-line entry point.

`cli.py` owns *all* path and zip walking. Parsers receive `(src_name, lines)` and
never touch the filesystem, which keeps them pure over line iterators and unit
-testable against a six-line in-memory string.

Subcommands, in the order they are meant to be run:

    coa ingest     logs/ + output/ + input/  ->  SQLite   (resumable)
    coa reparse    re-extract fields from stored raw lines, no source files read
    coa analyze    templating, archetypes, run bursts, agreement, cost
    coa report     markdown + CSV bundle under reports/<timestamp>/
    coa anomalies  summary | show CODE      <- the operator feedback channel
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from . import anomalies as anom
from .config import Config
from .db import already_ingested, connect, forget_file, mark_ingested
from .logs import ingest_log, reparse


def iter_source_files(root: Path, subdir: str, suffix: str) -> Iterator[tuple[str, list[str]]]:
    """Yield `(src_name, lines)` for every matching file under `root`.

    Handles both a plain directory tree and `.zip` archives without extracting —
    `ZipFile.open()` already streams members, so no separate source abstraction
    is warranted. Decoding uses errors="replace" so a bad byte degrades one
    character instead of killing the file; the caller detects the replacement
    character and records an ENCODING anomaly.
    """
    if not root.exists():
        return

    for zpath in sorted(root.glob("*.zip")):
        with zipfile.ZipFile(zpath) as z:
            for member in sorted(z.namelist()):
                if member.endswith(suffix) and not member.endswith("/"):
                    with z.open(member) as fh:
                        text = fh.read().decode("utf-8", errors="replace")
                    yield f"{zpath.name}!{member}", text.splitlines()

    for path in sorted((root / subdir).glob(f"*{suffix}")) if (root / subdir).exists() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        yield f"{subdir}/{path.name}", text.splitlines()


def cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    """Parse logs, outputs and inputs into SQLite. Resumable per source file.

    Each source file is its own transaction, committed only after `mark_ingested`.
    A crash mid-file therefore leaves no partial rows, and the re-run redoes
    exactly that file rather than the whole 2 GB corpus.
    """
    conn = connect(cfg.db)
    totals: Counter = Counter()
    seen = skipped = 0
    try:
        for src_name, lines in iter_source_files(cfg.data_root, "logs", ".log"):
            seen += 1
            if already_ingested(conn, src_name):
                if not args.force:
                    skipped += 1
                    continue
                forget_file(conn, src_name)
            rec = anom.AnomalyRecorder(conn, "logs", cfg.anomalies.max_excerpt_chars)
            try:
                stats = ingest_log(conn, rec, src_name, lines, cfg)
                rec.flush()
                mark_ingested(conn, src_name, "log", stats["lines"])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            totals.update(stats)
            print(f"  {src_name}: {stats['lines']} lines, {stats['action']} actions")

        print(_ingest_summary(conn, totals, seen, skipped))
        return 0
    finally:
        conn.close()


def _ingest_summary(conn, totals: Counter, seen: int, skipped: int) -> str:
    """One-screen data-quality report. Non-zero anomalies are the expected state."""
    strict, orphan = totals["strict"], totals["orphan"]
    paired = strict + orphan
    out = [
        "",
        "INGEST SUMMARY",
        f"  files              {seen} seen, {skipped} already done",
        f"  lines              {totals['lines']}",
        f"  timestamped        {totals['timestamped']} "
        f"(web_search_call {totals['web_search_call']})",
        f"  action lines       {paired}",
        f"    strict-paired    {strict}" + (f"  ({strict / paired:.1%})" if paired else ""),
        f"    orphan           {orphan}" + (f"  ({orphan / paired:.1%})" if paired else ""),
        f"  pairing delta      {totals['pairing_delta']} "
        f"(web_search_call lines with no adjacent action)",
        f"  parse_conf         clean {totals['conf_clean']}, "
        f"heuristic {totals['conf_heuristic']}, failed {totals['conf_failed']}",
        f"  possible wraps     {totals['possible_wrap']}",
        f"  encoding damage    {totals['encoding_damaged']} line(s)",
        "",
    ]
    out.append(anom.render_summary(anom.summary(conn)))
    return "\n".join(out)


def cmd_reparse(args: argparse.Namespace, cfg: Config) -> int:
    """Re-run field extraction over stored raw lines, without reading source files."""
    conn = connect(cfg.db)
    try:
        conn.execute("DELETE FROM anomalies WHERE stage = 'reparse'")
        rec = anom.AnomalyRecorder(conn, "reparse", cfg.anomalies.max_excerpt_chars)
        stats = reparse(conn, rec, cfg)
        rec.flush()
        conn.commit()
        print(
            f"reparsed {stats['reparsed']} action row(s) from stored raw lines "
            f"(clean {stats['conf_clean']}, heuristic {stats['conf_heuristic']}, "
            f"failed {stats['conf_failed']})"
        )
        return 0
    finally:
        conn.close()


def cmd_analyze(args: argparse.Namespace, cfg: Config) -> int:
    print("analyze: not implemented until P3/P4")
    return 0


def cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    print("report: not implemented until P5")
    return 0


def cmd_anomalies(args: argparse.Namespace, cfg: Config) -> int:
    """Summary table, or paste-ready samples for one code."""
    conn = connect(cfg.db)
    try:
        if args.what == "summary":
            print(anom.render_summary(anom.summary(conn)))
        else:
            code = args.code
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM anomalies WHERE code = ?", (code,)
            ).fetchone()["n"]
            rows = anom.samples(conn, code, args.sample or cfg.anomalies.default_sample)
            print(anom.render_samples(code, rows, total))
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coa", description=__doc__.split("\n")[0])
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--db", type=Path, help="override configured DB path")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="parse source files into SQLite")
    ing.add_argument("data_root", nargs="?", type=Path, help="override configured data_root")
    ing.add_argument("--force", action="store_true", help="re-ingest files already done")
    ing.set_defaults(func=cmd_ingest)

    rep = sub.add_parser("reparse", help="re-extract fields from stored raw lines")
    rep.set_defaults(func=cmd_reparse)

    ana = sub.add_parser("analyze", help="templating, archetypes, metrics")
    ana.set_defaults(func=cmd_analyze)

    rpt = sub.add_parser("report", help="build the markdown + CSV bundle")
    rpt.set_defaults(func=cmd_report)

    an = sub.add_parser("anomalies", help="inspect recorded anomalies")
    an_sub = an.add_subparsers(dest="what", required=True)
    an_sum = an_sub.add_parser("summary", help="counts by code")
    an_sum.set_defaults(func=cmd_anomalies)
    an_show = an_sub.add_parser("show", help="paste-ready samples for one code")
    an_show.add_argument("code")
    an_show.add_argument("--sample", type=int, help="how many samples (default from config)")
    an_show.set_defaults(func=cmd_anomalies)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    if args.db:
        cfg = Config(**{**vars(cfg), "db": args.db})
    if getattr(args, "data_root", None):
        cfg = Config(**{**vars(cfg), "data_root": args.data_root})
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
