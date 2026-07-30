# Web-Search Cost Optimization — Analysis Pipeline Implementation Plan

Target executor: Claude Code, building in a connected environment against **synthetic fixtures**.
Real data lives in an air-gapped environment (which itself has internet access, but Claude Code
never sees the real logs). The repo must therefore be: (a) fully testable on fixtures,
(b) defensive about every format assumption, (c) built to surface anomalies as copy-pasteable
text the operator can relay back to Claude Code for parser fixes.

---

## 0. Context Claude Code needs (put in CLAUDE.md)

- Domain: merchant fraud-screening pipeline (another team's) calls OpenAI web search
  (model: "gpt 5.4") with a fixed 48-question prompt per merchant, multiple runs per merchant,
  majority-voted answers. ~14 web searches/merchant/run average. Goal of THIS repo: parse the
  team's logs + outputs, attribute searches → questions → citations, cluster query archetypes,
  measure redundancy and per-question reliability, model cost, and produce reports that justify
  reducing/replacing searches.
- Scale: ~20k merchants today, ~2 GB zipped logs. Must stream; never assume it fits in memory,
  but 2 GB is small enough that SQLite handles everything comfortably.
- Billing model (parameterize, verify later): per-call fee applies to `action type - search`
  only; open_page / find_in_page cost tokens but no per-call fee. Model is post-training-data
  ("gpt 5.4") so ALL pricing constants live in config with placeholder values and a loud
  warning in reports until the operator fills them from the pricing page / billing dashboard.
- Cardinal rule: **never crash on malformed input; never silently drop it either.** Everything
  unparseable goes to the anomaly store with full context.

## 1. Repo layout

```
cost_optimization_analysis/
├── CLAUDE.md                  # context + conventions for Claude Code
├── PLAN.md                    # this file
├── pyproject.toml             # deps pinned loosely (>=) — env versions may differ
├── config.yaml                # all paths, thresholds, pricing constants
├── src/coa/
│   ├── config.py              # pydantic-validated config loader
│   ├── anomalies.py           # central anomaly recorder (see §8)
│   ├── ingest/
│   │   ├── zipsource.py       # iterate members of .zip / dirs of .log without full extract
│   │   ├── logs.py            # log line state machine (§3)
│   │   ├── outputs.py         # output/*.jsonl parser (§4)
│   │   └── inputs.py          # input/*.jsonl merchant-detail parser (§5)
│   ├── store/
│   │   ├── db.py              # SQLite schema + upsert helpers (§2)
│   │   └── export.py          # parquet/CSV exports of analysis views
│   ├── analyze/
│   │   ├── normalize.py       # PII → placeholder templating (§6.1)
│   │   ├── cluster.py         # 3-layer archetype clustering (§6.2)
│   │   ├── runs.py            # time-burst run attribution heuristic (§6.3)
│   │   ├── attribution.py     # citation↔query↔question joins (§6.4)
│   │   ├── agreement.py       # inter-run agreement, vote diffs (§6.5)
│   │   └── cost.py            # cost model (§6.6)
│   ├── enrich/
│   │   └── domains.py         # DNS/WHOIS/TLS features for merchant + cited domains (§7)
│   ├── report/
│   │   └── build.py           # markdown + CSV report bundle (§9)
│   └── cli.py                 # `coa ingest|analyze|enrich|report|anomalies` subcommands
└── tests/
    ├── fixtures/
    │   ├── gen_fixtures.py    # synthetic data generator (§10) — build this FIRST
    │   └── data/              # generated .log/.jsonl/.zip committed for determinism
    └── test_*.py              # pytest per module, golden-output style
```

Dependencies (all optional layers degrade gracefully — wrap imports):
pandas, pyarrow, duckdb (analysis convenience), rapidfuzz, scikit-learn (TF-IDF),
sentence-transformers (semantic layer, lazy-loaded), dnspython, python-whois,
cryptography (TLS cert parse), pydantic, pyyaml, tqdm, pytest.

## 2. Data model (SQLite, `coa.sqlite`)

All tables carry `src_file`, `src_line` (or line range) provenance columns.

- **merchants**(se10 PK, opening_date, city, industry_tagged, sub_category, email, phone,
  street, signer_name, owner_name, owner_city, owner_postal, owner_street, website,
  country, state, raw_json) — from input/. Duplicate se10 across input files → keep first,
  flag anomaly `DUP_INPUT_SE10`.
- **pii_terms**(se10, field, value_norm) — exploded normalized values used for templating.
- **log_events**(id PK, se10, ts, level, module, message, kind) — every timestamped [se10]
  line; kind ∈ {web_search_call, other}.
- **search_calls**(id PK, se10, ts, ws_id, action_type, raw_action_line, query_raw,
  queries_raw, queries_json, url, pattern, pairing ∈ {strict, orphan}, parse_conf ∈
  {clean, heuristic, failed}, run_id NULLABLE, possible_wrap BOOL)
  — one row per action line. `queries_json` = best-effort list; raw always retained.
- **output_records**(id PK, se10, src_file, src_line, n_runs, question_user_prompt,
  question_system_prompt, raw_json_hash, dup_flag BOOL)
- **questions**(qnum 1..48 PK, text) — extracted once from a reference record; every other
  record's extracted set is compared against it; mismatch → anomaly `QUESTION_SET_DRIFT`.
- **answers**(se10, output_id, run_id, qnum, answer_text, evidence_text, is_null BOOL,
  parsed_from ∈ {answers_text, answer_dict}, agree_with_dict BOOL)
- **citations**(id PK, se10, output_id, run_id, qnum, url, domain, title, empty_placeholder
  BOOL, source ∈ {citation_evidence, markdown_prose}, char_pos NULLABLE)
- **votes**(se10, output_id, qnum, voted_majority, voted_final, differs BOOL)
- **archetypes**(archetype_id PK, template, layer ∈ {exact, fuzzy, semantic}, exemplar,
  n_queries, n_merchants)
- **query_instances**(search_call_id, query_text, template, archetype_id)
- **domain_features**(domain PK, first_seen_source, dns_a, mx_present, ns, created_date,
  registrar, whois_privacy, tls_issuer, tls_not_before, fetch_status, fetched_at)
- **anomalies**(id PK, stage, code, se10 NULLABLE, src_file, src_line, raw_excerpt, detail,
  ts_recorded)

## 3. Log parser (`ingest/logs.py`) — the hard part

**Input:** all `*.log` members inside the zip(s)/dir under `data/cost_optimization/logs/`,
parsed together but with per-file provenance. Process each file independently (the race
condition is within-file line interleaving from async logging; there is no cross-file
ordering to exploit). Read as text with `errors="replace"`; any replacement chars → anomaly
`ENCODING`.

**Line classifier** (in priority order):
1. `TIMESTAMPED`: `^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+) \| ([\w.]+) \| \[(\d+)\] (.*)$`
   → (ts, level, module, se10, message).
2. `ACTION`: `^action type - (\w[\w ]*?)\s*,\s*(.*)$` — no timestamp, no se10.
   Known action types: `search`, `open_page`, `find in page` (note: spaces observed;
   accept both `find in page` and `find_in_page`). Unknown type → still store row with
   action_type verbatim + anomaly `UNKNOWN_ACTION_TYPE`.
3. `OTHER`: everything else (config noise etc.) — counted, not stored (except a per-file
   tally in the run summary).

**Pairing state machine (per file, single pass):**
- Hold `last_line_class` and, if the previous line was TIMESTAMPED with message containing
  `Response tool type - web_search_call`, hold its (se10, ts, ws_id).
- ACTION line arriving **immediately after** such a line → `pairing=strict`, inherit
  se10/ts/ws_id. (This is the operator's stated certainty rule.)
- ACTION line arriving in any other position (after OTHER, after another ACTION, after a
  non-web_search TIMESTAMPED line, or at file start) → `pairing=orphan`, se10 NULL.
  Also anomaly `ORPHAN_ACTION` with ±3 surrounding raw lines so the operator can inspect
  whether a new pairing rule is discoverable.
- Line following an ACTION line that classifies as OTHER but is non-boilerplate (does not
  match a configurable noise-pattern list) → treat as possible wrapped continuation of the
  ACTION line: append to `raw_action_line`, set `possible_wrap=True`, anomaly
  `POSSIBLE_WRAPPED_ACTION`. Do NOT re-run field extraction on the merged line automatically;
  keep both raw forms. (Operator observed 1-line actions but is not sure.)
- Extract `ws_id` from web_search_call lines: `id - (ws_\w+)`; store on log_events and
  propagate to strictly-paired search_calls.

**Action field extraction:**
- `search`: split off `query - ` and `queries - ` segments. Parse `query` as: text between
  `query - ` and the **last** occurrence of `, queries - ` (anomaly `MULTI_QUERIES_MARKER`
  if `, queries - ` appears more than once).
- `queries` string is the known mess: comma-separated items where **double quotes are part
  of query text** and items may be unquoted or partially quoted
  (observed: `prev_query , "asdasd" asdasd, next_query`). Parse strategy:
  1. Split on commas. Do NOT attempt quote-aware CSV parsing (quotes are content here).
  2. Strip whitespace; drop empty items.
  3. Validation: the `query` value must appear (substring match after whitespace-normalize)
     within the reconstructed queries. If yes → `parse_conf=clean` when items look sane
     (non-empty, no item > 300 chars), else `heuristic`.
  4. If the `query` value spans a comma (i.e., `query` contains `,` and substring-matching
     shows a split item boundary falls inside it) → merge the affected adjacent items,
     set `parse_conf=heuristic`, anomaly `COMMA_IN_QUERY`. This uses the redundant `query`
     field as ground truth to repair comma-splitting — the one honest signal we have.
  5. Always retain `queries_raw` verbatim; every `parse_conf != clean` row is inspectable
     via the anomaly CLI.
- `open_page`: extract `url - (\S+)`. `find in page`: extract url and `pattern - (.*)$`.
  Missing expected field → anomaly `ACTION_FIELD_MISSING`, keep row.

**Invariant checks after ingest (assert-and-report, not crash):**
- every ACTION row is strict-paired or orphan-flagged;
- per file: count(TIMESTAMPED web_search_call) vs count(strict-paired ACTION) — mismatch
  is expected (racy interleaving) but the delta is a data-quality KPI in the report;
- strict-paired rows where `query` value NOT in `queries` → anomaly `QUERY_NOT_IN_QUERIES`
  (operator believes it always is; verify empirically).

## 4. Output parser (`ingest/outputs.py`)

For each line of each `output/*.jsonl` (stream, one JSON per line; bad JSON → anomaly
`BAD_JSON_LINE` with first 500 chars):

- se10 (int or str — normalize to str everywhere in the codebase).
- Duplicate se10 across/within output files → ingest all, mark `dup_flag`, anomaly
  `DUP_OUTPUT_SE10` listing both provenances. Analysis layer uses the record with the most
  runs (tie: last by file order) but reports how often the choice mattered.
- `question[0][0]` system prompt, `question[0][1]` user prompt. Extract the 48 questions:
  `re.findall(r"Q(\d+)\.\s*(.*?)(?=\nQ\d+\.|\Z)", user_prompt, re.S)`. First record
  establishes the canonical set; drift → anomaly.
- `answers` dict: for each `run_k`, parse the Q/A/Evidence text with block regex:
  `Q(\d+)\..*?\nA\1\.\s*(.*?)\nEvidence\.\s*(.*?)(?=\n\nQ\d+\.|\Z)` (re.S). Answers or
  evidence of literal `NULL` (with trailing whitespace tolerance) → `is_null`. Blocks
  missing / count != 48 → anomaly `ANSWER_BLOCK_COUNT` (record which qnums missing).
- Citations, two independent sources, both stored with `source` tag:
  a. markdown links in evidence text: `\(\[([^\]]*)\]\(([^)]*)\)\)` — empty title+url →
     `empty_placeholder=True`; char offset recorded (position recovery per earlier design).
  b. `answer_dict.citation_evidence.run_k[]` entries: {question, a_key, answer, citation,
     evidence, full_answer_block}. `a_key` → qnum. citation may be null or a single URL;
     ANY other shape (list, dict, multiple URLs in one string) → anomaly
     `CITATION_SHAPE_UNEXPECTED` with the raw value printed — operator will relay these back.
  Cross-check (a) vs (b) per (run, qnum): URL sets should match; mismatch → anomaly
  `CITATION_SOURCE_MISMATCH` (this measures how lossy the prose post-processing is).
- `answer_dict.run_k` parsed answers: compare against our own text-parse of `answers.run_k`
  → `agree_with_dict`; disagreement → anomaly `ANSWER_PARSE_MISMATCH` (tells us whose
  parser — ours or theirs — to trust, empirically).
- `voted_majority` vs `voted_final`: store both per qnum; `differs` flag. Note
  voted_majority values can be lists (observed `"A2": ["NULL", "NULL"]`) — normalize:
  list of identical values → scalar; genuinely mixed list → keep JSON, anomaly
  `VOTE_VALUE_LIST`. Empty `voted_final` dict → treat as absent, count it.
- Merchant-level convenience keys (website, industry, state...) → merged into merchants
  table (input/ takes precedence on conflict; conflict → anomaly `INPUT_OUTPUT_FIELD_CONFLICT`).

## 5. Input parser (`ingest/inputs.py`)

- Stream all `input/*.jsonl`. Keep the full raw_json (schema has "many more" keys — never
  hard-code an exhaustive list; extract the known keys, preserve the rest).
- Build `pii_terms`: for the templating fields (merchant name/se_toc_name, street addresses,
  cities, postal codes, phone, emails, signer/owner names), normalize each value
  (casefold, collapse whitespace, strip punctuation variants; phones also digits-only form;
  emails also local-part and domain separately) and store every variant ≥3 chars.

## 6. Analysis layer

### 6.1 Normalization (`analyze/normalize.py`)
For each query of merchant M: greedily replace occurrences of M's pii_terms in the
normalized query, longest value first, with `<FIELD>` placeholders (`<NAME>`, `<STREET>`,
`<CITY>`, `<ZIP>`, `<PHONE>`, `<EMAIL>`, `<OWNER>`, ...). Digits-only phone matching handles
formatting differences. Result = template. Store per query instance. Queries where no
placeholder fired → keep verbatim template + tally (could be generic queries like
"BBB complaints database" — themselves interesting).

### 6.2 Archetype clustering (`analyze/cluster.py`) — three layers, each persisted
1. **Exact:** group identical templates. Expect this to capture the large majority.
2. **Fuzzy:** among remaining low-frequency templates, TF-IDF char 3–5-gram vectors +
   cosine, plus rapidfuzz token_set_ratio; connected components over
   similarity ≥ threshold (config, default 0.85). Merges typo/word-order variants.
3. **Semantic:** sentence-transformers (small model, e.g. all-MiniLM-class; lazy download at
   first use — environment has internet) over layer-2 cluster exemplars; agglomerative
   clustering (cosine, threshold in config) to merge paraphrase archetypes
   ("<NAME> scam" ↔ "<NAME> fraud complaints"). If the package/model is unavailable at
   runtime, log a warning and ship layer-2 results — reports state which layers ran.
Output: archetypes table + mapping of every query instance to (layer1, layer2, layer3) ids.

### 6.3 Run attribution (`analyze/runs.py`)
No run marker exists in logs. Heuristic: per merchant, order strict-paired search_calls by
ts; split into bursts where inter-call gap > threshold (config, default 120 s; also emit the
gap histogram so the operator can tune it). If burst count == n_runs from output → assign
run_ids in chronological order, `run_attribution=matched`; else NULL +
`run_attribution ∈ {too_few_bursts, too_many_bursts, no_output_record}`. Report the match
rate prominently — all per-run search metrics carry this caveat.

### 6.4 Citation ↔ query ↔ question attribution (`analyze/attribution.py`)
- citation → question is EXACT (citation_evidence carries a_key). This is the anchor.
- citation → search query, per merchant (and run where matched): candidate = that
  merchant's search queries; score by (i) cited domain string appearing in query,
  (ii) token overlap between query template and question text, (iii) burst/run match.
  Store best match + score + `ambiguous` flag when top-2 scores are close. No `sources`
  field exists in these logs, so this stays heuristic — labelled as such in reports.
- question → archetype affinity matrix: for each (archetype, qnum), P(archetype present in
  merchant-run | question answered non-NULL with citation). This is the empirical
  replacement for a hand-made mapping, though config allows seeding known pairs
  (phone archetypes → phone questions, etc.).

### 6.5 Reliability without ground truth (`analyze/agreement.py`)
- Per qnum: inter-run agreement rate (all runs equal / majority size / entropy), NULL rate,
  citation rate, empty-placeholder rate, voted_final≠voted_majority rate.
- Low-agreement or always-NULL questions = candidates for removal → direct search savings;
  this is the strongest defensible finding available before ground-truth labels arrive.
- Schema reserves a `labels`(se10, label, source, ts) table so future ground truth plugs in
  without rework (then: per-question predictive value, ablation vs labels).

### 6.6 Cost model (`analyze/cost.py`)
config.pricing: {fee_per_1k_search_calls, input_per_mtok, output_per_mtok,
search_content_flat_tokens_or_null, batch_discount}. All placeholders = null until operator
fills; reports print "UNVERIFIED PRICING" banner if null and still show call-count-based
relative shares (which need no prices). Metrics: search calls per merchant / per run
(strict-paired only, orphans reported as ± uncertainty band), open_page/find_in_page counts,
per-archetype call share, projected savings for scenario configs (drop archetypes X, cap
searches at N, drop questions Y).

## 7. Domain enrichment (`enrich/domains.py`)
Runs IN the air-gapped-from-Claude environment (it has internet). Inputs: merchant website
domains (input/output tables) + cited domains. Per domain: DNS A/NS/MX (dnspython), WHOIS
created_date/registrar/privacy (python-whois; failures common — status column, never fatal),
TLS leaf cert issuer/notBefore via ssl socket (timeout 5 s). Politeness: configurable
concurrency + per-TLD rate limit; resumable cache keyed by domain (rerun-safe). Output feeds:
(a) fraud-feature exploration (domain age vs answers), (b) the "which questions can free
lookups replace" comparison — e.g., registered-email question vs actual MX/WHOIS data
agreement rate.

## 8. Anomaly framework (`anomalies.py`) — first-class deliverable
- `record(stage, code, se10, src_file, src_line, raw_excerpt, detail)` — used by every module.
- Raw excerpts truncated to 2000 chars, secrets never involved (no keys in this data).
- CLI: `coa anomalies summary` (counts by code), `coa anomalies show CODE --sample N`
  (pretty-prints representative samples WITH surrounding context lines) — designed for the
  operator to copy-paste back to Claude Code so the parser can be patched against reality.
- The ingest run ends with a one-screen data-quality summary: files seen, lines by class,
  strict/orphan ratio, parse_conf distribution, anomaly counts. Non-zero anomalies is the
  expected steady state, not an error.

## 9. Reports (`report/build.py`)
Markdown bundle + CSVs (+ optional matplotlib PNGs) under `reports/<timestamp>/`:
1. **Data quality** — §8 summary, pairing loss, citation-source mismatch rate,
   voted_final vs voted_majority diff, dup se10 findings.
2. **Search behavior** — calls/merchant distribution (median, p90), calls/run where
   attributable, action-type mix, burst analysis, position-in-run citation decay.
3. **Archetypes** — table: archetype, layer, exemplar, frequency, merchant coverage,
   citation-linked rate, affinity questions, estimated cost share.
4. **Question scorecard** — per qnum: text, NULL rate, agreement, citation rate,
   replaceable-by-lookup flag (from §7 agreement), recommendation tier
   (keep / consolidate / replace-with-lookup / drop-candidate).
5. **Cost scenarios** — baseline vs configured what-ifs, with pricing-status banner.

## 10. Fixture generator (`tests/fixtures/gen_fixtures.py`) — BUILD FIRST
Deterministic (seeded) generator producing a miniature `data/cost_optimization/` tree with
~30 merchants, 2 log files inside a zip, 2 input jsonl, 2 output jsonl, covering EVERY case
in this plan: clean strict pairs; interleaved async lines breaking pairing (orphans);
noise lines; unknown action type; wrapped 2-line action; queries with embedded quotes,
embedded commas (repairable via `query`), unquoted junk; open_page + find-in-page (both
spellings); missing fields; empty `([]())` placeholders; citation_evidence vs prose
mismatch; duplicate se10 in outputs and inputs; run counts 1–4; a run with 47 answer
blocks; voted lists `["NULL","NULL"]`; empty voted_final; question-set drift in one record;
bad JSON line; non-UTF8 byte. Golden expected-counts JSON checked by tests. Every future
operator-reported anomaly gets added here as a regression case — this is the core loop for
air-gapped development.

## 11. Build phases for Claude Code (each ends green on fixtures)
- **P0** scaffold: repo, config, CLI skeleton, anomaly framework, SQLite schema, fixture
  generator + golden counts. Acceptance: `pytest` green, `coa ingest --help` works.
- **P1** log parser vs fixtures. Acceptance: golden counts of strict/orphan/parse_conf match;
  every fixture edge case lands in the intended table/anomaly code.
- **P2** output + input parsers, cross-checks, dedup flags. Acceptance: golden answers/
  citations/votes counts; citation mismatch fixture detected.
- **P3** normalization + 3-layer clustering. Acceptance: fixture queries collapse to the
  designed archetypes; semantic layer skippable via config.
- **P4** run attribution + attribution joins + agreement metrics + cost model.
- **P5** enrichment module (integration-test against a public domain list, unit-test with
  mocked resolvers).
- **P6** report bundle end-to-end on fixtures: `coa ingest && coa analyze && coa report`.
- **P7** operator loop: run on real data in the environment → paste `coa anomalies show`
  output back → patch parsers + extend fixtures → rerun. Budget explicit time for ≥2 such
  iterations; the plan assumes reality will contradict at least some format assumptions.

## 12. Known unknowns (tracked, non-blocking)
- Exact "gpt 5.4" pricing + whether flat search-content token block applies → config
  placeholders; verify against billing dashboard with one day's strict-paired search count.
- Whether open_page incurs a per-call fee → same verification.
- voted_final semantics → empirical diff report (§6.5) will answer it.
- Ground-truth labels → schema ready (§6.5); analysis upgrades when available.
- Race-condition pairing loss rate → measured, reported; if orphan rate is high, revisit
  with the ±context samples whether a secondary pairing signal exists (e.g., ws_id echoes).
