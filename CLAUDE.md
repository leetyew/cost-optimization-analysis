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
| **50,256 answers have NO observable evidence from any source** | **measured 2026-08-04** — the 1,047 unreadable runs. Their prose exists; nothing here can read it, and neither backstop carries evidence. Scoring a `3` among them as a default-3 would assert something unmeasurable, so `coa scorecard` excludes them from the **default-3 denominator only** (the NULL rate keeps all answers) and prints the exclusion. `noinfo` therefore under-states, which is the safe direction for a budget argument |
| Prose citations carry an exact qnum independently | confirmed — `_store_prose_citations` stamps `block.qnum` from the enclosing answer block, so P4's per-question citation rate never depended on `a_key` at all |
| **The prompt key is `questions` (plural), holding `[[system, user]]`** | confirmed. PLAN.md §4 and the parser read `question` singular, which is why the `questions` table came back empty for all 19,350 records. The *shape* was right all along; only the key name was wrong |
| **`voted_majority` / `voted_final` are absent** — `votes 0` | confirmed. The third planned cross-check has no data. Inter-run agreement must be computed from `answers` across `run_id`, not from the votes table. Also counter-only (`out_empty_voted_final`), so it too passed silently |
| The question set **is** 48, confirmed from the answer side rather than the prompt | confirmed — 2,420,160 answers = **50,420 output-runs x exactly 48**. The scorecard has its denominator even with `questions` empty |
| The `answer_dict` backstop rescued **1,047 whole runs** | confirmed — 50,256 dict-parsed answers = 1,047 x 48, i.e. runs where prose parsing yielded *nothing* and every answer came from the dict. Invariant 1 working as designed; worth diagnosing why their prose differs |
| **50.5% of all answers are literally `NULL`** (1,223,141 of 2,420,160) | confirmed, and it is the headline candidate. Too large to be the free-text questions alone, so either far more questions are free-text than assumed, or scale questions also return NULL. P4's per-qnum split decides which |
| 80 merchants appear in `input/` and `output/` but have **no logs** | confirmed — 19,349 vs 19,269. 0.4%; they have answers but no search calls, so they must be excluded from any per-merchant call or cost denominator |
| **`pii_terms` was exactly 1.000 per merchant** (19,349 / 19,349) | **RESOLVED and VERIFIED ON REAL DATA 2026-08-04.** Only `se_toc_name` matched; every other key was spelled differently. The real 29-key schema is encoded in `MERCHANT_KEY_BY_COLUMN` / `PII_FIELDS` and rendered by `gen_fixtures.py`. The blast radius was wider than PII: `MERCHANT_KEYS` used the same spellings, so **14 of 15 `merchants` columns were NULL for all 19,349 merchants** — only `website` ever matched. **The re-ingest confirms the fix: `merchant columns 15 of 15`, `PII_FIELDS missing none`, and 201,002 pii terms = 10.4 per merchant** (was 1.000). P3 templating is unblocked |
| **`pii_terms` has NO `owner` field rows on real data** | **OPEN — the one loose end from the 2026-08-04 re-ingest.** `terms by field` returned six fields (email 52,740, name 45,748, zip 37,085, street 26,214, city 23,500, phone 15,715) and no `owner`, yet `PII_FIELDS["owner"]` = `Significant_Owner_Name` + `Primary_Authorized_Signer_Name`, both of which back `merchants` columns that `merchant columns 15 of 15` reports populated. `pii_terms_for` reads the same record keys, so a populated column with zero terms is self-contradictory. Most likely a typing drop when the line crossed the air gap (the breakdown is ordered by count DESC, so `owner` sorts last). If it is real, **owner and signer names go unmasked by P3 templating** — a privacy defect, not a metrics one |
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
