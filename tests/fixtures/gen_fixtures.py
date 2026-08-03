"""Deterministic synthetic-fixture generator.

Claude Code never sees the real data, so this file *is* reality during
development. Every format surprise the operator reports gets added here as a
regression case — that loop is the core of the project, so adding a case is meant
to cost about three lines: append to a CASES list, bump a golden count.

Produces a miniature `data/cost_optimization/` tree:

    logs/jsonl/*.jsonl 2 files    (runs, token usage, web_search_calls, bad bytes)
    input/*.jsonl      2 files    (merchant details, one duplicate se10)
    output/*.jsonl     2 files    (answers, citations, votes, drift, bad JSON)

plus `golden.json`: counts of exactly what was planted, which the tests assert
against. The generator knows what it wrote, so the golden file is derived rather
than hand-maintained.

Run:  python tests/fixtures/gen_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "data"
DATA_ROOT = FIXTURE_ROOT / "cost_optimization"
SEED = 20260730

# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------

CITIES = ["Springfield", "Riverton", "Fairview", "Kingsport", "Ashland"]
STATES = ["IL", "WY", "OR", "TN", "OH"]
INDUSTRIES = ["retail", "restaurant", "e-commerce", "auto repair", "salon"]
NAME_PARTS = [
    ("Acme", "Widgets"),
    ("Blue", "Harbor"),
    ("Copper", "Kettle"),
    ("Delta", "Motors"),
    ("Evergreen", "Supply"),
    ("Falcon", "Tools"),
    ("Granite", "Foods"),
    ("Harbor", "Lane"),
    ("Ivory", "Studio"),
    ("Juniper", "Goods"),
]
SUFFIXES = ["LLC", "Inc", "Co"]

N_MERCHANTS = 30


def build_merchants(rng: random.Random) -> list[dict]:
    """Thirty merchants with PII designed to exercise the templating rules.

    Phones get inconsistent formatting on purpose (dashes vs parens vs bare
    digits) so the digits-only matching path in normalize.py is actually tested
    rather than assumed.
    """
    merchants = []
    for i in range(N_MERCHANTS):
        se10 = str(1000000001 + i)
        first, second = NAME_PARTS[i % len(NAME_PARTS)]
        name = f"{first} {second} {SUFFIXES[i % len(SUFFIXES)]}"
        city = CITIES[i % len(CITIES)]
        digits = f"555{2000000 + i * 7919:07d}"[:10]
        # Three formats across the corpus; the parser must match all of them.
        phone = [
            f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
            f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
            digits,
        ][i % 3]
        domain = f"{first.lower()}{second.lower()}.example"
        merchants.append(
            {
                "se10": se10,
                "se_toc_name": name,
                "opening_date": f"20{15 + i % 8}-0{1 + i % 9}-1{i % 9}",
                "city": city,
                "state": STATES[i % len(STATES)],
                "country": "US",
                "industry_tagged": INDUSTRIES[i % len(INDUSTRIES)],
                "sub_category": f"sub_{i % 4}",
                "email": f"contact@{domain}",
                "phone": phone,
                "street": f"{100 + i * 3} {second} Street",
                "signer_name": f"Pat {second}",
                "owner_name": f"Casey {first}",
                "owner_city": city,
                "owner_postal": f"{60000 + i * 11}",
                "owner_street": f"{200 + i * 5} Elm Avenue",
                "website": f"https://{domain}",
                # "many more keys" — the parser must preserve what it does not name.
                "internal_risk_bucket": rng.choice(["low", "medium", "high"]),
                "legacy_flag": rng.choice([0, 1]),
            }
        )
    return merchants


# ---------------------------------------------------------------------------
# Questions / answers
# ---------------------------------------------------------------------------

# (stem, kind). Two answer regimes, both operator-confirmed — see CLAUDE.md
# "Known real-data format facts":
#   scale -> 1-5, defaulting to 3 when evidence is insufficient, and Evidence is
#            returned ONLY when the answer is <= 3.
#   text  -> a free-text value or a literal NULL.
# The 6:2 split here puts the corpus at 36 scale / 12 text questions.
QUESTION_SPECS = [
    ("Is the merchant a legitimate business", "scale"),
    ("Does the merchant have negative reviews", "scale"),
    ("Is the merchant associated with fraud complaints", "scale"),
    ("What is the registered address of the merchant", "text"),
    ("Is the phone number registered to the business", "scale"),
    ("What type of building is at the registered address", "text"),
    ("Is the merchant listed with the BBB", "scale"),
    ("Are there lawsuits involving the merchant", "scale"),
]
N_QUESTIONS = 48

# The answer-format instruction rides along inside each question's text, exactly
# as it does in the real prompt. That matters downstream: the question extraction
# regex captures it, so `questions.text` is what P4 classifies scale-vs-text from
# rather than a hard-coded question-number list that would rot on any reordering.
SCALE_INSTRUCTION = (
    "Answer 1-5 (default: 3 if insufficient evidence)\n"
    "Evidence: Return evidence if Answer is less than or equal to 3, otherwise return NULL."
)
TEXT_INSTRUCTION = "Return: value | NULL"


def question_kinds() -> list[str]:
    """Per-qnum answer regime, indexed qnum-1. Drives both rendering and goldens."""
    return [QUESTION_SPECS[i % len(QUESTION_SPECS)][1] for i in range(N_QUESTIONS)]


def build_questions() -> list[str]:
    """The fixed 48-question prompt, stable across records except where drift is planted."""
    out = []
    for i in range(N_QUESTIONS):
        stem, kind = QUESTION_SPECS[i % len(QUESTION_SPECS)]
        instruction = SCALE_INSTRUCTION if kind == "scale" else TEXT_INSTRUCTION
        out.append(f"{stem} (variant {i // len(QUESTION_SPECS) + 1})?\n{instruction}")
    return out


def user_prompt(questions: list[str]) -> str:
    """Render the numbered question block the output parser extracts from.

    Questions are multi-line (they carry their answer-format instruction), so the
    §4 extraction regex has to rely on the `\\nQ<n>.` lookahead rather than on one
    question per line. Keeping that true here is the point of the fixture.
    """
    return "Answer each question.\n" + "\n".join(f"Q{i}. {q}" for i, q in enumerate(questions, 1))


SYSTEM_PROMPT = "You are a merchant risk analyst. Answer each question with A<n>. and Evidence."


# ---------------------------------------------------------------------------
# Query archetypes — what the clustering layer must collapse
# ---------------------------------------------------------------------------


def queries_for(m: dict, rng: random.Random) -> list[str]:
    """Realistic per-merchant search queries.

    Deliberately spans templated archetypes (name/city/phone/email based) and one
    generic query that contains no PII at all — the latter must survive templating
    verbatim and land in the "no placeholder fired" bucket.
    """
    name = m["se_toc_name"]
    pool = [
        f"{name} scam",
        f"{name} reviews",
        f"{name} fraud complaints",
        f"{name} {m['city']} address",
        f"{m['phone']} business listing",
        f"{m['email']} domain owner",
        f"{name} BBB rating",
        "BBB complaints database",  # generic: no placeholder should fire
    ]
    k = rng.randint(3, 6)
    return rng.sample(pool, k)


# ---------------------------------------------------------------------------
# Web-search logs (logs/jsonl/*.jsonl)
# ---------------------------------------------------------------------------

SERVICE_TIERS = ["standard", "flex", "priority"]


def build_web_search_calls(m: dict, rng: random.Random, n_searches: int) -> list[dict]:
    """One run's `web_search_calls` array.

    Mirrors the real shape per action type: `search` carries `query` + `queries`,
    `open_page` carries `url`, `find_in_page` carries `details`. The corpus-wide
    ratio is roughly 85/13/2, so the counts here follow that rather than being
    uniform — position-in-run analysis depends on the mix being realistic.
    """
    calls = []
    pool = queries_for(m, rng)
    for i in range(n_searches):
        q = pool[i % len(pool)]
        # `queries` is a set of sub-queries WITHIN one billed call, not a
        # cumulative history: real runs show a constant length with differing
        # members. The singular `query` is the billed unit and appears among them.
        others = [x for x in pool if x != q]
        rng.shuffle(others)
        calls.append(
            {
                "id": f"ws_{m['se10'][-4:]}_{i:02d}",
                "status": "completed",
                "action_type": "search",
                "query": q,
                "queries": [q, *others[:3]],
            }
        )
    for i in range(max(1, n_searches // 7)):
        calls.append(
            {
                "id": f"op_{m['se10'][-4:]}_{i:02d}",
                "status": "completed",
                "action_type": "open_page",
                "url": f"https://{m['se_toc_name'].split()[0].lower()}-source{i}.example/page",
            }
        )
    if rng.random() < 0.3:
        calls.append(
            {
                "id": f"fp_{m['se10'][-4:]}",
                "status": "completed",
                "action_type": "find_in_page",
                "details": f"pattern: chargeback | url: https://example.test/{m['se10']}",
            }
        )
    return calls


def build_weblog_record(m: dict, rng: random.Random, *, n_runs: int) -> tuple[dict, Counter]:
    """One `logs/jsonl` line: {se10: {run_k: {usage_metadata, ..., calls}}}."""
    tally: Counter[str] = Counter()
    runs = {}
    for run in range(n_runs):
        calls = build_web_search_calls(m, rng, rng.randint(3, 6))
        # cache_read is a SUBSET of input_tokens and reasoning a SUBSET of
        # output_tokens, so input + output == total holds exactly. A generator
        # that got this wrong would hide the double-counting bug it exists to catch.
        cache_read = rng.randint(0, 4000)
        input_tokens = cache_read + rng.randint(1000, 6000)
        reasoning = rng.randint(0, 500)
        output_tokens = reasoning + rng.randint(200, 1500)
        runs[f"run_{run}"] = {
            "usage_metadata": {
                "service_tier": SERVICE_TIERS[int(m["se10"]) % len(SERVICE_TIERS)],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cache_read": cache_read,
                "reasoning": reasoning,
            },
            # They did not opt into reasoning summaries, so content is empty. The
            # keys are still present, which is what the parser must tolerate.
            "response_reasoning": {
                "id": f"rs_{m['se10']}",
                "type": "reasoning",
                "summary": [],
                "content": [],
            },
            "web_search_calls": calls,
        }
        tally["runs"] += 1
        tally["calls"] += len(calls)
        for c in calls:
            tally[f"action_{c['action_type']}"] += 1
        tally["input_tokens"] += input_tokens
        tally["output_tokens"] += output_tokens
        tally["cache_read"] += cache_read
        tally["reasoning"] += reasoning
    tally["merchants"] += 1
    return {m["se10"]: runs}, tally


def build_weblogs(merchants: list[dict], rng: random.Random) -> tuple[list[dict], Counter]:
    """Records for every merchant, plus the planted hazards.

    Hazards are applied to existing records rather than appended, so merchant
    counts stay assertable — the same discipline the output fixtures use.
    """
    records, tally = [], Counter()
    for i, m in enumerate(merchants):
        rec, t = build_weblog_record(m, rng, n_runs=1 + (i % 3))
        records.append(rec)
        tally.update(t)

    # UNKNOWN_ACTION_TYPE: stored verbatim, never dropped.
    first_run = next(iter(records[1].values()))["run_0"]
    first_run["web_search_calls"].append(
        {
            "id": "unk_0001",
            "status": "completed",
            "action_type": "summarize_page",
            "url": "https://example.test/x",
        }
    )
    tally["calls"] += 1
    tally["action_summarize_page"] += 1
    tally["planted_unknown_action"] += 1

    # QUERY_NOT_IN_QUERIES: the operator believes this never happens.
    call = next(iter(records[2].values()))["run_0"]["web_search_calls"][0]
    call["queries"] = ["an unrelated query", "another one"]
    tally["planted_query_not_in_queries"] += 1

    # CALL_STATUS_NOT_COMPLETED: does an incomplete call still bill?
    next(iter(records[3].values()))["run_0"]["web_search_calls"][0]["status"] = "failed"
    tally["planted_status_not_completed"] += 1

    # CALL_FIELD_MISSING: a search with no query cannot be attributed.
    del next(iter(records[4].values()))["run_0"]["web_search_calls"][0]["query"]
    tally["planted_field_missing"] += 1

    # TOKEN_SUM_MISMATCH: the subset assumption the whole cost model rests on.
    usage = next(iter(records[5].values()))["run_0"]["usage_metadata"]
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"] + usage["reasoning"]
    tally["planted_token_sum_mismatch"] += 1

    # MISSING_USAGE_METADATA: a run whose tokens cannot be costed at all. Its
    # tokens leave the golden totals with it, or the goldens would assert a sum
    # the corpus no longer contains.
    orphaned = next(iter(records[6].values()))["run_0"].pop("usage_metadata")
    for key in ("input_tokens", "output_tokens", "cache_read", "reasoning"):
        tally[key] -= orphaned[key]
    tally["planted_no_usage_metadata"] += 1

    # RUN_KEY_UNPARSED: a run key that is not run_<n>.
    runs = next(iter(records[7].values()))
    runs["retry_final"] = runs.pop("run_0")
    tally["planted_run_key_unparsed"] += 1

    return records, tally


# ---------------------------------------------------------------------------
# Output records
# ---------------------------------------------------------------------------


BUILDING_TYPES = [
    "Standalone retail storefront",
    "Multi-tenant office building",
    "Residential apartment unit",
    "Industrial warehouse unit",
]


def answer_block(qnum: int, answer: str, evidence: str | None) -> str:
    """One Q/A/Evidence block.

    `evidence=None` omits the Evidence line entirely and `evidence=""` leaves the
    label bare. Which of the three shapes the real logs use when an answer needs
    no evidence is still unconfirmed, so all three are planted and the parser must
    survive each — an omitted line is the dangerous one, because a regex that
    requires `\\nEvidence.` silently loses the whole block.
    """
    head = f"Q{qnum}. question text\nA{qnum}. {answer}"
    if evidence is None:
        return head
    return f"{head}\nEvidence." if evidence == "" else f"{head}\nEvidence. {evidence}"


def _no_evidence_shape(qnum: int) -> str | None:
    """Which empty-evidence rendering this qnum uses. Deterministic, not random.

    Keying off qnum rather than the RNG means each shape lands on a fixed set of
    questions in every run, so the golden counts are stable and a test can point
    at one specific qnum to exercise one specific shape.
    """
    if qnum % 17 == 0:
        return None  # line omitted entirely
    if qnum % 19 == 0:
        return ""  # label present, value empty
    return "NULL"  # literal token


def build_run_text(
    m: dict,
    kinds: list[str],
    rng: random.Random,
    *,
    n_blocks: int = N_QUESTIONS,
) -> tuple[str, list[dict], dict[int, str], Counter]:
    """Render one run's answer text, prose citations, answers by qnum, and a tally.

    Returns what a correct parser should recover, so the golden counts are derived
    from what was written rather than restating the parser's own behaviour.

    The answer regimes mirror the real prompt: scale questions answer 1-5 and carry
    evidence only when <= 3; text questions return a value or a literal NULL. The
    weighting leans on 3 because "defaulted to 3 for want of evidence" is the
    signal the whole cost argument is built on — it needs bulk to be measurable.
    """
    blocks, cites = [], []
    answers_by_q: dict[int, str] = {}
    tally: Counter[str] = Counter()

    for qnum in range(1, n_blocks + 1):
        kind = kinds[qnum - 1]
        if kind == "scale":
            value = rng.choices([1, 2, 3, 4, 5], weights=[1, 2, 4, 2, 2])[0]
            answer = str(value)
            tally["scale_answers"] += 1
            if value == 3:
                tally["scale_default_3"] += 1
            has_evidence = value <= 3
        else:
            tally["text_answers"] += 1
            if rng.random() < 0.25:
                answer, has_evidence = "NULL", False
            elif qnum % 2:
                # Address-shaped: commas inside a free-text answer, plus real PII
                # for P3's templating to bite on.
                answer, has_evidence = f"{m['street']}, {m['city']}, {m['state']}", True
            else:
                answer, has_evidence = rng.choice(BUILDING_TYPES), True

        answers_by_q[qnum] = answer
        if answer == "NULL":
            tally["null_answers"] += 1

        if not has_evidence:
            evidence = _no_evidence_shape(qnum)
            tally[
                {None: "evidence_absent", "": "evidence_empty"}.get(evidence, "evidence_null")
            ] += 1
            blocks.append(answer_block(qnum, answer, evidence))
            continue

        domain = f"{m['se_toc_name'].split()[0].lower()}-source{qnum % 5}.example"
        url = f"https://{domain}/page{qnum}"
        if rng.random() < 0.1:
            # Empty markdown placeholder — the observed `([]())` artefact.
            evidence = "Found supporting detail ([]())"
            cites.append({"qnum": qnum, "url": "", "empty": True})
            tally["empty_placeholders"] += 1
        else:
            evidence = f"Reported publicly ([{domain} report]({url}))"
            cites.append({"qnum": qnum, "url": url, "empty": False})
            tally["prose_citations"] += 1
        tally["evidence_present"] += 1
        blocks.append(answer_block(qnum, answer, evidence))

    tally["answer_blocks"] += n_blocks
    return "\n\n".join(blocks), cites, answers_by_q, tally


def build_output_record(
    m: dict,
    questions: list[str],
    kinds: list[str],
    rng: random.Random,
    *,
    n_runs: int,
    drop_one_block: bool = False,
    drop_citation_from_dict: bool = False,
    vote_list: bool = False,
    empty_voted_final: bool = False,
    answer_parse_mismatch: bool = False,
    citation_shape_list: bool = False,
    website_conflict: bool = False,
) -> tuple[dict, Counter]:
    """One output/*.jsonl record, with optional planted defects."""
    answers, answer_dict, citation_evidence = {}, {}, {}
    tally: Counter[str] = Counter()
    for run in range(n_runs):
        key = f"run_{run}"
        n_blocks = N_QUESTIONS - 1 if (drop_one_block and run == 0) else N_QUESTIONS
        text, cites, answers_by_q, run_tally = build_run_text(m, kinds, rng, n_blocks=n_blocks)
        answers[key] = text
        tally.update(run_tally)
        tally["runs"] += 1
        # Their own parse of the same prose. Holding the real answer (rather than a
        # constant) is what makes agree_with_dict a signal instead of noise: with a
        # constant, 75% of answers disagreed for no reason and ANSWER_PARSE_MISMATCH
        # drowned in artefact.
        answer_dict[key] = {f"A{q}": a for q, a in answers_by_q.items()}
        if answer_parse_mismatch and run == 0:
            answer_dict[key]["A1"] = "5" if answer_dict[key].get("A1") != "5" else "1"
            tally["planted_parse_mismatch"] += 1
        # citation_evidence is the second, independent citation source. Dropping
        # entries here (but not from the prose) is what CITATION_SOURCE_MISMATCH
        # must catch — it measures how lossy their post-processing is.
        keep = cites[:-2] if (drop_citation_from_dict and run == 0) else cites
        citation_evidence[key] = [
            {
                "question": f"Q{c['qnum']}",
                "a_key": f"A{c['qnum']}",
                "answer": answers_by_q[c["qnum"]],
                "citation": c["url"] or None,
                "evidence": "supporting text",
                "full_answer_block": answer_block(c["qnum"], answers_by_q[c["qnum"]], "e"),
            }
            for c in keep
        ]
        # §4 allows `citation` to be a string or null and says ANY other shape is an
        # anomaly. A two-URL list is the cheapest way to prove that detector fires.
        if citation_shape_list and run == 0 and citation_evidence[key]:
            first = citation_evidence[key][0]
            first["citation"] = [f"https://a.example/{m['se10']}", f"https://b.example/{m['se10']}"]
            tally["planted_citation_shape"] += 1

    voted_majority = {f"A{q}": "Yes" for q in range(1, N_QUESTIONS + 1)}
    if vote_list:
        # Observed shape: a list of identical values. Normalizes to a scalar.
        voted_majority["A2"] = ["NULL", "NULL"]
        # Genuinely mixed list must NOT normalize — it stays JSON + anomaly. Note it
        # lands in voted_final too, so the detector has to dedupe per (record, qnum).
        voted_majority["A3"] = ["Yes", "No"]
    voted_final = {} if empty_voted_final else dict(voted_majority)
    if not empty_voted_final:
        voted_final["A5"] = "No"  # deliberate majority/final divergence

    record = {
        "se10": m["se10"],
        "n_runs": n_runs,
        "question": [[SYSTEM_PROMPT, user_prompt(questions)]],
        # Real records use the singular `answer` (operator-confirmed 2026-08-03);
        # PLAN.md §4 said `answers`. The generator follows reality, and
        # outputs.ANSWER_KEYS still tolerates the other spelling.
        "answer": answers,
        "answer_dict": {**answer_dict, "citation_evidence": citation_evidence},
        "voted_majority": voted_majority,
        "voted_final": voted_final,
        # Convenience keys duplicated from input/. input/ wins on conflict, so a
        # divergence here must surface as INPUT_OUTPUT_FIELD_CONFLICT, never be
        # written over the merchant row.
        "website": "https://conflicting.example" if website_conflict else m["website"],
        "industry": m["industry_tagged"],
        "state": m["state"],
    }
    if website_conflict:
        tally["planted_field_conflict"] += 1
    return record, tally


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def write_fixtures() -> dict:
    """Generate the whole tree and return the golden counts."""
    rng = random.Random(SEED)
    merchants = build_merchants(rng)
    questions = build_questions()
    kinds = question_kinds()

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "input").mkdir(exist_ok=True)
    (DATA_ROOT / "output").mkdir(exist_ok=True)
    (DATA_ROOT / "logs" / "jsonl").mkdir(parents=True, exist_ok=True)

    golden: dict = {"n_merchants": len(merchants), "n_questions": N_QUESTIONS}

    # --- logs/jsonl/*.jsonl --------------------------------------------------
    weblogs, wl_tally = build_weblogs(merchants, random.Random(SEED + 1))
    # Split across two files so per-file resumability is exercised, and inject a
    # bad JSON line plus a non-UTF8 byte: ingest must record BAD_JSON_LINE and
    # ENCODING rather than crash or skip the file.
    half = len(weblogs) // 2
    _write_jsonl(DATA_ROOT / "logs" / "jsonl" / "calls_001.jsonl", weblogs[:half])
    _write_jsonl(DATA_ROOT / "logs" / "jsonl" / "calls_002.jsonl", weblogs[half:], bad_json_after=2)
    damaged = DATA_ROOT / "logs" / "jsonl" / "calls_001.jsonl"
    payload = damaged.read_bytes()
    assert b"chargeback" in payload, "ENCODING hazard lost its injection target"
    damaged.write_bytes(payload.replace(b"chargeback", b"charge\xffback", 1))

    golden["weblogs"] = {
        "merchants": wl_tally["merchants"],
        "runs": wl_tally["runs"],
        "calls": wl_tally["calls"],
        "action_search": wl_tally["action_search"],
        "action_open_page": wl_tally["action_open_page"],
        "action_find_in_page": wl_tally["action_find_in_page"],
        "action_summarize_page": wl_tally["action_summarize_page"],
        "bad_json_lines": 1,
        "input_tokens": wl_tally["input_tokens"],
        "output_tokens": wl_tally["output_tokens"],
        "cache_read": wl_tally["cache_read"],
        "reasoning": wl_tally["reasoning"],
        "planted_unknown_action": wl_tally["planted_unknown_action"],
        "planted_query_not_in_queries": wl_tally["planted_query_not_in_queries"],
        "planted_status_not_completed": wl_tally["planted_status_not_completed"],
        "planted_field_missing": wl_tally["planted_field_missing"],
        "planted_token_sum_mismatch": wl_tally["planted_token_sum_mismatch"],
        "planted_no_usage_metadata": wl_tally["planted_no_usage_metadata"],
        "planted_run_key_unparsed": wl_tally["planted_run_key_unparsed"],
    }

    # --- input/*.jsonl ------------------------------------------------------
    first = merchants[:20]
    second = merchants[20:]
    _write_jsonl(DATA_ROOT / "input" / "merchants_001.jsonl", first)
    # Duplicate se10 across input files: keep first, flag DUP_INPUT_SE10.
    dup = dict(merchants[0], city="Elsewhere")
    _write_jsonl(DATA_ROOT / "input" / "merchants_002.jsonl", [*second, dup])
    golden["inputs"] = {
        "n_records": len(merchants) + 1,
        "n_unique_se10": len(merchants),
        "dup_input_se10": 1,
    }

    # --- output/*.jsonl -----------------------------------------------------
    # One planted defect per merchant index, so every golden count stays
    # attributable to exactly one record.
    planted: dict[int, dict] = {
        15: {"n_runs": 3, "drop_one_block": True},
        16: {"n_runs": 2, "drop_citation_from_dict": True},
        17: {"n_runs": 2, "vote_list": True},
        18: {"n_runs": 1, "empty_voted_final": True},
        20: {"n_runs": 2, "answer_parse_mismatch": True},
        21: {"n_runs": 2, "citation_shape_list": True},
        22: {"n_runs": 2, "website_conflict": True},
    }
    out_rng = random.Random(SEED + 3)
    out_tally: Counter[str] = Counter()

    def _record(m: dict, **kw) -> dict:
        rec, tally = build_output_record(m, questions, kinds, out_rng, **kw)
        out_tally.update(tally)
        return rec

    records_a = [_record(m, n_runs=1 + (i % 4)) for i, m in enumerate(merchants[:15])]
    records_b = [
        _record(m, **planted.get(i, {"n_runs": 2}))
        for i, m in enumerate(merchants)
        if i >= len(records_a)
    ]

    # Question-set drift in exactly one record. Applied to a record that already
    # exists rather than appended as a new one: appending would give merchants[19]
    # a second record, which both makes dup_output_se10 wrong and lets the
    # "keep the record with most runs" rule discard the drifted copy, so the
    # QUESTION_SET_DRIFT hazard would never fire.
    drifted = list(questions)
    drifted[7] = "Has the merchant changed its legal name recently?\n" + TEXT_INSTRUCTION
    drift_rec = next(r for r in records_b if r["se10"] == merchants[19]["se10"])
    drift_rec["question"] = [[SYSTEM_PROMPT, user_prompt(drifted)]]

    # Duplicate se10 across output files, with differing run counts so the
    # "use the record with most runs" tie-break is actually exercised.
    records_b.append(_record(merchants[0], n_runs=4))

    _write_jsonl(DATA_ROOT / "output" / "results_001.jsonl", records_a)
    _write_jsonl(DATA_ROOT / "output" / "results_002.jsonl", records_b, bad_json_after=2)

    golden["outputs"] = {
        "n_records": len(records_a) + len(records_b),
        "bad_json_lines": 1,
        "dup_output_se10": 1,
        "question_set_drift": 1,
        "answer_block_short_runs": 1,
        "citation_source_mismatch_records": 1,
        "vote_value_list_records": 1,
        "empty_voted_final_records": 1,
        "answer_parse_mismatch_records": out_tally["planted_parse_mismatch"],
        "citation_shape_unexpected_records": out_tally["planted_citation_shape"],
        "input_output_field_conflict_records": out_tally["planted_field_conflict"],
        # Derived from what was actually rendered, so a parser can be checked
        # against the corpus rather than against a restatement of itself.
        "n_runs": out_tally["runs"],
        "answer_blocks": out_tally["answer_blocks"],
        "scale_answers": out_tally["scale_answers"],
        "scale_default_3": out_tally["scale_default_3"],
        "text_answers": out_tally["text_answers"],
        "null_answers": out_tally["null_answers"],
        "evidence_present": out_tally["evidence_present"],
        "evidence_null": out_tally["evidence_null"],
        "evidence_empty": out_tally["evidence_empty"],
        "evidence_absent": out_tally["evidence_absent"],
        "prose_citations": out_tally["prose_citations"],
        "empty_placeholders": out_tally["empty_placeholders"],
    }

    golden["expected_anomaly_codes"] = sorted(
        [
            "ENCODING",
            "UNKNOWN_ACTION_TYPE",
            "QUERY_NOT_IN_QUERIES",
            "CALL_STATUS_NOT_COMPLETED",
            "CALL_FIELD_MISSING",
            "TOKEN_SUM_MISMATCH",
            "MISSING_USAGE_METADATA",
            "RUN_KEY_UNPARSED",
            "BAD_JSON_LINE",
            "DUP_INPUT_SE10",
            "DUP_OUTPUT_SE10",
            "QUESTION_SET_DRIFT",
            "ANSWER_BLOCK_COUNT",
            "CITATION_SOURCE_MISMATCH",
            "VOTE_VALUE_LIST",
            "ANSWER_PARSE_MISMATCH",
            "CITATION_SHAPE_UNEXPECTED",
            "INPUT_OUTPUT_FIELD_CONFLICT",
        ]
    )

    # Fingerprint last, and store it in the golden file. fingerprint() hashes only
    # DATA_ROOT while golden.json lives one level up, so there is no circularity —
    # and baking it in turns any future nondeterminism into a hard test failure
    # instead of a diff nobody notices.
    golden["fingerprint"] = fingerprint()
    (FIXTURE_ROOT / "golden.json").write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
    return golden


def _write_jsonl(path: Path, records: list[dict], bad_json_after: int | None = None) -> None:
    """Write JSONL, optionally injecting one malformed line after N good ones."""
    lines = []
    for i, rec in enumerate(records):
        lines.append(json.dumps(rec, sort_keys=True))
        if bad_json_after is not None and i == bad_json_after:
            lines.append('{"se10": "9999999999", "answers": {truncated...')
    path.write_text("\n".join(lines) + "\n")


def fingerprint() -> str:
    """Hash of the whole generated tree — proves the generator is deterministic."""
    h = hashlib.sha256()
    for p in sorted(DATA_ROOT.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(DATA_ROOT).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


if __name__ == "__main__":
    g = write_fixtures()
    print(f"fixtures written to {DATA_ROOT}")
    print(f"  merchants        {g['n_merchants']}")
    w = g["weblogs"]
    print(f"  weblog runs      {w['runs']} ({w['calls']} calls)")
    print(f"    search         {w['action_search']}  <- the only billed action")
    print(f"    open_page      {w['action_open_page']}, find_in_page {w['action_find_in_page']}")
    print(f"  output records   {g['outputs']['n_records']}")
    o = g["outputs"]
    print(f"  runs / blocks    {o['n_runs']} / {o['answer_blocks']}")
    print(
        f"  scale / text     {o['scale_answers']} / {o['text_answers']} "
        f"(default-3 {o['scale_default_3']}, NULL answers {o['null_answers']})"
    )
    print(
        f"  evidence shapes  present {o['evidence_present']}, NULL {o['evidence_null']}, "
        f"empty {o['evidence_empty']}, absent {o['evidence_absent']}"
    )
    print(f"  anomaly codes    {len(g['expected_anomaly_codes'])}")
    print(f"  fingerprint      {fingerprint()}")
