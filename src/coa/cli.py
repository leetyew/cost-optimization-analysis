"""Command-line entry point.

`cli.py` owns *all* path and zip walking. Parsers receive `(src_name, lines)` and
never touch the filesystem, which keeps them pure over line iterators and unit
-testable against a six-line in-memory string.

Subcommands, in the order they are meant to be run:

    coa ingest     logs/jsonl/ + input/ + output/  ->  SQLite  (resumable)
    coa reparse    re-extract fields from stored raw lines, no source files read
    coa analyze    templating, archetypes, agreement, cost
    coa report     markdown + CSV bundle under reports/<timestamp>/
    coa doctor     one-screen health check, ready to paste back
    coa anomalies  summary | show CODE      <- the operator feedback channel
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from . import anomalies as anom
from .config import Config
from .db import already_ingested, connect, forget_file, mark_ingested
from .health import health_report
from .inputs import ingest_input
from .metrics import (
    cache_picture,
    open_page_overlap,
    render_cache_report,
    render_cost_report,
    render_open_page_report,
    tier_usage,
)
from .outputs import ingest_output
from .weblogs import ingest_weblog, reparse

# (kind, subdir, suffix, parser). Order matters: input/ is ingested before output/
# because output records duplicate a few merchant fields, and comparing them
# against an already-populated merchants table is what makes "input/ takes
# precedence" automatic instead of a precedence rule someone has to maintain.
#
# All three kinds are `.jsonl` and `logs/jsonl` sits a level deeper than the
# others, which is exactly why members are matched on subdir as well as suffix.
SOURCE_KINDS: tuple[tuple[str, str, str, Callable], ...] = (
    ("weblog", "logs/jsonl", ".jsonl", ingest_weblog),
    ("input", "input", ".jsonl", ingest_input),
    ("output", "output", ".jsonl", ingest_output),
)


def iter_source_files(root: Path, subdir: str, suffix: str) -> Iterator[tuple[str, Iterable[str]]]:
    """Yield `(src_name, lines)` for every matching file under `root`.

    Handles both a plain directory tree and `.zip` archives without extracting.
    Decoding uses errors="replace" so a bad byte degrades one character instead
    of killing the file; the parsers detect the replacement character and record
    an ENCODING anomaly.

    **Lines are yielded lazily, never materialized here.** `output/*.jsonl` alone
    extrapolates to 1-2 GB on a real corpus (fixture records average 39 KB), and
    reading a member whole costs several times its size in peak memory. That is
    not a slow path but a hard stop: resumable ingest skips *completed* files, so
    a single member too large to materialize fails identically on every retry.
    Streaming works because `ZipFile.open()` and `TextIOWrapper` are both lazy,
    and because the generator suspends inside the `with` block, keeping the
    handle open exactly as long as the consumer is reading it.

    A parser needing random access must materialize the iterator itself and say
    why in its docstring. None currently does.

    Zip members carrying a directory component are matched on `subdir` as well as
    `suffix`. Suffix alone was enough while logs were the only source, but input/
    and output/ are both `.jsonl` — a zip holding either would otherwise be
    ingested twice, once as each kind. Members at the archive root are still
    accepted; see the comment below.
    """
    if not root.exists():
        return

    for zpath in sorted(root.glob("*.zip")):
        with zipfile.ZipFile(zpath) as z:
            for member in sorted(z.namelist()):
                if not member.endswith(suffix) or member.endswith("/"):
                    continue
                # Only filter when there is a directory to filter on. A member at
                # the archive root has no subdir to disagree with, and skipping it
                # would ingest nothing at all while still exiting 0 — a far worse
                # failure than ingesting it twice, which at least fires DUP_*.
                # Matched as a contiguous run of path parts, not a substring, so
                # `input` does not also match `myinput/`.
                member_dirs = Path(member).parts[:-1]
                if member_dirs and not _under(member_dirs, subdir):
                    continue
                with z.open(member) as fh:
                    stream = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                    yield f"{zpath.name}!{member}", _strip_endings(stream)

    for path in sorted((root / subdir).glob(f"*{suffix}")) if (root / subdir).exists() else []:
        with path.open(encoding="utf-8", errors="replace") as fh:
            yield f"{subdir}/{path.name}", _strip_endings(fh)


def _under(member_dirs: tuple[str, ...], subdir: str) -> bool:
    """Whether `member_dirs` contains `subdir`'s parts as a contiguous run.

    `subdir` can be nested (`logs/jsonl`), so a plain membership test is not
    enough; a substring test would be worse, matching `input` inside `myinput`.
    """
    want = Path(subdir).parts
    return any(member_dirs[i : i + len(want)] == want for i in range(len(member_dirs)))


def _strip_endings(stream: Iterable[str]) -> Iterator[str]:
    """Drop line terminators, matching what `str.splitlines()` used to hand over.

    Universal-newline decoding has already normalized `\\r\\n` and lone `\\r` to
    `\\n` by this point, so stripping the tail is all that is left.
    """
    for line in stream:
        yield line.rstrip("\r\n")


def cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    """Parse web-search logs, inputs and outputs into SQLite. Resumable per file.

    Each source file is its own transaction, committed only after `mark_ingested`.
    A crash mid-file therefore leaves no partial rows, and the re-run redoes
    exactly that file rather than the whole corpus.
    """
    conn = connect(cfg.db)
    totals: Counter = Counter()
    seen = skipped = 0
    try:
        for kind, subdir, suffix, parser in SOURCE_KINDS:
            for src_name, lines in iter_source_files(cfg.data_root, subdir, suffix):
                seen += 1
                if already_ingested(conn, src_name):
                    if not args.force:
                        skipped += 1
                        continue
                    forget_file(conn, src_name)
                stats = _ingest_one(conn, cfg, kind, parser, src_name, lines)
                totals.update(stats)
                # Every parser reports `lines`, so the merged Counter holds the
                # cross-kind total. Keep a per-kind tally too, or the log section
                # of the summary silently reports jsonl lines as log lines.
                totals[f"{kind}_lines"] += stats["lines"]
                print(f"  {src_name}: {_file_line(kind, stats)}")

        print(_ingest_summary(conn, totals, seen, skipped))
        return 0
    finally:
        conn.close()


def _ingest_one(
    conn: sqlite3.Connection,
    cfg: Config,
    kind: str,
    parser: Callable,
    src_name: str,
    lines: list[str],
) -> Counter:
    """Parse one source file inside its own transaction.

    `mark_ingested` lands in the same transaction as the rows, so a crash mid-file
    leaves neither — the re-run redoes exactly that file rather than the whole
    corpus, and never double-counts one that finished.
    """
    rec = anom.AnomalyRecorder(
        conn, f"{kind}s", cfg.anomalies.max_excerpt_chars, cfg.anomalies.max_payload_rows
    )
    try:
        stats = parser(conn, rec, src_name, lines, cfg)
        rec.flush()
        mark_ingested(conn, src_name, kind, stats["lines"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def _file_line(kind: str, stats: Counter) -> str:
    """Per-file progress line, in the units that matter for that source kind."""
    if kind == "weblog":
        return (
            f"{stats['wl_merchants']} merchants, {stats['wl_runs']} runs, {stats['wl_calls']} calls"
        )
    if kind == "input":
        return f"{stats['in_records']} merchants, {stats['in_pii_terms']} pii terms"
    return f"{stats['out_records']} records, {stats['out_runs']} runs"


def _ingest_summary(conn: sqlite3.Connection, totals: Counter, seen: int, skipped: int) -> str:
    """One-screen data-quality report. Non-zero anomalies are the expected state."""
    calls = totals["wl_calls"]
    billed = totals["wl_action_search"]
    out = [
        "",
        "INGEST SUMMARY",
        f"  files              {seen} seen, {skipped} already done",
        f"  lines              {totals['lines']} "
        f"(weblog {totals['weblog_lines']}, input {totals['input_lines']}, "
        f"output {totals['output_lines']})",
        "",
        "  WEB-SEARCH LOGS",
        f"    merchants        {totals['wl_merchants']}",
        f"    runs             {totals['wl_runs']}"
        + (
            f"  ({totals['wl_runs'] / totals['wl_merchants']:.1f} per merchant)"
            if totals["wl_merchants"]
            else ""
        ),
        f"    calls            {calls}"
        + (f"  ({calls / totals['wl_runs']:.1f} per run)" if totals["wl_runs"] else ""),
        f"      search         {billed}"
        + (f"  ({billed / calls:.1%} of billed calls)" if calls else ""),
        f"      open_page      {totals['wl_action_open_page']}",
        f"      find_in_page   {totals['wl_action_find_in_page']}",
        f"    parse_conf       clean {totals['wl_conf_clean']}, "
        f"heuristic {totals['wl_conf_heuristic']}, failed {totals['wl_conf_failed']}",
        f"    encoding damage  {totals['encoding_damaged']} line(s)",
        _token_summary(conn),
        "",
        "  INPUTS",
        f"    merchants        {totals['in_records']} "
        f"({totals['in_dup_se10']} duplicate se10 kept-first, "
        f"{totals['in_bad_json']} unparseable line(s))",
        f"    pii terms        {totals['in_pii_terms']} "
        f"({totals['in_no_pii_terms']} merchant(s) yielded none)",
        "",
        "  OUTPUTS",
        f"    records          {totals['out_records']} "
        f"({totals['out_dup_se10']} duplicate se10 both-kept, "
        f"{totals['out_bad_json']} unparseable line(s))",
        # Every top-level key the parser depends on, as positive confirmation. An
        # absent one used to show up only as a downstream zero — an empty questions
        # table, no dict citations, no votes — which reads as "clean", not "missing".
        f"    keys not found   questions {totals['out_no_questions']}, "
        f"answer {totals['out_no_answers']}, "
        f"citation_evidence {totals['out_no_citation_evidence']}, "
        f"votes {totals['out_no_votes']}  (of {totals['out_records']} records)",
        f"    runs             {totals['out_runs']} "
        f"({totals['out_answer_blocks']} answer blocks, "
        f"{totals['out_answers_from_dict']} recovered from answer_dict)",
        f"    short runs       {totals['out_short_runs']} "
        f"(fewer answer blocks than the canonical question set)",
        # Expected to be ~every record: the questions carry merchant values inline.
        # Reported as a rate rather than an anomaly for exactly that reason.
        f"    question text    {totals['out_question_text_varies']} record(s) differ from the "
        f"canonical text\n                     (expected — merchant values are inline; "
        f"only a changed qnum SET is an anomaly)",
        f"    answer/dict      {totals['out_answer_disagreements']} answer(s) differ "
        f"from answer_dict",
        f"    citations        {totals['out_prose_citations']} prose + "
        f"{totals['out_dict_citations']} citation_evidence "
        f"({totals['out_empty_placeholders']} empty placeholder(s), "
        f"{totals['out_citation_mismatch']} qnum mismatch(es))",
        f"    votes            {totals['out_votes']} "
        f"({totals['out_votes_differ']} majority/final differ, "
        f"{totals['out_empty_voted_final']} record(s) with empty voted_final)",
        _evidence_shapes(totals),
        "",
    ]
    out.append(anom.render_summary(anom.summary(conn)))
    return "\n".join(out)


def _token_summary(conn: sqlite3.Connection) -> str:
    """Measured token volume, service-tier mix, and cache hit rate.

    These are cost levers that need no change to search behaviour at all, so
    they belong in front of the operator from the first ingest rather than
    waiting for the report:

    * `service_tier` spans roughly 4x between flex and priority rates, and this
      workload (batch fraud screening) is what flex exists for.
    * `cache_read` is a subset of input_tokens billed at a large discount, and
      the prompt is a fixed 48-question block -- an ideal caching profile. A low
      hit rate means paying full rate for a prefix that should be nearly free.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
        "SUM(cache_read) AS c, SUM(reasoning) AS r FROM runs"
    ).fetchone()
    if not row or not row["n"]:
        return "    tokens           (no runs ingested)"

    tiers = conn.execute(
        "SELECT COALESCE(service_tier, '<null>') AS tier, COUNT(*) AS n "
        "FROM runs GROUP BY tier ORDER BY n DESC"
    ).fetchall()
    i_tok, o_tok = row["i"] or 0, row["o"] or 0
    cached, reasoning = row["c"] or 0, row["r"] or 0
    lines = [
        f"    tokens           input {i_tok:,} (cached {cached:,}"
        + (f", {cached / i_tok:.1%} hit rate)" if i_tok else ")")
        + f", output {o_tok:,} (reasoning {reasoning:,}"
        + (f", {reasoning / o_tok:.1%})" if o_tok else ")"),
        "                     cache_read is INSIDE input, reasoning INSIDE output —",
        "                     costing them separately double-counts",
        "    service tier     " + ", ".join(f"{t['tier']} {t['n']}" for t in tiers),
    ]
    return "\n".join(lines)


def _evidence_shapes(totals: Counter) -> str:
    """Which Evidence rendering the corpus actually uses.

    Real data is expected to answer many questions on a 1-5 scale and return
    Evidence only when the answer is <= 3, but whether the no-evidence case writes
    `NULL`, a bare label, or omits the line entirely is unconfirmed. The parser
    accepts all three and reports the split, so the first real ingest settles it
    instead of a guess baked into a regex.
    """
    shapes = ("evidence_present", "evidence_null", "evidence_empty", "evidence_absent")
    if not sum(totals[s] for s in shapes):
        return "    evidence shapes  (no answer blocks parsed)"
    parts = ", ".join(f"{s.removeprefix('evidence_')} {totals[s]}" for s in shapes)
    return (
        f"    evidence shapes  {parts}\n"
        f"                     (`absent` = no Evidence line at all. If that is 0 on real\n"
        f"                     data, the optional-Evidence branch never ran — see CLAUDE.md\n"
        f"                     'Known real-data format facts'.)"
    )


def cmd_reparse(args: argparse.Namespace, cfg: Config) -> int:
    """Re-run field extraction over stored raw lines, without reading source files."""
    conn = connect(cfg.db)
    try:
        conn.execute("DELETE FROM anomalies WHERE stage = 'reparse'")
        rec = anom.AnomalyRecorder(
            conn, "reparse", cfg.anomalies.max_excerpt_chars, cfg.anomalies.max_payload_rows
        )
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


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    """One paste-ready screen of what actually landed. See health.py."""
    conn = connect(cfg.db)
    try:
        print(health_report(conn))
        return 0
    finally:
        conn.close()


def cmd_analyze(args: argparse.Namespace, cfg: Config) -> int:
    """Analysis over ingested tables. Templating and archetypes land in P3."""
    conn = connect(cfg.db)
    try:
        print(render_cache_report(cache_picture(conn)))
        print()
        print(render_open_page_report(open_page_overlap(conn)))
        print()
        print(render_cost_report(tier_usage(conn), cfg.pricing))
        return 0
    finally:
        conn.close()


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

    doc = sub.add_parser("doctor", help="one-screen health check, ready to paste back")
    doc.set_defaults(func=cmd_doctor)

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
