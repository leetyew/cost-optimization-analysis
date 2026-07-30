"""Central anomaly recorder and its paste-oriented rendering.

This module is the project's only feedback channel from reality. Real data lives
in an environment Claude Code never sees, so the loop is:

    operator runs `coa anomalies show CODE`
      -> pastes the output into a Claude Code session
      -> parser gets patched
      -> the new case joins the fixture generator as a regression

That makes the *rendering* a UX surface rather than a formatting detail: output
that runs to thousands of lines does not get pasted, and the loop stalls. Hence
the dedup-by-code, the sample cap, and the fenced block.

Nothing here raises. Recording an anomaly is what we do *instead* of failing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

DEFAULT_MAX_EXCERPT = 2000


class AnomalyRecorder:
    """Buffers anomaly rows and flushes them to SQLite.

    Buffering keeps ingest a single bulk insert per source file rather than a
    write per surprise — on malformed-heavy files the anomaly rate can approach
    the line rate, and a per-row INSERT would dominate ingest time.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        stage: str,
        max_excerpt: int = DEFAULT_MAX_EXCERPT,
    ) -> None:
        self.conn = conn
        self.stage = stage
        self.max_excerpt = max_excerpt
        self._buf: list[tuple] = []
        self.counts: dict[str, int] = {}

    def record(
        self,
        code: str,
        *,
        se10: str | None = None,
        src_file: str | None = None,
        src_line: int | None = None,
        raw_excerpt: str | None = None,
        context: Iterable[str] | None = None,
        detail: str | None = None,
    ) -> None:
        """Log one anomaly. Never raises, never drops the underlying record."""
        self.counts[code] = self.counts.get(code, 0) + 1
        self._buf.append(
            (
                self.stage,
                code,
                se10,
                src_file,
                src_line,
                _truncate(raw_excerpt, self.max_excerpt),
                "\n".join(context) if context else None,
                detail,
            )
        )

    def flush(self) -> None:
        """Write buffered rows. Safe to call repeatedly."""
        if not self._buf:
            return
        self.conn.executemany(
            "INSERT INTO anomalies "
            "(stage, code, se10, src_file, src_line, raw_excerpt, context, detail, ts_recorded) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            self._buf,
        )
        self._buf.clear()


def _truncate(text: str | None, limit: int) -> str | None:
    """Cap an excerpt, marking that it was cut so nobody debugs a phantom truncation."""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text)} chars total]"


def summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Counts by code, worst first — the one-screen ingest health check."""
    return conn.execute(
        "SELECT code, stage, COUNT(*) AS n, COUNT(DISTINCT src_file) AS n_files "
        "FROM anomalies GROUP BY code, stage ORDER BY n DESC"
    ).fetchall()


def render_summary(rows: Iterable[sqlite3.Row]) -> str:
    """Format the summary table. Zero anomalies is suspicious, not clean."""
    rows = list(rows)
    if not rows:
        return (
            "No anomalies recorded.\n"
            "On real data this is a warning sign, not a success: it usually means a\n"
            "detector is not wired up rather than that the input was flawless."
        )
    width = max(len(r["code"]) for r in rows)
    out = [f"{'CODE'.ljust(width)}  {'COUNT':>7}  {'FILES':>5}  STAGE", "-" * (width + 25)]
    total = 0
    for r in rows:
        total += r["n"]
        out.append(f"{r['code'].ljust(width)}  {r['n']:>7}  {r['n_files']:>5}  {r['stage']}")
    out.append("-" * (width + 25))
    out.append(f"{'TOTAL'.ljust(width)}  {total:>7}")
    out.append("")
    out.append("Inspect any code with:  coa anomalies show <CODE>")
    return "\n".join(out)


def samples(conn: sqlite3.Connection, code: str, limit: int) -> list[sqlite3.Row]:
    """Representative rows for one code.

    Spread across distinct source files first: N samples from one file usually
    show the same defect N times, whereas one per file exposes whether the
    surprise is systematic or local to a single export.
    """
    return conn.execute(
        """
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY src_file ORDER BY id) AS rn
            FROM anomalies WHERE code = ?
        ) ORDER BY rn, id LIMIT ?
        """,
        (code, limit),
    ).fetchall()


def render_samples(code: str, rows: Iterable[sqlite3.Row], total: int) -> str:
    """Render samples as a fenced block sized to be pasted into a chat session."""
    rows = list(rows)
    if not rows:
        return f"No anomalies recorded with code {code!r}."

    out = [
        f"ANOMALY {code} — showing {len(rows)} of {total}",
        "```",
    ]
    for i, r in enumerate(rows, 1):
        loc = f"{r['src_file'] or '?'}:{r['src_line'] if r['src_line'] is not None else '?'}"
        out.append(
            f"--- sample {i}/{len(rows)}  {loc}" + (f"  se10={r['se10']}" if r["se10"] else "")
        )
        if r["detail"]:
            out.append(f"    detail: {r['detail']}")
        if r["context"]:
            out.append("    context:")
            out.extend(f"      {ln}" for ln in r["context"].split("\n"))
        elif r["raw_excerpt"]:
            out.append("    raw:")
            out.extend(f"      {ln}" for ln in r["raw_excerpt"].split("\n"))
    out.append("```")
    return "\n".join(out)
