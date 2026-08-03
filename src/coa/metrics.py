"""Analysis over ingested tables. Cache economics first — see `cache_report`.

Everything here is a query over what ingest already stored, so it is re-runnable
and cheap. Nothing writes back to `search_calls` or `query_instances`, which is
what keeps `coa analyze` safe to run after `coa reparse`.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from .config import Pricing, TierRates

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

    # Folded one prompt at a time rather than collected into a list. A common
    # prefix only ever shrinks, so the running value plus the current row is all
    # that is needed — and holding ~19k prompts costs ~150 MB at fixture prompt
    # sizes, several times that at real ones, for no benefit.
    prefix: str | None = None
    total_chars = n_prompts = 0
    for row in conn.execute(
        "SELECT question_user_prompt AS p FROM output_records "
        "WHERE question_user_prompt IS NOT NULL AND question_user_prompt != ''"
    ):
        prompt = row["p"]
        n_prompts += 1
        total_chars += len(prompt)
        prefix = prompt if prefix is None else os.path.commonprefix([prefix, prompt])

    return CachePicture(
        n_runs=totals["n"],
        input_tokens=totals["i"],
        cache_read=totals["c"],
        by_run=by_run,
        prefix_chars=len(prefix or ""),
        avg_prompt_chars=total_chars // n_prompts if n_prompts else 0,
        n_prompts=n_prompts,
    )


@dataclass(frozen=True)
class TierUsage:
    """Measured volume for one service tier. Prices are applied separately."""

    tier: str
    n_runs: int
    input_tokens: int
    cache_read: int
    output_tokens: int
    n_search_calls: int
    n_query_entries: int

    def cost(self, rates: TierRates) -> float | None:
        """Cost for this tier, or None when any rate it needs is unset.

        The two subset rules live here and are the easiest thing in the project
        to get wrong: `cache_read` is *inside* `input_tokens`, so full-rate input
        is the difference, and `reasoning` is *inside* `output_tokens`, so it has
        no term of its own. Adding either separately double-counts and inflates
        the baseline — the worst direction to err in a document arguing someone
        should spend less.

        The call fee is per visible call across every action type, settled
        against the dashboard — see `tier_usage`. `n_query_entries` is reported
        alongside as volume, never as cost: sub-queries are not billed.
        """
        if any(
            r is None
            for r in (rates.input_per_mtok, rates.cached_input_per_mtok, rates.output_per_mtok)
        ):
            return None
        full_rate_input = self.input_tokens - self.cache_read
        return (
            full_rate_input * rates.input_per_mtok / 1e6
            + self.cache_read * rates.cached_input_per_mtok / 1e6
            + self.output_tokens * rates.output_per_mtok / 1e6
            + self.search_fee(rates, self.n_search_calls)
        )

    def search_fee(self, rates: TierRates, n_billed: int) -> float:
        fee = rates.fee_per_1k_search_calls
        return 0.0 if fee is None else n_billed * fee / 1000


def tier_usage(conn: sqlite3.Connection) -> list[TierUsage]:
    """Token and call volume per service tier.

    **Every action type is billed**, not just `search`. Settled 2026-08-03 against
    the dashboard: the $6,946.95 charge is exactly 694,695 calls at $10/1K, and
    694,695 is the total across search + open_page + find_in_page. Filtering to
    `search` here understated the fee by 101,985 calls — $1,019.85, or 17.2%.

    Two things this must not do, both of which silently *understate* cost:

    * **Drop calls whose `run_pk` is NULL.** An inner join would, and a billed
      call vanishing from a cost report is the wrong direction to be wrong. They
      are bucketed under `<unset>` instead, and the tier list is the union of both
      sides so a tier with calls but no runs still appears.
    * **Let an empty `queries` array count as zero billed searches.** A call bills
      at least once, so the per-entry count floors at 1.
    """
    runs = {
        r["tier"]: r
        for r in conn.execute(
            """
            SELECT
                COALESCE(service_tier, '<unset>')  AS tier,
                COUNT(*)                           AS n_runs,
                COALESCE(SUM(input_tokens), 0)     AS input_tokens,
                COALESCE(SUM(cache_read), 0)       AS cache_read,
                COALESCE(SUM(output_tokens), 0)    AS output_tokens
            FROM runs GROUP BY tier
            """
        )
    }
    calls = {
        r["tier"]: (r["n_calls"], r["n_entries"])
        for r in conn.execute(
            """
            SELECT
                COALESCE(r.service_tier, '<unset>') AS tier,
                COUNT(*)                            AS n_calls,
                COALESCE(SUM(
                    CASE WHEN c.queries_json IS NULL THEN 1
                         ELSE MAX(1, json_array_length(c.queries_json)) END
                ), 0)                               AS n_entries
            FROM search_calls c LEFT JOIN runs r ON r.id = c.run_pk
            GROUP BY tier
            """
        )
    }
    return [
        TierUsage(
            tier=tier,
            n_runs=runs[tier]["n_runs"] if tier in runs else 0,
            input_tokens=runs[tier]["input_tokens"] if tier in runs else 0,
            cache_read=runs[tier]["cache_read"] if tier in runs else 0,
            output_tokens=runs[tier]["output_tokens"] if tier in runs else 0,
            n_search_calls=calls.get(tier, (0, 0))[0],
            n_query_entries=calls.get(tier, (0, 0))[1],
        )
        for tier in sorted(set(runs) | set(calls))
    ]


def render_cost_report(usage: list[TierUsage], pricing: Pricing) -> str:
    """Cost per tier, as a single figure per the settled billing unit.

    This used to print a low—high range straddling "billed per call" versus
    "billed per `queries` entry". The dashboard reconciliation collapsed that,
    so a range here would now overstate the uncertainty that remains.

    A tier whose rates are unset is reported as unpriced rather than folded in at
    someone else's rate, and the banner names exactly what is missing.
    """
    if not usage:
        return "COST\n  (no runs ingested)"

    out = ["COST"]
    if not pricing.is_verified:
        out += [
            "  *** UNVERIFIED PRICING ***",
            f"  unset: {', '.join(pricing.missing())}",
            "  Rates are operator-supplied. Relative shares below need no prices",
            "  and are unaffected.",
            "",
        ]

    total = 0.0
    priced_all = True
    for u in usage:
        rates = pricing.for_tier(None if u.tier == "<unset>" else u.tier)
        cost = u.cost(rates)
        out.append(
            f"  {u.tier:<10} {u.n_runs:>7,} runs  {u.n_search_calls:>9,} billed calls  "
            f"({u.n_query_entries:,} query entries, not billed)"
        )
        if cost is None:
            priced_all = False
            out.append(f"  {'':<10} UNPRICED — no rates configured for this tier")
            continue
        total += cost
        out.append(f"  {'':<10} ${cost:,.2f}")

    if any(u.tier == "<unset>" for u in usage):
        out.append(
            "  `<unset>` runs carry no service_tier and are costed at STANDARD rates, "
            "on the\n             assumption that absent means the API default was used. "
            "That is an inference."
        )

    out += ["", f"  TOTAL      ${total:,.2f}"]
    out.append(
        "  Billed per visible call across ALL action types (search, open_page,"
        "\n  find_in_page), reconciled against the dashboard charge. Sub-queries"
        "\n  inside a call are not billed."
    )
    if not priced_all:
        out.append("  Total EXCLUDES tiers with no rates configured.")
    return "\n".join(out)


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
