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


@dataclass(frozen=True)
class OpenPageOverlap:
    """How much of the citation set can be reached through `open_page` URLs."""

    cited_urls: int
    opened_urls: int
    both: int

    @property
    def covered(self) -> float:
        return self.both / self.cited_urls if self.cited_urls else 0.0


def open_page_overlap(conn: sqlite3.Connection) -> OpenPageOverlap:
    """Test the "if it was cited, the page was opened" hypothesis against the corpus.

    If it holds, `citation.url == open_page.url` links citations to calls exactly,
    with no new field and no heuristic. It probably does not hold — web-search
    models cite from result snippets without opening the page — but it is cheap to
    measure and the answer decides how much weight the open_page path can carry.

    Note what this can and cannot establish. Even at full coverage it links a
    citation to the *page open*, never to the *search that surfaced the page* —
    that hop exists only in `web_search_call.action.sources`, which is opt-in and
    absent from this corpus. So a high number is a finding about open_page value,
    not a substitute for search attribution.

    Compared per (se10, url): the same URL cited for two merchants is two facts.
    """
    row = conn.execute(
        """
        WITH cited AS (
            SELECT DISTINCT se10, url FROM citations
            WHERE url IS NOT NULL AND url != ''
        ), opened AS (
            SELECT DISTINCT se10, url FROM search_calls
            WHERE action_type = 'open_page' AND url IS NOT NULL AND url != ''
        )
        SELECT
            (SELECT COUNT(*) FROM cited)  AS cited_urls,
            (SELECT COUNT(*) FROM opened) AS opened_urls,
            (SELECT COUNT(*) FROM cited c
             JOIN opened o ON o.se10 = c.se10 AND o.url = c.url) AS both
        """
    ).fetchone()
    return OpenPageOverlap(row["cited_urls"], row["opened_urls"], row["both"])


def render_open_page_report(o: OpenPageOverlap) -> str:
    """Format the overlap, and say plainly what it does and does not license."""
    if not o.cited_urls:
        return "CITATION <-> OPEN_PAGE\n  (no citations ingested)"
    out = [
        "CITATION <-> OPEN_PAGE",
        f"  distinct cited URLs      {o.cited_urls:,}",
        f"  distinct opened URLs     {o.opened_urls:,}",
        f"  cited AND opened         {o.both:,}  ({o.covered:.1%} of cited URLs)",
        "",
    ]
    if o.covered >= 0.8:
        out += [
            "  Most citations were opened first, so citation -> open_page call is an exact",
            "  link for the bulk of the corpus. It still does NOT reach the search that",
            "  surfaced the page — only action.sources does that.",
        ]
    else:
        out += [
            "  Most cited URLs were never opened, so the model cited them straight from",
            "  search-result snippets. open_page cannot carry citation attribution; it",
            "  measures the value of page opens only.",
        ]
    return "\n".join(out)


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
