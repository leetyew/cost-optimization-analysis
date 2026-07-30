"""Deterministic synthetic-fixture generator.

Claude Code never sees the real data, so this file *is* reality during
development. Every format surprise the operator reports gets added here as a
regression case — that loop is the core of the project, so adding a case is meant
to cost about three lines: append to a CASES list, bump a golden count.

Produces a miniature `data/cost_optimization/` tree:

    logs.zip           2 x .log   (clean pairs, orphans, wraps, junk, bad bytes)
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
import zipfile
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
# Log generation
# ---------------------------------------------------------------------------

NOISE_LINES = [
    "loading configuration from /etc/app/config.yaml",
    "  ",
    "=====",
    "cache warm complete",
]


class LogBuilder:
    """Accumulates log lines and tallies what was planted, for the golden file."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.lines: list[str] = []
        self.tally: Counter[str] = Counter()
        self._clock = 0

    def _ts(self) -> str:
        """Monotonic fake clock. Minute-scale steps keep run bursts separable."""
        self._clock += self.rng.randint(1, 20)
        h, rem = divmod(self._clock, 3600)
        mnt, sec = divmod(rem, 60)
        return f"2026-07-30 {10 + h % 12:02d}:{mnt:02d}:{sec:02d},{self.rng.randint(0, 999):03d}"

    def ws_line(self, se10: str, ws_id: str) -> None:
        """The `Response tool type - web_search_call` line that enables strict pairing."""
        self.lines.append(
            f"{self._ts()} | INFO | app.search | [{se10}] "
            f"Response tool type - web_search_call, id - {ws_id}"
        )
        self.tally["web_search_call"] += 1

    def action_search(self, query: str, queries: list[str], *, strict: bool) -> None:
        qs = " , ".join(queries)
        self.lines.append(f"action type - search, query - {query}, queries - {qs}")
        self.tally["action_search"] += 1
        self.tally["strict" if strict else "orphan"] += 1

    def action_open_page(self, url: str, *, strict: bool) -> None:
        self.lines.append(f"action type - open_page, url - {url}")
        self.tally["action_open_page"] += 1
        self.tally["strict" if strict else "orphan"] += 1

    def action_find_in_page(self, url: str, pattern: str, *, spelling: str, strict: bool) -> None:
        self.lines.append(f"action type - {spelling}, url - {url}, pattern - {pattern}")
        self.tally["action_find_in_page"] += 1
        self.tally["strict" if strict else "orphan"] += 1

    def noise(self, n: int = 1) -> None:
        for _ in range(n):
            self.lines.append(self.rng.choice(NOISE_LINES))
            self.tally["noise"] += 1

    def other_timestamped(self, se10: str, msg: str) -> None:
        """A timestamped line that is NOT a web_search_call — breaks strict pairing."""
        self.lines.append(f"{self._ts()} | DEBUG | app.core | [{se10}] {msg}")
        self.tally["other_timestamped"] += 1


def build_clean_log(merchants: list[dict], rng: random.Random) -> LogBuilder:
    """Log file 1: the happy path, plus benign noise between call groups.

    Every action line here is immediately preceded by its web_search_call line, so
    all of it must come out strict-paired. This file establishes the baseline the
    pairing-loss KPI is measured against.
    """
    lb = LogBuilder(rng)
    for m in merchants:
        n_runs = 1 + (int(m["se10"]) % 4)  # run counts 1..4
        for run in range(n_runs):
            for q in queries_for(m, rng):
                ws = f"ws_{m['se10'][-4:]}{run}{rng.randint(100, 999)}"
                lb.ws_line(m["se10"], ws)
                lb.action_search(q, [q], strict=True)
            # open_page / find in page: token cost but (believed) no per-call fee
            ws = f"ws_{m['se10'][-4:]}{run}op"
            lb.ws_line(m["se10"], ws)
            lb.action_open_page(f"https://example.test/{m['se10']}", strict=True)
        lb.noise(rng.randint(0, 2))
    return lb


def build_messy_log(merchants: list[dict], rng: random.Random) -> LogBuilder:
    """Log file 2: one planted instance of every parsing hazard in PLAN.md §10.

    Each block below is a named hazard. Keeping them explicit (rather than
    randomly sprinkled) is what makes a golden count meaningful — a test can
    assert "exactly one COMMA_IN_QUERY" and mean it.
    """
    lb = LogBuilder(rng)
    m = merchants[0]
    se10 = m["se10"]
    name = m["se_toc_name"]

    # 1. Baseline strict pair, so this file is not pathological end to end.
    lb.ws_line(se10, "ws_base001")
    lb.action_search(f"{name} scam", [f"{name} scam"], strict=True)

    # 2. ORPHAN_ACTION: action at file position with no preceding web_search_call.
    lb.noise(1)
    lb.action_search(f"{name} orphaned", [f"{name} orphaned"], strict=False)

    # 3. ORPHAN via async interleaving: another merchant's timestamped line cuts in
    #    between the web_search_call and its action. This is the race the operator
    #    described, and the reason pairing loss is a reported KPI.
    lb.ws_line(se10, "ws_race001")
    lb.other_timestamped(merchants[1]["se10"], "heartbeat from concurrent worker")
    lb.action_search(f"{name} raced", [f"{name} raced"], strict=False)

    # 4. COMMA_IN_QUERY: the query itself contains a comma, so naive comma-splitting
    #    of `queries` breaks it. The singular `query` is the ground truth used to
    #    repair the split — the one honest signal available.
    comma_q = f"{name}, {m['city']} reviews"
    lb.ws_line(se10, "ws_comma01")
    lb.lines.append(f"action type - search, query - {comma_q}, queries - {comma_q} , {name} scam")
    lb.tally["action_search"] += 1
    lb.tally["strict"] += 1
    lb.tally["comma_in_query"] += 1

    # 5. Embedded double quotes: quotes are CONTENT here, not delimiters. A
    #    quote-aware CSV parse would mangle this, which is why §3 forbids one.
    quoted = f'"{name}" complaints'
    lb.ws_line(se10, "ws_quote01")
    lb.action_search(quoted, [quoted, f"{name} scam"], strict=True)
    lb.tally["embedded_quotes"] += 1

    # 6. Unquoted junk item in `queries` alongside a sane one.
    lb.ws_line(se10, "ws_junk001")
    lb.lines.append(
        f"action type - search, query - {name} lawsuit, "
        f'queries - {name} lawsuit , "asdasd" asdasd , '
    )
    lb.tally["action_search"] += 1
    lb.tally["strict"] += 1
    lb.tally["junk_query_item"] += 1

    # 7. UNKNOWN_ACTION_TYPE: stored verbatim, never dropped.
    lb.ws_line(se10, "ws_unk0001")
    lb.lines.append("action type - summarize_page, url - https://example.test/x")
    lb.tally["action_unknown"] += 1
    lb.tally["strict"] += 1

    # 8. POSSIBLE_WRAPPED_ACTION: a non-noise line directly after an action line,
    #    which may be a wrapped continuation. Both raw forms are kept; field
    #    extraction is NOT silently re-run on the merged line.
    lb.ws_line(se10, "ws_wrap001")
    lb.action_search(f"{name} bankruptcy", [f"{name} bankruptcy"], strict=True)
    lb.lines.append("filings public record search continued")
    lb.tally["possible_wrap"] += 1

    # 9. ACTION_FIELD_MISSING: open_page with no url field.
    lb.ws_line(se10, "ws_miss001")
    lb.lines.append("action type - open_page")
    lb.tally["action_open_page"] += 1
    lb.tally["strict"] += 1
    lb.tally["field_missing"] += 1

    # 10. Both find-in-page spellings, one strict each.
    for spelling in ("find in page", "find_in_page"):
        lb.ws_line(se10, f"ws_fip{len(spelling):03d}")
        lb.action_find_in_page(
            "https://example.test/report", "chargeback", spelling=spelling, strict=True
        )

    # 11. MULTI_QUERIES_MARKER: `, queries - ` appears twice; the LAST one wins.
    lb.ws_line(se10, "ws_multi01")
    lb.lines.append(
        f"action type - search, query - {name} , queries - refund policy, "
        f"queries - {name} , queries - refund policy"
    )
    lb.tally["action_search"] += 1
    lb.tally["strict"] += 1
    lb.tally["multi_queries_marker"] += 1

    # 12. QUERY_NOT_IN_QUERIES: the operator believes this never happens. Plant one
    #     so the detector itself is proven to fire.
    lb.ws_line(se10, "ws_notin01")
    lb.lines.append(
        f"action type - search, query - {name} tax liens, queries - unrelated other query"
    )
    lb.tally["action_search"] += 1
    lb.tally["strict"] += 1
    lb.tally["query_not_in_queries"] += 1

    # A few more clean merchants so this file has usable bulk too.
    for other in merchants[16:22]:
        for q in queries_for(other, rng)[:2]:
            lb.ws_line(other["se10"], f"ws_{other['se10'][-4:]}xx")
            lb.action_search(q, [q], strict=True)

    return lb


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
        "answers": answers,
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

    golden: dict = {"n_merchants": len(merchants), "n_questions": N_QUESTIONS}

    # --- logs.zip -----------------------------------------------------------
    clean = build_clean_log(merchants[:15], random.Random(SEED + 1))
    messy = build_messy_log(merchants, random.Random(SEED + 2))

    clean_bytes = ("\n".join(clean.lines) + "\n").encode("utf-8")
    # A stray non-UTF8 byte: ingest must read with errors="replace" and record
    # ENCODING rather than crash or skip the file.
    messy_bytes = ("\n".join(messy.lines) + "\n").encode("utf-8")
    # Damage the heartbeat message body, not a noise line. Corrupting a noise line
    # would stop it matching config.noise_patterns, so it would also trip the
    # wrapped-action heuristic and two hazards would share one line — correct
    # behaviour, but it makes both counts untestable in isolation.
    assert b"heartbeat from" in messy_bytes, "ENCODING hazard lost its injection target"
    messy_bytes = messy_bytes.replace(b"heartbeat from", b"heart\xffbeat from", 1)

    # Pin the member timestamps. writestr() otherwise stamps time.localtime(), so
    # the archive bytes would differ on every run — which both breaks the
    # determinism guarantee the golden counts rest on and produces a spurious git
    # diff every time anyone regenerates the fixtures.
    with zipfile.ZipFile(DATA_ROOT / "logs.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for member, payload in (
            ("logs/clean_001.log", clean_bytes),
            ("logs/messy_002.log", messy_bytes),
        ):
            info = zipfile.ZipInfo(member, date_time=(2026, 7, 30, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, payload)

    golden["logs"] = {
        "clean": dict(clean.tally),
        "messy": dict(messy.tally),
        "total_action_lines": (
            sum(
                clean.tally[k] for k in ("action_search", "action_open_page", "action_find_in_page")
            )
            + sum(
                messy.tally[k]
                for k in (
                    "action_search",
                    "action_open_page",
                    "action_find_in_page",
                    "action_unknown",
                )
            )
        ),
        "total_strict": clean.tally["strict"] + messy.tally["strict"],
        "total_orphan": clean.tally["orphan"] + messy.tally["orphan"],
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
            "ORPHAN_ACTION",
            "UNKNOWN_ACTION_TYPE",
            "POSSIBLE_WRAPPED_ACTION",
            "ACTION_FIELD_MISSING",
            "COMMA_IN_QUERY",
            "MULTI_QUERIES_MARKER",
            "QUERY_NOT_IN_QUERIES",
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
    print(f"  action lines     {g['logs']['total_action_lines']}")
    print(f"  strict / orphan  {g['logs']['total_strict']} / {g['logs']['total_orphan']}")
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
