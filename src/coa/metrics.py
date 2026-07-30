"""Analysis over ingested tables. Cache economics first — see `cache_report`.

Everything here is a query over what ingest already stored, so it is re-runnable
and cheap. Nothing writes back to `search_calls` or `query_instances`, which is
what keeps `coa analyze` safe to run after `coa reparse`.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

# OpenAI does not cache prompts below this, and there is no partial credit: under
# the floor `cache_read` is exactly 0. A short static prefix is a dead lever, not
# a weak one.
CACHE_MIN_TOKENS = 1024

# Rough chars-per-token for English prose. Only ever used to compare a measured
# prefix against CACHE_MIN_TOKENS, so an approximation is honest here — but the
# figure is labelled as approximate wherever it is printed.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class CachePicture:
    """What the corpus says about prompt-cache economics."""

    n_runs: int
    input_tokens: int
    cache_read: int
    by_run: list[tuple[int | None, int, int, int]]  # (run_id, n, input, cached)
    prefix_chars: int
    avg_prompt_chars: int
    n_prompts: int

    @property
    def hit_rate(self) -> float:
        return self.cache_read / self.input_tokens if self.input_tokens else 0.0

    @property
    def prefix_tokens(self) -> int:
        return self.prefix_chars // CHARS_PER_TOKEN

    @property
    def prefix_reaches_floor(self) -> bool:
        return self.prefix_tokens >= CACHE_MIN_TOKENS


def cache_picture(conn: sqlite3.Connection) -> CachePicture:
    """Measure both cache levers separately, because they have different fixes.

    1. **Cross-merchant** — a static prefix shared by every merchant's prompt.
       Measured as the longest common prefix of the stored user prompts. When the
       48 questions carry merchant values inline (`...building at <zip>...`), the
       first such question ends the shared prefix, so this is usually tiny and the
       only fix is restructuring the prompt.
    2. **Same-merchant repeat runs** — run_1+ reuse run_0's byte-identical prompt.
       This works no matter how unique each merchant's prompt is, so it is the
       lever that survives inline placeholders. Its fix is scheduling: issue a
       merchant's runs inside the cache TTL rather than in separate passes.

    Splitting by `run_id` is what tells the two apart: cache on run_0 can only
    come from (1), cache on run_1+ mostly from (2).
    """
    totals = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i, "
        "COALESCE(SUM(cache_read), 0) AS c FROM runs"
    ).fetchone()
    by_run = [
        (r["run_id"], r["n"], r["i"], r["c"])
        for r in conn.execute(
            "SELECT run_id, COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i, "
            "COALESCE(SUM(cache_read), 0) AS c FROM runs GROUP BY run_id ORDER BY run_id"
        )
    ]

    prompts = [
        r["question_user_prompt"]
        for r in conn.execute(
            "SELECT question_user_prompt FROM output_records "
            "WHERE question_user_prompt IS NOT NULL AND question_user_prompt != ''"
        )
    ]
    prefix = os.path.commonprefix(prompts) if prompts else ""
    avg = sum(len(p) for p in prompts) // len(prompts) if prompts else 0

    return CachePicture(
        n_runs=totals["n"],
        input_tokens=totals["i"],
        cache_read=totals["c"],
        by_run=by_run,
        prefix_chars=len(prefix),
        avg_prompt_chars=avg,
        n_prompts=len(prompts),
    )


def render_cache_report(p: CachePicture) -> str:
    """Format the cache picture, naming the fix each number implies."""
    if not p.n_runs:
        return "PROMPT CACHE\n  (no runs ingested)"

    out = [
        "PROMPT CACHE",
        f"  input tokens       {p.input_tokens:,}",
        f"  read from cache    {p.cache_read:,}  ({p.hit_rate:.1%})",
        "",
        "  by run index       (run_0 cache can only come from a shared prefix;",
        "                      run_1+ from the same merchant's earlier run)",
    ]
    for run_id, n, tokens, cached in p.by_run:
        label = f"run_{run_id}" if run_id is not None else "unparsed"
        share = f"{cached / tokens:.1%}" if tokens else "n/a"
        out.append(f"    {label:<12} {n:>7} runs  {tokens:>12,} tok  cached {share:>6}")

    out += ["", "  shared prompt prefix"]
    if not p.n_prompts:
        out.append("    (no prompts stored; ingest output/ to measure this)")
    else:
        out.append(
            f"    {p.prefix_chars:,} of {p.avg_prompt_chars:,} chars "
            f"(~{p.prefix_tokens:,} tokens, {p.prefix_chars / p.avg_prompt_chars:.1%} "
            f"of an average prompt)"
        )
        if p.prefix_reaches_floor:
            out.append(
                f"    Clears the {CACHE_MIN_TOKENS}-token minimum, so this prefix is "
                f"cacheable across merchants today."
            )
        else:
            out.append(
                f"    BELOW the {CACHE_MIN_TOKENS}-token minimum, so it caches NOTHING "
                f"across merchants —"
            )
            out.append(
                "    there is no partial credit under the floor. Expected when the questions carry"
            )
            out.append(
                "    merchant values inline: the first such question ends the shared prefix."
            )

    out += [
        "",
        "  Reading this:",
        "    * run_1+ cached but run_0 not  -> repeat-run caching works; the lever is",
        "      keeping a merchant's runs inside the cache TTL (30 min on current models).",
        "    * nothing cached anywhere      -> check run spacing first, then prompt shape.",
        "    * prefix below the floor       -> cross-merchant caching needs the prompt",
        "      restructured (static questions first, merchant values in a block at the end),",
        "      which is a change to the other team's prompt and needs A/B validation before",
        "      it is recommended: it is not answer-preserving by construction.",
    ]
    return "\n".join(out)
