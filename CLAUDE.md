# Project: Web-Search Cost Optimization Analysis (`coa`)

## Domain

Another team runs a merchant fraud-screening pipeline. Per merchant it calls OpenAI web
search (model `gpt 5.4`) with a **fixed 48-question prompt**, several runs per merchant,
then majority-votes the answers. Average ~14 web searches per merchant per run.

**This repo does not run that pipeline.** It parses that team's logs and outputs to answer
one question: *which of those searches can be cut, and what does that save?*

Scale: ~20k merchants, ~2 GB zipped logs. Stream everything; never assume a file fits in
memory. 2 GB is small enough that stdlib `sqlite3` handles all storage and analysis.

## Core invariants (never violate)

1. **Never crash on malformed input. Never silently drop it either.** Every unparseable
   thing gets an `anomalies` row with full raw context, and the record is still stored with
   a confidence marker. Non-zero anomaly counts are the expected steady state, not a bug.
2. **Retain raw text always** — `raw_action_line`, `queries_raw`, `raw_json`. This is what
   makes `coa reparse` possible (re-extract fields from SQLite without re-reading 2 GB).
3. **Never commit merchant PII.** `coa.sqlite`, `reports/`, `data/` are gitignored at the
   repo root. Check before every commit.
4. **Pricing constants stay `null`** until an operator fills them from the billing
   dashboard. Reports print an `UNVERIFIED PRICING` banner while they are null, and still
   show call-count-based relative shares, which need no prices.
5. **Nothing unverifiable goes in a report.** The output is an argument aimed at another
   team's budget; an unmeasurable heuristic is the weakest link. Prefer an exact, narrower
   claim over an inferred, broader one — and label every heuristic that does survive.

## The air-gap (shapes everything)

Real data lives in an environment Claude Code **never sees**. Development happens entirely
against synthetic fixtures. Reality reaches the parser through exactly one channel: the
operator runs `coa anomalies show CODE`, pastes the output back into a Claude Code session,
the parser gets patched, and the new case is added to the fixture generator as a regression.

Consequences:
- `tests/fixtures/gen_fixtures.py` **is** reality during development. Over-invest in it.
  Adding a newly-discovered case must be a ~3-line diff plus a golden count.
- The anomaly CLI output is a UX surface, not a formatting detail. If it emits 5000 lines,
  the loop breaks. Keep it deduped, capped, and paste-ready.
- Assume reality contradicts at least some format assumption in `PLAN.md`. Budget for it.

## Stack

- Python 3.13, stdlib `sqlite3` + `zipfile` + `re` + `csv`.
- Dependencies: `pyyaml`, `pytest`, `ruff`. **That is the whole list.** Adding one requires
  a stated trigger — see the reversal triggers in the plan; heavier options
  (rapidfuzz/sklearn/sentence-transformers, dnspython/whois, pandas/duckdb) were all
  deliberately deferred behind measurements, not forgotten.
- Layout: `src/coa/{config,anomalies,db,logs,outputs,inputs,normalize,metrics,report,cli}.py`

## Architectural rules

- **Parsers take `(src_name, lines_iterable)` — never a path.** `cli.py` owns all path and
  zip walking. This keeps `logs.py` / `outputs.py` / `inputs.py` pure over line iterators so
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
