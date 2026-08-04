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

### The billing unit is RESOLVED (operator-supplied charge, 2026-08-03)

The operator reported the web-search line item for the corpus period: **$6,946.95**. At the
$10.00/1K rate that is exactly **694,695 billed units** — to the cent, and to the unit, the
total number of `web_search_calls` in the corpus:

```
592,710 search + 87,854 open_page + 14,131 find_in_page = 694,695 calls
694,695 x $0.01                                         = $6,946.95   EXACT
```

Two facts follow, and they point in opposite cost directions:

1. **Billing is per visible call, NOT per `queries` entry.** The ~2.37M `queries` entries
   would have billed ~$23,700 — off by 3.4x. The OpenAI developer-forum claim about
   internally-billed sub-searches does not apply to this account. `is_billed_query` on the
   singular `query` is now a verified convention, not a stated one.
2. **All three action types carry the per-call fee.** `open_page` and `find_in_page` are
   billed exactly like `search`. Charging only `search` understates the fee by
   101,985 calls = **$1,019.85 (+17.2%)**.

The alternatives require rates nobody publishes, which is what makes this decisive rather
than merely consistent — two unknowns and one equation, but only one solution lands on both
a round published rate and an independently measured integer:

| Hypothesis | Implied rate | Verdict |
|---|---|---|
| all calls bill | **$10.0000/1K** | exact, and the published rate |
| only `search` bills | $11.7207/1K | not a list price |
| per `queries` entry | $2.9312/1K | not a list price |

Standing assumption: the $6,946.95 covers exactly the corpus period and nothing else. The
exactness makes contamination implausible — an unrelated window would have to land on
694,695 units. `CALL_STATUS_NOT_COMPLETED` is 0 on the corpus (all 694,695 completed), so
non-completed-call billing cannot be confounding the match — and has no instances to settle.

Because the whole corpus runs on **one** service tier (`default`), this reconciliation says
nothing about whether the call fee varies by tier. Do not generalise it to `flex`/`priority`.

### Rates

`config.yaml` holds rates per tier (`service_tier` is part of the pricing key: flex bills
near batch rates and priority roughly 2x standard — a ~4x spread).

Filled so far (operator-supplied 2026-08-03, `standard` only):

| | per 1M tokens |
|---|---|
| input | $2.50 |
| cached input | $0.25 (1/10th — matches the published discount) |
| output | $15.00 |
| search calls | $10.00 per 1K, on **every** action type |

`flex` and `priority` stay null deliberately. Deriving them from published ratios would be
inventing numbers, so their runs report as UNPRICED and are excluded from the total rather
than borrowing standard's rates. **The real corpus needs neither**: 100% of runs report
`service_tier: "default"`, which is the OpenAI API's own name for the standard tier, and the
billing reconciliation above confirms its call fee is the $10/1K standard rate. A run with
**no** `service_tier` is still costed at standard, on the inference that absent means the API
default was used — `coa analyze` labels that.

`coa analyze` reports cost as a **single figure**, not a range. The range existed only to
straddle the unresolved billing unit; the reconciliation collapsed it.

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
- **`coa doctor` is the diagnostic surface.** The operator hand-types across the air gap, so
  anything worth asking about an ingest belongs in that one command rather than in ad-hoc SQL
  sent over chat. It reports row counts as *positive confirmation*: an absent anomaly is
  ambiguous (clean data, or a detector that never ran), and only counts disambiguate.
- Assume reality contradicts at least some format assumption in `PLAN.md`. Budget for it.
- **Prefer a parser that tolerates every candidate shape and reports which one it saw**
  over one that asks the operator to confirm a shape up front. A counter in the ingest
  summary settles the question from data in one run; a question costs a round-trip and can
  be answered from a faulty memory. `evidence shapes` in the summary exists for exactly this.
- **But that applies to shapes nobody has stated — never to a fact the operator has already
  given.** Once a key name or shape arrives through channel 2, encode it directly: a tuple of
  candidate spellings for a *known* key is speculative generality, and it buries the confirmed
  answer among guesses. If a stated fact was not recorded and is now unclear, **ask** — the
  operator is a cheap, authoritative source, and the round-trip argument above is about
  questions that data can answer instead, not about facts only they hold.
- What must survive either way is invariant 1: a shape the parser cannot use gets an anomaly
  naming what it actually saw. `OUTPUT_NO_QUESTIONS`, `OUTPUT_NO_CITATION_EVIDENCE` and
  `OUTPUT_NO_VOTES` exist because their silent-counter predecessors let an empty questions
  table, zero dict citations and zero votes pass unremarked through a full real ingest.

## Known real-data format facts

Operator-relayed (channel 2), not derivable from anything in this repo. The fixture
generator renders all of it; `src/coa/outputs.py` parses it.

| Fact | Status |
|---|---|
| Many of the 48 questions answer on a **1-5 scale**, defaulting to **3 when evidence is insufficient** | confirmed |
| Evidence is returned **only when the answer is ≤ 3**; otherwise NULL | confirmed |
| Free-text questions (registered address, building type) return **`value \| NULL`** — a literal `NULL` is a first-class *answer*, not only an evidence state | confirmed |
| The answer-format instruction is part of the **user prompt**, attached to the questions | confirmed |
| The per-run answer map is keyed **`answer`** (singular), not `answers` as PLAN.md §4 said | confirmed — and `answers` does **not** exist, so only `answer` is read. The `keys not found` line in the ingest summary is the positive confirmation |
| **`voted_majority` / `voted_final` live INSIDE `answer_dict`**, beside its `run_<n>` keys | confirmed 2026-08-04. PLAN.md §4 put them at the record top level, which is why `votes` came back 0 — the same wrong-nesting bug as `citation_evidence`, in the opposite direction |
| `logs/jsonl/*.jsonl` is the authoritative call source; `logs/*.log` is redundant except for timestamps | confirmed — action counts matched exactly |
| A call's `queries` is a **fixed-length set of sub-queries within one call**, NOT cumulative session history | confirmed — length constant, members differ between consecutive calls |
| The singular `query` is **not always verbatim** in `queries` — 2.9% of real calls | confirmed, but the *cause* was wrong. "Nearly all are requoting" is **refuted**: `QUERY_REQUOTED` is 173 of 17,681 (1.0%), so ~99% are genuine disagreement, not the quoting variant this table used to claim. Do NOT ask the operator to paste `coa anomalies show QUERY_NOT_IN_QUERIES` — its detail embeds raw query text, i.e. merchant names and addresses |
| Whether each `queries` entry bills as its own search | **RESOLVED — it does not.** Billing is per visible call; $6,946.95 / $0.01 = 694,695 = the exact call count. See "The billing unit is RESOLVED" |
| Whether `open_page` / `find_in_page` carry the per-call fee | **RESOLVED — they do**, at the same $10/1K. They are 101,985 of the 694,695 billed calls (**$1,019.85**). Costing only `search` understates the fee by 17.2% |
| `web_search_call.action.sources` would link citations to calls, but is opt-in (`include=[...]`) and absent here | confirmed from API docs — per *call*, so it cannot reach an individual `queries` entry. **Low value: see "The decision lever is the question"** |
| `input_tokens + output_tokens == total_tokens`, so `cache_read` and `reasoning` are **subsets** | confirmed — holds across the corpus |
| `response_reasoning` has keys `id`/`type`/`summary`/`content`, but reasoning summaries were **not opted into**, so content is empty | confirmed — field not stored, only its token count |
| `service_tier` appears per run, and on the real corpus its value is **`"default"` for 100% of runs** | confirmed — a *fourth* tier string beyond standard/flex/priority. `"default"` is the OpenAI API's own name for the standard tier, so it must map to `standard` rates. It currently does **not**: `Pricing.for_tier` finds no `default` key and returns empty rates, so every run reports UNPRICED and the total comes out $0.00 |
| Whether a non-`completed` call is still billed | **moot on this corpus** — status is `completed` for all 694,695 calls, so `CALL_STATUS_NOT_COMPLETED` is 0 and there are no instances to settle |
| When no evidence is required, whether the line reads `Evidence. NULL`, a bare `Evidence.`, or is **omitted entirely** | **RESOLVED — all three occur, and the omitted case is common.** Of 2,369,904 prose answers: `NULL` 1,048,180 (44.2%), real evidence 720,370 (30.4%), **no Evidence line at all 601,353 (25.4%)**, bare label 1. Requiring the Evidence line in the block regex would have silently lost a quarter of the corpus |
| Whether a scale answer is a bare digit or a digit followed by prose | **unconfirmed** — `answer_text` stored verbatim, scale value derived, never assumed |
| **`PROSE_CITATION_RE`'s outer-parens requirement is NOT too strict** | **measured 2026-08-04** — of 499,507 answers whose evidence contains a markdown link, **499,507 are paren-wrapped and 0 are bare**. The regex drops nothing. So the prose-vs-dict citation gap (`CITATION_SOURCE_MISMATCH` 238,583) is a genuine finding about their pipeline, not our parser bug — it may now be reported as one |
| Whether `answer_dict` holds their parse of the same prose or a normalized form | **their parse, and it is bimodal.** 143,148 of 2,369,904 comparable answers differ (6.0%), but they concentrate: only 8,808 of 50,420 runs (17.5%) contain *any* disagreement, and those average 16 of 48 differing. Even spread would mean normalization; this shape means a systematic format difference in a subset of runs. Per-qnum breakdown is P4's job |
| `answer_dict` shape is `{run_0: {"A1": ..., "A2": ...}, ...}` | confirmed — matches what `_run_answer_map` already expects |

### What the first real doctor run added (2026-08-03)

Everything below came from one `coa doctor` paste. Most of it is now rendered in
`gen_fixtures.py` (2026-08-04): the prose-unparseable run that the `answer_dict` backstop
rescues, `citation_evidence` as one entry per *answer* rather than per *citation*, and
default-3 answers that carry no evidence. What remains unrendered is called out per row.

| Fact | Status |
|---|---|
| **The `questions` table is EMPTY on real data — `canonical set 0`** | confirmed, and it is the P4 blocker. `extract_questions` matched nothing for all 19,350 records, so the `question` field's shape differs from `record["question"][0][1]` holding a `Q1. ... Q48.` block. Knock-on: `QUESTION_SET_DRIFT` and `ANSWER_BLOCK_COUNT` were **vacuously** silent (the latter is gated on `if canonical and missing`), and `cache_picture`'s shared-prefix measurement had no prompts to fold |
| `_sync_questions` records **no anomaly** when extraction yields nothing | confirmed bug — it bumps `out_no_questions` and returns. A silent counter-only path, exactly what invariant 1 forbids. The only trace was `canonical set 0`, which the operator had to think to look at. `OUTPUT_NO_ANSWERS` is the model to copy |
| **`citation_evidence` is a TOP-LEVEL key**, sibling to `questions` / `answer` / `answer_dict` | confirmed. Shape `{"run_0": [{…, "a_key": "A1", …}], "run_1": …}` — a list of dicts, and the entries **do** carry `a_key`, so PLAN.md §6.4's "citation → question is EXACT via `a_key`" stands. What was wrong is only *where* PLAN.md §4 put the field: `_store_dict_citations` read `answer_dict["citation_evidence"]`, got `None`, and returned with **no anomaly and not even a counter** — the worst of the silent paths, and the reason `citations by source` showed only `markdown_prose` and `CITATION_SOURCE_MISMATCH` was 0 |
| `citation_evidence[run][i]["question"]` holds the question **text** | confirmed. The parser files it as `citations.title`. Worth a `COUNT(DISTINCT title)` — a second, independent source for the canonical question set |
| **`citation_evidence[run][i]` is a complete per-answer record**, not a citation: keys `question`, `a_key`, `answer`, `citation`, `evidence`, `full_answer_block` | confirmed 2026-08-04. It is therefore a *third answer source* and the only non-prose *evidence* source. `answer` and `evidence` are now parsed into `answers.ce_answer` / `answers.ce_evidence`; `full_answer_block` is deliberately left unparsed (~2.4M raw blocks would dominate the DB, and the two fields it would repair are already extracted) |
| **`citation_evidence` is CO-DERIVED with the prose, not an independent parse of it** | **measured 2026-08-04.** It covers exactly the 49,373 runs whose prose parsed — 49,373 x 48 = 2,369,904 answers, matching the prose-answer count to the unit — and is **entirely absent** for the other 1,047. So it is not, as assumed, the repair path for the runs that need one: when their prose pipeline failed, the prose *and* `citation_evidence` both vanished |
| **`answer_dict` is the only genuinely independent parse, and is NOT redundant** | **measured 2026-08-04.** It is the SOLE source for 50,256 answers (1,047 runs x 48) where `citation_evidence` has nothing. It also disagrees with us on 6.0% against `citation_evidence`'s 1.4% — consistent with `answer_dict` applying its own normalization while `citation_evidence` tracks the prose. Do not delete its answer path |
| Whose parse is authoritative — ours, `answer_dict`, or `citation_evidence` | **settled enough to stop worrying: it barely matters.** Where ours and `citation_evidence` are both present they agree on 98.6% of answers, so `coa scorecard --answer-source ce` shifts ~1.4% and no conclusion should turn on it. The flag stays because it costs nothing, not because it is load-bearing |
| **50,256 answers have NO observable evidence from any source** | **measured 2026-08-04, then re-confirmed to the unit on the fresh post-remap DB** (`d3 denominator 2,369,904 of 2,420,160`). The re-check matters because the first figure came from the stale DB that predated the `ce_*` columns and had 14 NULL merchant columns — it proves the 29-key input fix did not perturb the answer side, and that 49,373 x 48 / 1,047 x 48 still partition the corpus exactly. These are the 1,047 unreadable runs. Their prose exists; nothing here can read it, and neither backstop carries evidence. Scoring a `3` among them as a default-3 would assert something unmeasurable, so `coa scorecard` excludes them from the **default-3 denominator only** (the NULL rate keeps all answers) and prints the exclusion. `noinfo` therefore under-states, which is the safe direction for a budget argument |
| Prose citations carry an exact qnum independently | confirmed — `_store_prose_citations` stamps `block.qnum` from the enclosing answer block, so P4's per-question citation rate never depended on `a_key` at all |
| **The prompt key is `questions` (plural), holding `[[system, user]]`** | confirmed. PLAN.md §4 and the parser read `question` singular, which is why the `questions` table came back empty for all 19,350 records. The *shape* was right all along; only the key name was wrong |
| **`voted_majority` / `voted_final` are absent** — `votes 0` | confirmed. The third planned cross-check has no data. Inter-run agreement must be computed from `answers` across `run_id`, not from the votes table. Also counter-only (`out_empty_voted_final`), so it too passed silently |
| The question set **is** 48, confirmed from the answer side rather than the prompt | confirmed — 2,420,160 answers = **50,420 output-runs x exactly 48**. The scorecard has its denominator even with `questions` empty |
| The `answer_dict` backstop rescued **1,047 whole runs** | confirmed — 50,256 dict-parsed answers = 1,047 x 48, i.e. runs where prose parsing yielded *nothing* and every answer came from the dict. Invariant 1 working as designed; worth diagnosing why their prose differs |
| **50.5% of all answers are literally `NULL`** (1,223,141 of 2,420,160) | confirmed, and it is the headline candidate. Too large to be the free-text questions alone, so either far more questions are free-text than assumed, or scale questions also return NULL. P4's per-qnum split decides which |
| 80 merchants appear in `input/` and `output/` but have **no logs** | confirmed — 19,349 vs 19,269. 0.4%; they have answers but no search calls, so they must be excluded from any per-merchant call or cost denominator |
| **`pii_terms` was exactly 1.000 per merchant** (19,349 / 19,349) | **RESOLVED and VERIFIED ON REAL DATA 2026-08-04.** Only `se_toc_name` matched; every other key was spelled differently. The real 29-key schema is encoded in `MERCHANT_KEY_BY_COLUMN` / `PII_FIELDS` and rendered by `gen_fixtures.py`. The blast radius was wider than PII: `MERCHANT_KEYS` used the same spellings, so **14 of 15 `merchants` columns were NULL for all 19,349 merchants** — only `website` ever matched. **The re-ingest confirms the fix: `merchant columns 15 of 15`, `PII_FIELDS missing none`, and 201,002 pii terms = 10.4 per merchant** (was 1.000). P3 templating is unblocked |
| **Real `pii_terms` field breakdown** | **confirmed 2026-08-04** — email 52,740, name 45,748, zip 37,085, owner 35,774, street 26,214, city 23,500, phone 15,715 = **236,776 terms over 19,349 merchants (12.2 each)**, against 1.000 before the remap. All seven fields populate, so P3 templating masks every PII class it names. The `owner` row was missing from the first relay and briefly looked like a privacy defect (a populated `merchants` column with zero terms is self-contradictory, since `pii_terms_for` reads the same keys) — it was a typing drop, resolved by re-reading the line. **The contradiction was worth chasing**: had it been real, owner and signer names would have gone unmasked into anything the report quotes |
| Every search call has a singular `query` | confirmed — 592,710 billed `query_instances` rows = the search-call count exactly, and `CALL_FIELD_MISSING` is 0. Sub-queries are 1,805,026 = 3.05 per call, consistent with a constant `len(queries) == 4` minus the verbatim dedup |
| The pasted DB **predates the `QUERY_REQUOTED` split** (commit `0402168`) | inferred — `QUERY_NOT_IN_QUERIES` still reads 17,231, the pre-split figure, and `QUERY_REQUOTED` is absent. Anomalies are written at ingest and `coa doctor` only reads them. `coa reparse` refreshes the parse but writes its rows under `stage='reparse'` while leaving the old `stage='weblogs'` rows in place, so the codes would then double-count in doctor's tally |

### The real `input/*.jsonl` schema (operator-supplied 2026-08-04)

All 29 keys, verbatim. Encoded in `MERCHANT_KEY_BY_COLUMN` / `PII_FIELDS` and rendered by
`gen_fixtures.py`. **Spellings are load-bearing** — the fixture previously invented tidy
names (`city`, `phone`, `owner_name`), which matched the parser by construction and let a
corpus with 14 empty columns pass the whole suite.

```
se10  se_toc_name  sell_dba_nm  sell_lgl_nm  sell_ctry_cd  sell_pstl_cd  state_name
Seller_City_Name  Seller_Street_Address  Seller_Email_Address  Business_Phone_No
Significant_Owner_Name  Significant_Owner_City_Name  Significant_Owner_Postal_Code
Significant_Owner_Street_Address  Primary_Authorized_Signer_Name
Authorized_Signer_Physical_Address  merchant_opening_date  merchant_sub_category
wwic_industry_tagged  WWIC_Code  HRSE_tagged_merchant_ind  se_not_good_seller_ind
se_not_good_reason  obligor_id  obligor_id_recency_indicator  rno  type_of_se  website
```

Three things to know about it:

- **Spellings are the whole game.** A key that does not match produces a silent column of
  NULLs, never an error. `Primary_Authorized_Signer_Name` first reached this repo as
  `Primary_Auhorized_Signer_Name` (a hand-transcription slip across the air gap, corrected
  2026-08-04) — one missing character would have blanked `signer_name` corpus-wide with no
  symptom. `coa doctor`'s per-column fill is the check: a column at 0% while its neighbours
  populate means the map is wrong, not that the data is empty.
- **Four PII-bearing keys have no `merchants` column** and reach `pii_terms` only:
  `sell_dba_nm`, `sell_lgl_nm` (trade and legal names — a query is at least as likely to use
  these as `se_toc_name`), `sell_pstl_cd` (the SELLER postal code; only the owner's was
  mapped before), and `Authorized_Signer_Physical_Address` (a person's home address).
- **`se_not_good_seller_ind` / `se_not_good_reason` are NOT usable labels — the fields are
  ENTIRELY empty.** **RESOLVED 2026-08-04**, and it closes the project's last direction-
  changing question. All three label candidates measured **0 of 19,349 non-null, 0 distinct**:

  ```
  se_not_good_seller_ind    0 of 19,349 non-null (0.00%), 0 distinct
  se_not_good_reason        0 of 19,349 non-null (0.00%), 0 distinct
  HRSE_tagged_merchant_ind  0 of 19,349 non-null (0.00%), 0 distinct
  ```

  "Mainly null" was the operator's recollection; the corpus says *entirely* null. The
  distinction was the whole question, and it resolved to the pessimistic side.

  **The zero is trustworthy because of the output SHAPE, not the number.**
  `_label_candidate_lines` prints `key absent from every merchant record` when the key never
  appears, and the counted `0 of N non-null` form only when it does — tracked over every
  record, not the 200-row schema sample. Getting the counted form therefore proves two
  things at once: our key spellings match the real data (a misspelling would have printed
  the absent message), and the keys exist in the input schema while **not one of the 19,349
  merchants carries a non-null value**. Note the block does not establish that the keys are
  present on *every* record — `label_seen` is a set, so a single occurrence sets it. What is
  measured exactly is the non-null count, and that is 0; nothing here needs more.
  This is the one case where 0% fill is **not** the silent-misspelling symptom that blanked
  14 columns — the block was built to tell those two apart, and it did.

  Consequences, all of them subtractive:

  - **The analysis stays at "which questions carry no information."** It cannot become
    "which questions predict the outcome" — there is no outcome column to predict. Do not
    re-propose supervised question selection; it has no target variable.
  - **Nobody needs to chase the field's meaning.** The semantics question is moot: a field
    that is empty for 100% of merchants fails invariant 5 no matter what it would have meant.
  - **The `labels` table in `db.py` is reserved for data that has been measured absent.**
    It stays only because `CREATE TABLE IF NOT EXISTS` costs nothing; it is not pending work.
  - `gen_fixtures.py` renders all three keys **present and null**, so the fixture reproduces
    the real block byte-for-byte. It previously rendered `Y`/`N` at ~50% fill, i.e. a
    ground-truth label the corpus does not have.

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
  invariant. P2 stores the raw material; `coa scorecard` computes the rate.
- **A 3 that carried evidence is NOT a drop candidate.** Only a 3 that stood because the
  search found nothing counts — the prompt's stated default. Conflating the two inflates the
  headline with answers the pipeline actually determined.
- **"No evidence" means from EITHER parse.** Our regex missing an Evidence line while
  `citation_evidence` carries one is our failure, not the pipeline's finding. Scoring those
  as default-3 would have charged 1,047 runs' worth of our own parse bugs to the other team's
  question set. `answer_facts` (a view in `db.py`) classifies both, in one place.

## P4 headline findings (measured 2026-08-04, real corpus)

The scorecard ran on the post-remap DB. Three results, in descending order of how much
they change the deliverable.

### 1. The drop-candidate head is steep — four questions are close to inert

The full `DROP CANDIDATES` top 10, over 50,420 question-runs each. **Every one is `mixed`**
— no pure-`scale` question ranks, which is itself a finding:

| Q | noinfo | cited | tier |
|---|---|---|---|
| 22 | **91.6%** | 1.1% | inert |
| 9 | 82.7% | 2.9% | inert |
| 13 | 81.9% | 4.8% | inert |
| 36 | 77.5% | 5.1% | inert |
| 30 | 75.3% | 14.1% | low-yield |
| 37 | 73.2% | 15.3% | low-yield |
| 35 | 72.9% | 23.6% | low-yield |
| 29 | 72.8% | 22.3% | low-yield |
| 16 | 70.4% | 25.6% | low-yield |
| 17 | 69.7% | 17.5% | low-yield |

Q22 carried no information in ~46,185 of 50,420 runs and produced a citation in ~555.

**Rank by `noinfo`, but CUT by `cited`.** `noinfo` decays smoothly across the ten
(91.6 -> 69.7) and offers no natural cut point. The citation rate does: it sits at
1.1-5.1% for the top four and then jumps to 14.1% at Q30 — a ~3x discontinuity between
adjacent ranks. That splits the list into two tiers that deserve different recommendations:

- **Inert (22, 9, 13, 36)** — almost never answers, almost never surfaces a source. The
  deletion case.
- **Low-yield (30, 37, 35, 29, 16, 17)** — answers nothing ~70% of the time but still
  cites a source in a fifth to a quarter of runs. These are *functioning* questions with
  poor hit rates; recommending deletion on the `noinfo` rank alone would overreach.

A steep head was the good case: individual questions are droppable and the report can name
them, rather than the flat distribution that would have forced the weaker
consolidation-only argument.

### 2. `default-3` contributes essentially nothing — `noinfo` IS the NULL rate

On Q1-Q4 the `d3` column reads 0.0 / 0.1 / 0.0 / 0.0, and `noinfo` equals `null` to the
decimal. The default-3 mechanism was assumed to be half the drop signal; on the real
corpus the NULL answer carries it alone. Do not describe the headline as "NULL plus
default-3" without re-checking `d3` across all 48 — on the evidence so far it is a
rounding error. The apparatus still earns its place by *proving* that, and by keeping
the 50,256 unobservable answers out of a denominator they would have distorted.

### 3. Repeated runs disagree more often than they agree

`agree` on Q1-Q4: 42.3 / 38.8 / 36.6 / 53.9. **Q1 is the clean case** — pure `scale`, so
the answer is a 1-5 value and normalization cannot confound it. Q1 also has `null 0.0` and
`d3 0.0`: it always returns a confident-looking number, and that number fails to reproduce
across a merchant's runs more often than not.

**Do NOT read `agree` as "got the same answer twice."** It is
`COUNT(DISTINCT norm(answer)) = 1` over **all** of a merchant's runs
(`scorecard._inter_run_agreement`), gated on `>= 2` runs — so a 4-run merchant must have
all four match. It is therefore **conflated by run count and decays mechanically as runs
rise**, which the fixture shows plainly (2 runs: 3 of 17 agree; 3 runs: 0 of 5; 4 runs: 0
of 4, on random answers). With the corpus averaging 2.6 runs, 42.3% mixes 2-run and 3-run
populations and **understates** pairwise agreement. `coa runs` reports the clean figure,
conditioned on exactly two runs.

This may be the stronger cost argument. A question that returns nothing is cheap to cut;
one that returns a *different answer each run* is worse, because everything downstream
treats it as a finding. Caveat for the `mixed` rows only: exact-normalized-string
agreement is harsh on free text, where two runs can both be right and phrased
differently. Q1 has no such excuse.

## The first real templating run (operator-relayed 2026-08-04)

| Measure | Real corpus |
|---|---|
| query rows templated | 2,397,736 of 2,397,736 (**592,710 billed**) |
| unmasked (`n_placeholders = 0`) | **183,704 billed (31.0%)**, 775,190 overall |
| placeholders fired | name 182,030, city 96,932, email 89,346, street 68,915, zip 51,617, phone 31,859, owner 11,129 |
| distinct templates (billed) | **315,717** |
| singletons | **280,987** — 89% of templates, 47% of billed queries |
| top-100 coverage | 21.6% |
| archetypes mapped | none — `archetype_groups.csv` not built yet |

**All seven placeholder fields fire**, so the 29-key remap holds and the
field-stuck-at-zero failure did not recur.

### The head is thin AND singleton-dominated: this is UNDER-MASKING, not diversity

The pre-registered rule above fired exactly as written, which is what makes it
usable as evidence rather than as a post-hoc story. **Do not add `rapidfuzz`.**
Layer 2 would merge near-identical templates; it cannot merge templates that are
still merchant-specific because the PII in them was never recognised.

Corroborating arithmetic, independent of the singleton count: a top-100 template
averages ~1,280 billed queries against **19,269 merchants**. A genuinely shared
archetype ("`<NAME>` scam") should approach one per merchant per run. Capturing
~6.6% of the merchants that ought to share it means the other ~93% of those same
queries are sitting in the singleton tail under a merchant-specific template.

The fix is `pii_terms` coverage, and the diagnosis has to happen **locally** —
singleton templates are verbatim or near-verbatim query text and must not cross
the air gap. Only the *shape* of what is left unmasked comes back.

### Cache: input is ~24x the prompt per run, and 90% of it is billed at full rate

| Measure | Real corpus |
|---|---|
| input tokens | **2,525,321,998** |
| read from cache | 257,767,808 (**10.2%**) |
| run_0 / run_1 / run_2 | 19,269 / 19,169 / 10,943 runs; 8% / 11.7% / 11.4% cached |
| shared cross-merchant prefix | **32 chars (~8 tokens), 0.4%** of a prompt |

Two things follow, and they are not the same size:

- **Cross-merchant caching is confirmed dead**, exactly as predicted — 8 tokens
  against a 1024-token floor with no partial credit. Do not report it as waste.
- **The re-send is the lever.** 2,525,321,998 / 49,381 runs = ~51,139 input
  tokens per run against a ~2,150-token prompt (8,602 chars): a ratio of ~**24x**,
  consistent with the 48-question prompt being re-sent on each of the ~14 tool
  calls in a run. At 10.2% cached, nearly all of that repetition bills at the
  full input rate. **The ~24x is an INFERENCE about their agent loop, not a
  measurement** — `usage_metadata` is per run, not per API call — and any report
  using it must say so. What IS measured is the ratio and the hit rate.

### Real run distribution

run_0 19,269 / run_1 19,169 / run_2 10,943 = **49,381 log runs, 2.56 per
merchant**: 100 merchants with 1 run, 8,226 with 2, 10,943 with 3. Cutting to 2
removes 10,943 runs (**22.2%**).

Log runs (49,381) and output runs (50,420) differ by 1,039 — close to the 1,047
prose-unreadable runs but **not equal**, so it is a lead, not a conclusion.

`citation <-> open_page` overlap is **13.9%** (23,910 of 171,860 cited URLs), so
most cited pages were never opened and open_page cannot carry citation
attribution. It measures the value of page opens only.

### The run-count lever (`coa runs`, added 2026-08-04)

Cost scales with runs and needs **no attribution**, which makes it the one large lever not
blocked on the call -> question link. 50,420 output-runs over 19,349 merchants is 2.6 each,
so cutting to 2 removes ~23% of runs. Two things stop that becoming a naive 23% saving:

- **Only part of it is linear.** The per-call search fee (42.9% of the bill) and output
  tokens fall exactly in proportion. **Input does not** — `run_1+` reuse `run_0`'s prompt
  at the cached rate, so the run being cut is already the *cheapest* on input. Costing a
  marginal run at the average overstates the saving.
- **Cost comes from the LOG run count, not the output one.** 50,420 is output-runs over
  19,349 merchants; `usage_metadata` lives on the 19,269-merchant log side. Dividing cost
  by the wrong population is the denominator error `SubqueryPicture` already exists to flag.

**The "is the third run redundant?" question inverts.** For runs A, B, C: if A == B the
majority is fixed and C *cannot* change it; if A != B there was no majority for C to
change — C creates one. So "how often does run_2 flip the majority" reduces to "how often
do the first two disagree", and **low agreement means run_2 is decisive OFTEN, not rarely**.
A high `decisive` share is therefore *not* evidence the run earns its keep: it is a
tie-break between two answers that already contradict each other, drawn from the same
unreliable process. A high `no majority` share (all three differ) is worse still — the
pipeline is not determining that question at all, and more runs will not fix it.

### The blocker on any savings figure: searches are not attributable to questions

**Do not convert these rates into a dollar figure by proportional allocation.** Cost is
42.9% search fee, and nothing measured so far attributes a search call to a question:
`web_search_call.action.sources` is not enabled, so the call -> question link does not
exist in the corpus. `citation -> question` is exact via `a_key`, but a citation is the
*output* of a search, not the search itself.

That gap hides two opposite readings of the same 91.6%, and they differ by the entire
search fee:

- **The question never triggers a search.** Dropping it saves prompt and answer tokens only.
- **The question triggers searches that find nothing.** Dropping it saves the searches too
  — and a question that searches and comes back empty 91.6% of the time is the single
  most expensive thing in the corpus.

A near-zero citation rate does **not** settle it: searching and finding nothing is exactly
what produces a NULL answer with no citation. The pessimistic reading is at least as
consistent with the data as the optimistic one.

**One savings component needs NO attribution at all.** Every run pays input tokens for all
48 question texts whether or not a question yields anything, so deleting a question removes
its text from the prompt deterministically. `canonical set 48` on the real corpus (confirmed
2026-08-04, after the `questions`-plural key fix) means the question text is finally in the
`questions` table, so each question's share of the user prompt is measurable. Multiply that
share by the measured `input_tokens` and the input saving is exact up to the char->token
proxy — which must be labelled, or `tiktoken` justified as a dependency under the stack rule.

Two cautions on that figure: the **output** saving is much smaller than it looks, because an
inert question's answer block is `NULL / Evidence. NULL` — a handful of tokens, not a
paragraph. And input tokens are ~57% of cost with the search fee at 42.9%, so the
no-attribution floor is real but is not the headline.

**P3 is the bridge for the rest, and it is BUILT** (`src/coa/normalize.py`, 2026-08-04).
PII-templated query archetypes are human-readable (`"what type of building is at <ZIP>"`),
so an archetype maps to a question by inspection even though the corpus carries no explicit
link. That converts "Q22 is inert" into "Q22 costs N searches", which is the claim the
report actually needs. Label the mapping as the human judgement it is — it is the one
heuristic worth keeping, and invariant 5 requires it be named as such.

What is done and what is not:

- **Done, mechanically.** `coa analyze` populates `template` / `n_placeholders` / `archetype`
  for every `query_instances` row, and the `archetypes` view rolls them up. Verified on
  fixtures: billed calls partition across the view exactly, so archetype cost shares sum
  to 100%.
- **Not done, and it is the deliverable.** The `template -> archetype` map is hand-built by
  the operator from `coa analyze --export-templates`, and the `archetype -> question` step
  after it is pure inspection. No dollar figure exists until both happen.

### P3 templating rules that are easy to get wrong

- **`coa analyze` WRITES.** It is the one analysis command that does. `coa reparse` rebuilds
  `query_instances` from scratch and therefore clears every template, so reparse must always
  be followed by analyze. Both commands say so at runtime; the silent symptom is an
  `archetypes` view that has quietly gone empty.
- **`n_placeholders == 0` is a privacy control, not a statistic.** It means nothing matched,
  so the template IS the verbatim query text. It gates the head export, and the gate is
  **necessary but not sufficient** — a template with placeholders can still carry a term
  that never reached `pii_terms`. `archetype_groups.csv` and `reports/` are gitignored for
  this reason; treat both as merchant data.
- **A thin exact-template head has TWO causes with opposite fixes, and `singletons` is what
  separates them.** Under-masking looks identical to genuine query diversity: a query
  carrying PII that never reached `pii_terms` stays merchant-specific, so 19,269 merchants
  produce 19,269 distinct templates. A thin head with MANY singletons means the fix is more
  `pii_terms` coverage; only a thin head with FEW singletons is a real layer-2 (fuzzy)
  case. Adding `rapidfuzz` on the raw head figure would paper over a masking gap while
  leaving the PII unmasked in the export.
- **Head coverage is vacuous below `HEAD_FOR_COVERAGE` distinct templates.** The fixture
  corpus has 9, so its `top 9 -> 100%` says nothing; the report labels that case rather
  than letting it read as success.
- **Watch `placeholders fired` for a field stuck at 0** while its `pii_terms` bucket is
  populated. That is the P3-side version of the silent mismatch that blanked 14 merchant
  columns. On fixtures `street` / `zip` / `owner` read 0 only because `gen_fixtures.
  queries_for` never builds queries from them — on the real corpus all three must fire.
- Templating matches **per merchant via `se10`**, never against a global term list, and
  masks **longest term first** (a street value contains the city value). Both are
  correctness properties, not optimizations.

## Stack

- Python **3.10+** (the operator's analysis environment runs 3.12; the pin follows the
  deployment, not an aspiration). Stdlib `sqlite3` + `zipfile` + `re` + `csv`.
- Dependencies: `pyyaml`, `pytest`, `ruff`. **That is the whole list.** Adding one requires
  a stated trigger — see the reversal triggers in the plan; heavier options
  (rapidfuzz/sklearn/sentence-transformers, dnspython/whois, pandas/duckdb) were all
  deliberately deferred behind measurements, not forgotten.
- Layout: `src/coa/{config,anomalies,db,weblogs,outputs,inputs,normalize,metrics,report,cli}.py`

## `scripts/` vs a `coa` subcommand

Both exist; the boundary is what the output is FOR, not how big the code is.

| | goes in `coa` (doctor / subcommand) | goes in `scripts/` |
|---|---|---|
| asked | every ingest, or more than twice | once, about one specific discrepancy |
| output | numbers the operator relays back | an artifact they inspect locally |
| lifetime | permanent, tested | may be deleted once the question closes |

`coa doctor` remains the diagnostic surface: **if the answer is a number that
crosses the air gap, it belongs there**, because a figure worth asking for once is
worth having on every ingest. A script that starts getting run every time has
graduated — move its counts into doctor and leave the artifact-writing behind.

Two rules that keep `scripts/` from becoming a graveyard:

- **Never a `sys.path` hack.** The package is installed editable, so
  `from coa.db import connect` just works; inserting a path forces `# noqa: E402`
  on every import and is pure debt. (`find_unparsed_runs.py` shipped with one.)
- **Split output by what may cross the air gap**, exactly as the head-template
  export does: safe counts to stdout, anything carrying `se10` or query text to a
  file under the gitignored `reports/`.

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
