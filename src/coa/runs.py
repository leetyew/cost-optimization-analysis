"""The run-count lever: what repeat runs cost, and whether they earn it.

Cost scales with the number of runs and **needs no attribution at all**, which
makes this the one large lever that is not blocked on the call -> question link.
19,349 merchants at ~2.6 runs each means cutting to 2 removes ~23% of all runs,
and the per-call search fee (42.9% of the bill) falls exactly in proportion.

Two halves, and they must be read together or the lever gets oversold:

1. **What a marginal run costs.** Not the average run's cost. `run_1+` reuse
   `run_0`'s byte-identical prompt, so their input is largely cache reads billed
   at a tenth of the input rate -- the run you would cut is already the cheapest
   one on input. Its search fee and output tokens, however, are full price and
   identical to `run_0`'s. Costing a marginal run at the average overstates the
   saving; this module splits it so the linear and sub-linear parts are visible
   separately.

2. **Whether the third run changes any answer.** This is where the intuition
   inverts. For runs A, B, C: if A == B the majority is already fixed and C
   *cannot* change it. If A != B there is no majority for C to change -- C is
   what creates one. So "how often does the third run flip the majority" reduces
   to "how often do the first two disagree", and a LOW agreement rate means the
   third run is decisive **often**, not rarely.

   That reframes the finding. A third run that arbitrates between two answers
   which already disagree is not a quorum doing its job; it is a tie-break whose
   own reliability is exactly as poor as the two it is adjudicating. The cases
   where all three differ are worse still: three runs, no majority, nothing to
   vote on.

This module deliberately does NOT reproduce `scorecard.py`'s `agreement_rate`.
That figure is `COUNT(DISTINCT answer) = 1` over ALL of a merchant's runs, so a
4-run merchant must have all four match -- it is conflated by run count and
decays mechanically as runs increase. Here every comparison is conditioned on
the run count, and the three runs compared are always the three lowest run_ids.

(PLAN.md §6.3 reserved `runs.py` for burst-based run *attribution*. That is moot:
`run_id` is a structural key in `logs/jsonl`, so attribution is exact. The name is
reused for the question about runs that is still open.)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Pricing
from .metrics import TierUsage
from .outputs import _norm_text

# Runs at or above this index are what "cut to 2 runs per merchant" removes.
# run_0 and run_1 stay, so repeat-run prompt caching is untouched.
KEEP_RUNS = 2


@dataclass(frozen=True)
class RunSlice:
    """One run index, costed per service tier.

    Tier is part of the pricing key, so a run index spanning two tiers must be
    costed cell by cell rather than at a blended rate. Holding the `TierUsage`
    list rather than pre-summed tokens is what lets `cost` reuse the one
    implementation of the cost formula instead of restating it.
    """

    run_id: int | None
    by_tier: list[TierUsage]

    @property
    def n_runs(self) -> int:
        return sum(u.n_runs for u in self.by_tier)

    @property
    def n_calls(self) -> int:
        return sum(u.n_search_calls for u in self.by_tier)

    @property
    def input_tokens(self) -> int:
        return sum(u.input_tokens for u in self.by_tier)

    @property
    def cache_read(self) -> int:
        return sum(u.cache_read for u in self.by_tier)

    @property
    def output_tokens(self) -> int:
        return sum(u.output_tokens for u in self.by_tier)

    @property
    def hit_rate(self) -> float | None:
        return self.cache_read / self.input_tokens if self.input_tokens else None

    def cost(self, pricing: Pricing) -> float | None:
        """Total cost, or None if ANY tier in this slice is unpriced.

        None rather than a partial sum: a slice costed from half its tiers would
        understate, and understating the cost of a run is the wrong direction to
        err when the whole point is arguing a run can be cut.
        """
        costs = [u.cost(pricing.for_tier(_tier_key(u.tier))) for u in self.by_tier]
        return None if any(c is None for c in costs) else sum(costs)

    def fee(self, pricing: Pricing) -> float:
        """Just the per-call search fee. Strictly linear in runs, so it is the
        part of a run-count cut that carries no caching caveat."""
        return sum(
            u.search_fee(pricing.for_tier(_tier_key(u.tier)), u.n_search_calls)
            for u in self.by_tier
        )


def _tier_key(tier: str) -> str | None:
    """`<unset>` means no service_tier was reported; Pricing treats None as standard."""
    return None if tier == "<unset>" else tier


def usage_by_run(conn: sqlite3.Connection) -> list[RunSlice]:
    """Token and call volume per (run index, tier).

    Mirrors `metrics.tier_usage`'s two rules, for the same reasons: calls whose
    `run_pk` is NULL are bucketed rather than inner-joined away, because a billed
    call vanishing from a cost figure understates it; and query entries are
    counted directly rather than floored at one per call.

    Calls are bucketed by **`search_calls.run_id`**, not the joined `runs.run_id`.
    Both columns exist and they diverge exactly when `run_pk` failed to resolve
    (a DUP_RUN whose lookup missed): the call still carries its own run index, so
    grouping on the call's copy keeps it in the run slice it actually belongs to
    instead of dumping it in `unparsed`.

    Its tier, though, comes from the run row it could not reach, so it lands under
    `<unset>` -- and `Pricing.for_tier(None)` costs that at STANDARD, deliberately,
    on the documented inference that an absent tier means the API default was used.
    So such a call is costed, not dropped and not reported unpriced. That is the
    right default here for the same reason it is in `tier_usage`: a billed call
    vanishing from a cost figure understates it, and understating is the wrong
    direction when the argument is that runs can be cut.
    """
    runs = {
        (r["run_id"], r["tier"]): r
        for r in conn.execute(
            """
            SELECT run_id,
                   COALESCE(service_tier, '<unset>') AS tier,
                   COUNT(*)                          AS n_runs,
                   COALESCE(SUM(input_tokens), 0)    AS input_tokens,
                   COALESCE(SUM(cache_read), 0)      AS cache_read,
                   COALESCE(SUM(output_tokens), 0)   AS output_tokens
            FROM runs GROUP BY run_id, tier
            """
        )
    }
    calls = {
        (r["run_id"], r["tier"]): (r["n_calls"], r["n_entries"])
        for r in conn.execute(
            """
            SELECT c.run_id                             AS run_id,
                   COALESCE(r.service_tier, '<unset>')  AS tier,
                   COUNT(*)                             AS n_calls,
                   COALESCE(SUM(
                       (SELECT COUNT(*) FROM query_instances q
                        WHERE q.search_call_id = c.id)
                   ), 0)                                AS n_entries
            FROM search_calls c LEFT JOIN runs r ON r.id = c.run_pk
            GROUP BY c.run_id, tier
            """
        )
    }

    keys = set(runs) | set(calls)
    slices: dict[int | None, list[TierUsage]] = {}
    for run_id, tier in sorted(keys, key=lambda k: (k[0] is None, k[0], k[1])):
        row = runs.get((run_id, tier))
        n_calls, n_entries = calls.get((run_id, tier), (0, 0))
        slices.setdefault(run_id, []).append(
            TierUsage(
                tier=tier,
                n_runs=row["n_runs"] if row else 0,
                input_tokens=row["input_tokens"] if row else 0,
                cache_read=row["cache_read"] if row else 0,
                output_tokens=row["output_tokens"] if row else 0,
                n_search_calls=n_calls,
                n_query_entries=n_entries,
            )
        )
    return [
        RunSlice(run_id, by_tier)
        for run_id, by_tier in sorted(slices.items(), key=lambda kv: (kv[0] is None, kv[0]))
    ]


def run_count_distribution(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """`(runs per merchant, how many merchants)`, from the LOG side.

    The log side is what carries `usage_metadata`, so it is what cost scales
    with. The output side has its own run count over a different merchant
    population (19,349 vs 19,269), and dividing cost by that one would be a
    denominator error -- the same trap `SubqueryPicture` exists to flag.
    """
    return [
        (r["n"], r["merchants"])
        for r in conn.execute(
            "SELECT n, COUNT(*) AS merchants FROM ("
            "  SELECT se10, COUNT(*) AS n FROM runs GROUP BY se10) "
            "GROUP BY n ORDER BY n"
        )
    ]


@dataclass(frozen=True)
class ThirdRunPicture:
    """Whether run_2 can change anything, per question.

    `redundant` and `decisive` partition the 3-run cases by whether the first two
    runs already agreed, which is the only thing that determines whether a third
    run can move the majority at all.
    """

    qnum: int
    n_three: int
    n_redundant: int
    n_decisive: int
    n_no_majority: int
    n_pairs: int
    n_pairs_agree: int

    def _rate(self, num: int, den: int) -> float | None:
        return num / den if den else None

    @property
    def redundant_rate(self) -> float | None:
        """Share of 3-run cases where run_2 could not have changed the outcome."""
        return self._rate(self.n_redundant, self.n_three)

    @property
    def no_majority_rate(self) -> float | None:
        """Three runs, three different answers -- nothing to vote on."""
        return self._rate(self.n_no_majority, self.n_three)

    @property
    def pair_agreement(self) -> float | None:
        """Clean pairwise agreement, on merchants with EXACTLY two runs.

        This is the figure `scorecard.agreement_rate` is often read as but is
        not: that one requires every run to match, so it decays mechanically with
        run count. Conditioned here, it is comparable across questions.
        """
        return self._rate(self.n_pairs_agree, self.n_pairs)


def third_run_picture(conn: sqlite3.Connection, answer_expr: str) -> list[ThirdRunPicture]:
    """Per qnum, whether the third run is redundant, decisive, or inconclusive.

    Compares the three LOWEST run_ids per (output_id, qnum) via ROW_NUMBER, so a
    merchant with a gap in its run numbering is still compared on its first three
    actual runs rather than on run_0/1/2 by name.

    Normalization goes through `_norm_text`, the same function the scorecard uses
    to decide whether two runs said the same thing. A second definition of "the
    same answer" is precisely the drift this project has been bitten by before.
    A NULL answer normalizes to the empty string, so two runs that both answered
    NULL correctly count as agreeing.
    """
    conn.create_function("coa_norm", 1, lambda v: _norm_text(v or ""))
    return [
        ThirdRunPicture(
            qnum=r["qnum"],
            n_three=r["n_three"] or 0,
            n_redundant=r["redundant"] or 0,
            n_decisive=r["decisive"] or 0,
            n_no_majority=r["no_majority"] or 0,
            n_pairs=r["n_pairs"] or 0,
            n_pairs_agree=r["pairs_agree"] or 0,
        )
        for r in conn.execute(
            f"""
            WITH ranked AS (
                SELECT output_id, qnum, coa_norm({answer_expr}) AS a,
                       ROW_NUMBER() OVER (PARTITION BY output_id, qnum
                                          ORDER BY run_id) AS rn
                FROM answers
                WHERE qnum IS NOT NULL AND run_id IS NOT NULL
            ), trio AS (
                SELECT output_id, qnum, COUNT(*) AS n_runs,
                       MAX(CASE WHEN rn = 1 THEN a END) AS a0,
                       MAX(CASE WHEN rn = 2 THEN a END) AS a1,
                       MAX(CASE WHEN rn = 3 THEN a END) AS a2
                FROM ranked GROUP BY output_id, qnum
            )
            SELECT qnum,
                   SUM(n_runs >= 3)                                  AS n_three,
                   SUM(n_runs >= 3 AND a0 =  a1)                     AS redundant,
                   SUM(n_runs >= 3 AND a0 <> a1
                                   AND (a2 = a0 OR a2 = a1))         AS decisive,
                   SUM(n_runs >= 3 AND a0 <> a1
                                   AND a2 <> a0 AND a2 <> a1)        AS no_majority,
                   SUM(n_runs =  2)                                  AS n_pairs,
                   SUM(n_runs =  2 AND a0 = a1)                      AS pairs_agree
            FROM trio GROUP BY qnum ORDER BY qnum
            """
        )
    ]


def _money(value: float | None) -> str:
    return "UNPRICED" if value is None else f"${value:,.2f}"


def _pct(value: float | None) -> str:
    return "    -" if value is None else f"{value * 100:5.1f}"


def render_run_economics(
    slices: list[RunSlice], dist: list[tuple[int, int]], pricing: Pricing
) -> str:
    """What the marginal run costs, with the linear and sub-linear parts split."""
    if not slices:
        return "RUN ECONOMICS\n  (no runs ingested)"

    n_runs = sum(s.n_runs for s in slices)
    n_merchants = sum(m for _, m in dist)
    all_costs = [s.cost(pricing) for s in slices]
    out = ["RUN ECONOMICS  (cost scales with runs — this lever needs NO attribution)"]
    # Keyed on whether anything ACTUALLY came out unpriced, not on
    # `pricing.is_verified`. That flag only inspects tiers the config names, so a
    # tier present in the DATA but absent from config.yaml -- the real failure
    # mode, and how `default` reported $0.00 across the whole corpus -- passes it
    # while every cost in the table below reads UNPRICED with no explanation.
    if any(c is None for c in all_costs):
        unpriced = sorted(
            {
                u.tier
                for s in slices
                for u in s.by_tier
                if u.cost(pricing.for_tier(_tier_key(u.tier))) is None
            }
        )
        out += [
            "  *** UNVERIFIED PRICING ***",
            f"  tiers with no usable rates: {', '.join(unpriced)}",
            "  A run index spanning ANY unpriced tier reports UNPRICED rather than a",
            "  partial sum — understating the cost of a run is the wrong way to be wrong",
            "  when the argument is that the run can be cut. Call counts are unaffected.",
        ]
    out += [
        f"  runs               {n_runs:,} over {n_merchants:,} merchants"
        + (f"  ({n_runs / n_merchants:.2f} each)" if n_merchants else ""),
        "  distribution       " + (", ".join(f"{n} run(s): {m:,}" for n, m in dist) or "none"),
        "",
        "  by run index       (run_1+ reuse run_0's prompt, so their input is cache)",
        f"    {'':<8}{'runs':>9}{'calls':>10}{'input tok':>14}{'cached':>8}"
        f"{'output tok':>13}{'cost':>13}",
    ]
    for s in slices:
        label = f"run_{s.run_id}" if s.run_id is not None else "unparsed"
        out.append(
            f"    {label:<8}{s.n_runs:>9,}{s.n_calls:>10,}{s.input_tokens:>14,}"
            f"{(f'{s.hit_rate:.0%}' if s.hit_rate is not None else 'n/a'):>8}"
            f"{s.output_tokens:>13,}{_money(s.cost(pricing)):>13}"
        )

    cut = [s for s in slices if s.run_id is not None and s.run_id >= KEEP_RUNS]
    if not cut:
        out += [
            "",
            f"  No merchant has more than {KEEP_RUNS} runs, so there is no marginal run to cut.",
        ]
        return "\n".join(out)

    costs = [s.cost(pricing) for s in cut]
    cut_cost = None if any(c is None for c in costs) else sum(costs)
    # `total` is None unless EVERY slice priced. Summing only the priced ones and
    # then dividing by it would report a share of a partial denominator as a
    # "% of total" -- inflating the lever in the one document where overstating a
    # saving is least forgivable.
    total = None if any(c is None for c in all_costs) else sum(all_costs)
    cut_fee = sum(s.fee(pricing) for s in cut)
    cut_runs = sum(s.n_runs for s in cut)
    cut_calls = sum(s.n_calls for s in cut)

    out += [
        "",
        f"  CUTTING TO {KEEP_RUNS} RUNS PER MERCHANT",
        f"    removes          {cut_runs:,} runs ({cut_runs / n_runs:.1%} of all runs), "
        f"{cut_calls:,} billed calls",
        f"    saves            {_money(cut_cost)}"
        + (
            f"  ({cut_cost / total:.1%} of total)"
            if cut_cost is not None and total
            else "  (share of total needs every tier priced)"
            if cut_cost is not None
            else ""
        ),
        f"      search fee     {_money(cut_fee)}  — LINEAR in runs, and attribution-free",
        "",
        "  Reading this:",
        "    * The search fee and output tokens fall exactly in proportion to runs cut.",
        "      Those are the trustworthy part of the figure.",
        "    * INPUT does not. The run being cut is the one whose prompt was already a",
        "      cache hit at a tenth of the input rate, so it is the CHEAPEST run on input",
        "      and the saving there is far below its share of runs.",
        "    * run_0 and run_1 are kept, so repeat-run prompt caching still works — this",
        "      lever and the caching lever do not cancel, but nor do they simply add.",
    ]
    return "\n".join(out)


def render_third_run(
    rows: list[ThirdRunPicture], dist: list[tuple[int, int]] | None = None, top: int = 10
) -> str:
    """Whether the third run arbitrates or rubber-stamps, and what that implies.

    `dist` is the LOG-side run distribution, passed in only so this block can say
    when the two populations disagree. They are different measurements: cost above
    counts runs in `runs` (from `logs/jsonl/`), agreement here counts runs in
    `answers` (from `output/`). On the real corpus that is 10,943 merchants with 3
    log runs against 11,720 records with 3 output runs -- a 777 gap. Printing the
    two blocks adjacently without saying so invites multiplying a rate from one
    population by a cost from the other, which is the denominator error
    `SubqueryPicture` already exists to flag elsewhere.
    """
    if not rows:
        return "THIRD-RUN VALUE\n  (no answers ingested)"

    n_three = sum(r.n_three for r in rows)
    n_pairs = sum(r.n_pairs for r in rows)
    if not n_three:
        return (
            "THIRD-RUN VALUE\n"
            f"  No (merchant, question) has 3+ runs — {n_pairs:,} have exactly 2.\n"
            "  Nothing here can be measured; the run-count cut has no third run to remove."
        )

    redundant = sum(r.n_redundant for r in rows)
    decisive = sum(r.n_decisive for r in rows)
    none_maj = sum(r.n_no_majority for r in rows)
    agree = sum(r.n_pairs_agree for r in rows)

    # The record count is the MAX per-question case count, not the mean: a record
    # missing answers for some questions contributes to fewer than all of them, so
    # dividing the total by the question count floors it. On the real corpus every
    # question had 11,720 and the two agree; on a corpus with short runs they do
    # not, and the mean would silently understate the population.
    n_records = max(r.n_three for r in rows)
    uniform = n_three == n_records * len(rows)
    out = [
        "THIRD-RUN VALUE  (can run_2 change the majority run_0+run_1 produced?)",
        f"  3-run cases        {n_three:,} (record, question) pairs"
        + (
            f" = {n_records:,} records x {len(rows)} questions"
            if uniform
            else f" over {n_records:,} records (not every record answers all {len(rows)} questions)"
        ),
    ]
    # The populations behind the cost block above and this one are not the same.
    # SUM every bucket at 3-or-more, not the first one. `n_records` counts records
    # with >= 3 runs, so taking only the exactly-3 bucket would compare different
    # definitions and report a phantom gap the moment a merchant has four runs.
    log_three = sum(m for n, m in (dist or []) if n >= 3)
    if dist and log_three != n_records:
        out += [
            f"  POPULATION         {n_records:,} records here (from output/) vs "
            f"{log_three:,} merchants with",
            f"                     3 runs above (from logs/jsonl/) — a gap of "
            f"{abs(n_records - log_three):,}.",
            "                     Cost above is the LOG side; these rates are the OUTPUT side.",
            "                     Multiplying one by the other crosses populations; the cost",
            "                     of a third run is a FLOOR, since runs with answers but no",
            "                     log carry no tokens.",
        ]
    out += [
        f"    redundant        {redundant:,} ({redundant / n_three:.1%}) — first two agreed, "
        f"so run_2 CANNOT change it",
        f"    decisive         {decisive:,} ({decisive / n_three:.1%}) — first two differed, "
        f"run_2 broke the tie",
        f"    no majority      {none_maj:,} ({none_maj / n_three:.1%}) — all three differed, "
        f"nothing to vote on",
    ]
    if n_pairs:
        out.append(
            f"  2-run cases        {n_pairs:,}, of which {agree:,} agree "
            f"({agree / n_pairs:.1%} pairwise)"
        )
        out.append("                     This is the CLEAN agreement figure: conditioned on run")
        out.append("                     count, unlike scorecard `agree`, which requires every run")
        out.append("                     to match and so decays as run count rises.")

    worst = sorted(rows, key=lambda r: (-(r.no_majority_rate or 0.0), r.qnum))[:top]
    out += ["", "  LEAST CONCLUSIVE QUESTIONS (highest share of 3-run cases with no majority)"]
    out.append("     Q    3-run   redund  decisive  no-maj   pair-agree")
    for r in worst:
        out.append(
            f"  {r.qnum:>4} {r.n_three:>8,}   {_pct(r.redundant_rate)}     "
            f"{_pct(r._rate(r.n_decisive, r.n_three))}   {_pct(r.no_majority_rate)}      "
            f"{_pct(r.pair_agreement)}"
        )

    out += [
        "",
        "  Reading this:",
        "    * HIGH redundant -> the third run mostly confirms a decision already made.",
        "      That is the case for cutting it: it is buying a rubber stamp.",
        "    * HIGH decisive  -> the first two runs disagree, so run_2 is arbitrating.",
        "      This is NOT evidence the run earns its keep. It is a tie-break between two",
        "      answers that already contradict each other, decided by a third draw from the",
        "      same unreliable process. Cutting it would leave no majority at all — but",
        "      keeping it launders a coin flip into a finding.",
        "    * HIGH no-majority -> three runs, three answers. The pipeline is not",
        "      determining this question at all, and more runs will not fix that.",
    ]
    return "\n".join(out)
