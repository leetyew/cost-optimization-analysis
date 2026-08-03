# Project: Web-Search Cost Optimization Analysis (`coa`)

## Domain

Another team runs a merchant fraud-screening pipeline. Per merchant it calls OpenAI web
search (model `gpt 5.4`) with a **fixed 48-question prompt**, several runs per merchant,
then majority-votes the answers. Average ~14 web searches per merchant per run.

**This repo does not run that pipeline.** It parses that team's logs and outputs to answer
one question: *which of those searches can be cut, and what does that save?*

Scale, measured on the real corpus: **19,269 merchants, ~2 runs each, 694,695 web-search
calls** — 592,710 `search` (85.3%), 87,854 `open_page` (12.6%), 14,131 `find_in_page` (2.0%).
Stream everything; never assume a file fits in memory. Stdlib `sqlite3` handles all storage
and analysis at this scale.

## Source layout (`data/cost_optimization/`)

| Tree | Content |
|---|---|
| `logs/jsonl/*.jsonl` | **The authoritative source.** One merchant per line: `{se10: {run_k: {usage_metadata, response_reasoning, web_search_calls[]}}}` |
| `input/*.jsonl` | Merchant detail records -> `merchants` + `pii_terms` |
| `output/*.jsonl` | Answers, citations, votes for the 48-question prompt |

`logs/*.log` also exists — a text log of the same events. **It is deliberately not parsed.**
Its action-type tallies matched `logs/jsonl` *exactly* (592,710 / 87,854 / 14,131), so it is
pure redundancy except for timestamps, and nothing in the deliverables needs a clock:
position within a run comes from array order in `web_search_calls`. Parsing it cost ~1,100
lines of pairing state machine, comma-split repair, and burst-based run attribution, all to
reconstruct what the JSON states outright. If time-of-day or latency analysis is ever wanted,
the files are still on disk.

## Core invariants (never violate)

1. **Never crash on malformed input. Never silently drop it either.** Every unparseable
   thing gets an `anomalies` row with full raw context, and the record is still stored with
   a confidence marker. Non-zero anomaly counts are the expected steady state, not a bug.
2. **Retain raw text always** — `search_calls.raw_json`, `merchants.raw_json`. This is what
   makes `coa reparse` possible (re-extract fields from SQLite without re-reading the corpus).
3. **Never commit merchant PII.** `coa.sqlite`, `reports/`, `data/` are gitignored at the
   repo root. Check before every commit.
4. **Pricing constants stay `null`** until an operator fills them from the billing
   dashboard. Reports print an `UNVERIFIED PRICING` banner while they are null, and still
   show call-count-based relative shares, which need no prices.
5. **Nothing unverifiable goes in a report.** The output is an argument aimed at another
   team's budget; an unmeasurable heuristic is the weakest link. Prefer an exact, narrower
   claim over an inferred, broader one — and label every heuristic that does survive.

## Cost model

`usage_metadata` gives measured token volume per run, so cost is metered rather than
inferred from call counts. Two subset relationships are load-bearing and easy to get
backwards — **verified on the corpus (`input + output == total` holds exactly) and in
OpenAI's docs**:

- `cache_read` is **inside** `input_tokens`, billed at the discounted cached-input rate.
- `reasoning` is **inside** `output_tokens`, billed at the output rate.

```
cost = (input_tokens - cache_read) * input_rate
     +  cache_read                * cached_input_rate
     +  output_tokens             * output_rate        # reasoning already inside
     +  n_search_calls            * fee_per_1k / 1000
```

Adding `reasoning` or `cache_read` as separate terms double-counts and inflates the
baseline — the worst possible error in a document arguing another team should spend less.
`TOKEN_SUM_MISMATCH` fires if the arithmetic ever stops holding.

**The billing unit is UNRESOLVED, and it is a ~4x swing.** Treat every absolute cost figure
as provisional until the dashboard reconciliation below is done.

- *Structurally*, one `web_search_call` is one item with one id, one status, and one
  `queries` array. `queries` sits inside a single call.
- *For billing*, OpenAI support (developer forum, not official docs) states the tool "can
  sometimes run multiple internal sub-searches for a single prompt… each one is counted as
  a separate, billable call", with a reported case billing ~2.5x the visible invocations.
  A constant `len(queries) == 4` is consistent with those sub-searches being visible here.

| If billing is | Billed searches | At $10/1K |
|---|---|---|
| per visible call | 592,710 | ~$5.9k |
| per `queries` entry | ~2.37M | ~$23.7k |

An earlier note here claimed this was settled because 592,710 / 19,269 / ~2 runs ≈ 14 per
run matches the operator's "~14 searches per run". **That reasoning is circular** if the ~14
figure was itself derived from these logs — it confirms 14 *visible calls*, not 14 *billed
searches*.

**RESOLVE THIS FIRST:** take one day's call count and reconcile against the billing
dashboard. The dashboard reports invocations, not billed sub-searches, so compare against
the *charge*, not the displayed call count. Until then `is_billed_query` remains on the
singular `query` — one row per call, shares summing to 100% — because that is the only
attribution that is internally consistent; it is a stated convention, not a verified fact.

Only `action_type: search` is believed to carry the per-call fee; `open_page` and
`find_in_page` consume tokens but no call fee. Also unverified.

**`service_tier` is part of the pricing key**, not a detail: flex bills near batch rates and
priority roughly 2x standard — a ~4x spread. `config.yaml` holds rates per tier.

Filled so far (operator-supplied 2026-08-03, `standard` only):

| | per 1M tokens |
|---|---|
| input | $2.50 |
| cached input | $0.25 (1/10th — matches the published discount) |
| output | $15.00 |
| search calls | $10.00 per 1K |

`flex` and `priority` stay null deliberately. Deriving them from published ratios would be
inventing numbers, so their runs report as UNPRICED and are excluded from the total rather
than borrowing standard's rates. A run with **no** `service_tier` is costed at standard, on
the inference that absent means the API default was used — `coa analyze` labels that.

`coa analyze` reports cost as a **range**, because the billing unit is unresolved: the low
bound bills per visible call, the high bound per `queries` entry. The spread is the open
question, not rate uncertainty.

Two cost levers that need **no change to search behaviour at all**:

- **Service tier.** Batch fraud screening is exactly what flex exists for. Visible in the
  ingest summary from the first run.
- **Repeat-run caching.** See below. `coa analyze` measures it.

### Prompt caching (operator-confirmed structure)

The 48 questions carry merchant values **inline** — `"...what type of building is at
<zip>..."` — rather than in a separate block. That is decisive, because OpenAI caches on
**exact prefix** only:

- **Cross-merchant caching is structurally unavailable.** The first question containing a
  merchant field ends the shared prefix; everything after it differs per merchant. And
  caching needs **≥1024 tokens** with *no partial credit*, so a short static preamble caches
  nothing whatsoever. Do not report a low hit rate here as waste — it is the expected
  consequence of the prompt shape.
- **Same-merchant repeat runs are the live lever.** `run_1+` reuse `run_0`'s byte-identical
  prompt, so this works regardless of inline placeholders. It depends only on the two runs
  falling inside the cache TTL (30 min on current models). With ~2 runs/merchant this caps
  around half of all input tokens, and the fix is scheduling, not prompt surgery.

`coa analyze` splits the hit rate by `run_id` precisely to tell these apart, and measures
the actual shared prefix against the 1024-token floor.

Restructuring the prompt (static questions first, merchant values in a tail block) would
unlock cross-merchant caching, but it is **not answer-preserving by construction** and needs
A/B validation before it is recommended. Do not put it in a report as a free win.

## The air-gap (shapes everything)

Real data lives in an environment Claude Code **never sees**. Development happens entirely
against synthetic fixtures. Reality reaches this repo through exactly **two** channels, and
both are narrow:

1. **The anomaly loop.** The operator runs `coa anomalies show CODE`, pastes the output back
   into a Claude Code session, the parser gets patched, and the new case joins the fixture
   generator as a regression.
2. **The operator saying so.** Format facts stated in conversation — "the answer is 1-5,
   defaulting to 3" — with no anomaly attached. This channel carries the highest-value
   information in the project and leaves no artifact of its own.

### Capture rule (channel 2 has no memory of its own)

An operator-relayed fact is **unrecoverable** once the transcript is gone: it cannot be
re-derived from the code, the fixtures, or git history, and nobody can go and look it up.
So before the session ends, every such fact lands in **three** places:

- **"Known real-data format facts" below** — the durable, shared record.
- **`tests/fixtures/gen_fixtures.py`** — as a rendered shape, so a parser that regresses
  against it fails a test rather than a production run.
- **Session memory** (`memory/` + `MEMORY.md`) — provenance and confidence: what is
  confirmed, what is still assumed, and what would settle it.

A fact recorded in only one of the three is one refactor away from being lost.

### Consequences

- `tests/fixtures/gen_fixtures.py` **is** reality during development. Over-invest in it.
  Adding a newly-discovered case must be a ~3-line diff plus a golden count.
- The anomaly CLI output is a UX surface, not a formatting detail. If it emits 5000 lines,
  the loop breaks. Keep it deduped, capped, and paste-ready.
- Assume reality contradicts at least some format assumption in `PLAN.md`. Budget for it.
- **Prefer a parser that tolerates every candidate shape and reports which one it saw**
  over one that asks the operator to confirm a shape up front. A counter in the ingest
  summary settles the question from data in one run; a question costs a round-trip and can
  be answered from a faulty memory. `evidence shapes` in the summary exists for exactly this.

## Known real-data format facts

Operator-relayed (channel 2), not derivable from anything in this repo. The fixture
generator renders all of it; `src/coa/outputs.py` parses it.

| Fact | Status |
|---|---|
| Many of the 48 questions answer on a **1-5 scale**, defaulting to **3 when evidence is insufficient** | confirmed |
| Evidence is returned **only when the answer is ≤ 3**; otherwise NULL | confirmed |
| Free-text questions (registered address, building type) return **`value \| NULL`** — a literal `NULL` is a first-class *answer*, not only an evidence state | confirmed |
| The answer-format instruction is part of the **user prompt**, attached to the questions | confirmed |
| `logs/jsonl/*.jsonl` is the authoritative call source; `logs/*.log` is redundant except for timestamps | confirmed — action counts matched exactly |
| A call's `queries` is a **fixed-length set of sub-queries within one call**, NOT cumulative session history | confirmed — length constant, members differ between consecutive calls |
| Whether each `queries` entry bills as its own search | **UNRESOLVED, ~4x cost swing** — see "Cost model"; settle on the billing dashboard before publishing any figure |
| `web_search_call.action.sources` would link citations to calls, but is opt-in (`include=[...]`) and absent here | confirmed from API docs — per *call*, so it cannot reach an individual `queries` entry. **Low value: see "The decision lever is the question"** |
| `input_tokens + output_tokens == total_tokens`, so `cache_read` and `reasoning` are **subsets** | confirmed — holds across the corpus |
| `response_reasoning` has keys `id`/`type`/`summary`/`content`, but reasoning summaries were **not opted into**, so content is empty | confirmed — field not stored, only its token count |
| `service_tier` appears per run and varies | confirmed — pricing is keyed by tier |
| Whether a non-`completed` call is still billed | **unconfirmed** — `CALL_STATUS_NOT_COMPLETED` counts them; settle on the billing dashboard |
| When no evidence is required, whether the line reads `Evidence. NULL`, a bare `Evidence.`, or is **omitted entirely** | **unconfirmed** — all three parsed; see `evidence shapes` in the ingest summary |
| Whether a scale answer is a bare digit or a digit followed by prose | **unconfirmed** — `answer_text` stored verbatim, scale value derived, never assumed |
| Whether `answer_dict` holds their parse of the same prose or a normalized form | **unconfirmed** — `agree_with_dict` measures it; `ANSWER_PARSE_MISMATCH` is aggregated per (record, run) so a systematic difference cannot flood |

### The decision lever is the question, not the query

Easy to lose sight of, and it demotes a whole line of analysis:

- The other team controls **the 48 questions**. The `queries` inside a call are generated by
  the model from those questions — nobody can prune sub-query #3, and no report can
  recommend it.
- `citation -> question` is already **exact**, via `citation_evidence.a_key`. The chain
  needed for a keep/drop recommendation is therefore already complete.

So `web_search_call.action.sources` is **diagnostic, not decisional**. It would explain
which call surfaced a cited URL; it would not change which question gets cut. Do not spend
the other team's goodwill asking them to enable it before the question-level analysis is
done — and do not present it as a prerequisite for the headline finding.

This also reorders the deliverables. **Per-question rates (PLAN.md §6.5 — NULL rate,
default-3 rate, citation rate, agreement) are the primary finding**, because they are exact
and they map onto the only lever that exists. Archetypes (§6.2, P3) stay valuable for the
*consolidation* argument — "these two questions provoke the same search, merge them" — but
they are not the path to the *deletion* argument.

Two consequences that are easy to get backwards:

- **`is_null` keys on the answer, never on the evidence.** Evidence being NULL is the
  *specified outcome* for a scale answer above 3 — it means the search found nothing
  adverse, which is a good result, not a missing one. Reading it as a null answer inverts
  the headline metric.
- **The drop-candidate signal is "the answer carried no information"**, which unifies both
  regimes: `answer == NULL` for text questions, `answer == 3 with NULL evidence` for scale
  ones. Both are exactly measurable, so the finding survives the "nothing unverifiable"
  invariant. P2 stores the raw material; P4 computes the rate.

## Stack

- Python **3.10+** (the operator's analysis environment runs 3.12; the pin follows the
  deployment, not an aspiration). Stdlib `sqlite3` + `zipfile` + `re` + `csv`.
- Dependencies: `pyyaml`, `pytest`, `ruff`. **That is the whole list.** Adding one requires
  a stated trigger — see the reversal triggers in the plan; heavier options
  (rapidfuzz/sklearn/sentence-transformers, dnspython/whois, pandas/duckdb) were all
  deliberately deferred behind measurements, not forgotten.
- Layout: `src/coa/{config,anomalies,db,weblogs,outputs,inputs,normalize,metrics,report,cli}.py`

## Architectural rules

- **Parsers take `(src_name, lines_iterable)` — never a path.** `cli.py` owns all path and
  zip walking. This keeps `weblogs.py` / `outputs.py` / `inputs.py` pure over line iterators so
  they can be unit-tested against a 6-line in-memory string.
- **Cost attribution:** a billed call's archetype comes from the singular `query` field.
  The plural `queries` list is for parse repair and redundancy analysis only, never cost.
- Every stored row carries `src_file` + `src_line` provenance.

## CodeGraph

This project has a CodeGraph MCP server (`codegraph_*` tools) — a tree-sitter knowledge
graph of every symbol and edge. Use it for structural questions (what calls what, where is
X defined, what breaks if I change Z) instead of grep. See the global CLAUDE.md for the
tool-selection table.

---

## Linting and Formatting

After writing or editing Python files, **always** run before tests or commits:

```bash
ruff format . && ruff check --fix .
```

**Self-correction rule**: if a `ruff` run reformats or errors, record the specific rule here
so it does not repeat. Goal is zero wasted formatting round-trips.

- Line length: 100 (set in `pyproject.toml`).

---

## Rules for Python Comments
- Rule of Thumb:
  - Every .py should have a header comment or module docstring unless intent is obvious
  - Every function/class should have a docstring
  - Every 20-30 lines of logic should have at least one comment explaining intent
  - For every tricky block, explain the `why`, not `what`
- When you should comment:
  - Non-obvious logic: explain why something is done
  - Complex algorithm: summarize approach in plain language
  - Function/Module purpose: describe role, inputs, outputs, and side effects
- When you should not comment:
  - Do not state the obvious
  - Do not duplicate docstrings or variable names

---

## Rules for Python Type Hints
- Always have type hints for function parameters and return types
- Often for dataclass/class attributes
- Sometimes for tricky variables where type is not obvious
- Never for obvious local variables

---

## Rules of git commit
  - If doing git commit, do not include claude or antropic stuffs in message
  - The format should follow:
    <type>(<scope>): <subject line - max 50-72 chars>

    <why paragraph - 2-4 sentences explaining context and problem>

    <how paragraph - 2-4 sentences explaining solution at high level>

    CHANGE: <1-2 sentences summarizing what changed>

    <optional: BREAKING CHANGE: sentence if applicable>
  - Put BREAKING CHANGE: in the body with a clear sentence, bullets optional
  - So: subject line clean; body explanatory; bullets only if they improve readability

---

## Important Rules
- Apply YAGNI principle (You Aren't Gonna Need It)
- Keep code clean and scalable
- Provide clean, maintainable fixes instead of minimal patches
- Avoid naming conventions like "enhanced", "new", "latest", "better", "best"

---

### File size
- **Primary criterion**: each `.py` file has clear **single responsibility** and stays **readable**. Line count is a heuristic, not the criterion. Don't split a file purely because it crossed 500 lines, and don't defer a split that's actually needed because the file is still under 500.
- **Typical range: 200–500 lines.** Most files land here naturally when SRP is honored — but the range *follows from* SRP, not the other way around.
- **Below 200 lines** is fine only when a distinct self-contained concern justifies it (dataclass-only module, tiny CLI shim, small facade `__init__.py`). Otherwise, merge small fragments that share a concern rather than proliferating tiny files.
- **Above 500 lines** is a flag to look closer, not a violation. Split when the file has actually accreted unrelated concerns or become hard to navigate; leave it alone when it's doing one thing well even if long.
- Quick check (lists files worth reviewing — being on this list is NOT itself a reason to split):
  ```bash
  find src -name "*.py" -not -path "*/__pycache__/*" -exec wc -l {} + | awk '$1 > 500 {print}'
  ```
