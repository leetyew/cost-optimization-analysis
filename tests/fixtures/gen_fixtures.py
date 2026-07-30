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

QUESTION_STEMS = [
    "Is the merchant a legitimate business",
    "Does the merchant have negative reviews",
    "Is the merchant associated with fraud complaints",
    "Does the business address match public records",
    "Is the phone number registered to the business",
    "Does the website resolve and appear active",
    "Is the merchant listed with the BBB",
    "Are there lawsuits involving the merchant",
]
N_QUESTIONS = 48


def build_questions() -> list[str]:
    """The fixed 48-question prompt, stable across records except where drift is planted."""
    return [
        f"{QUESTION_STEMS[i % len(QUESTION_STEMS)]} (variant {i // len(QUESTION_STEMS) + 1})?"
        for i in range(N_QUESTIONS)
    ]


def user_prompt(questions: list[str]) -> str:
    """Render the numbered question block the output parser extracts from."""
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


def answer_block(qnum: int, answer: str, evidence: str) -> str:
    """One Q/A/Evidence block in the format the output parser regex expects."""
    return f"Q{qnum}. question text\nA{qnum}. {answer}\nEvidence. {evidence}"


def build_run_text(
    m: dict,
    rng: random.Random,
    *,
    n_blocks: int = N_QUESTIONS,
    null_rate: float = 0.2,
) -> tuple[str, list[dict]]:
    """Render one run's answer text and the citations embedded in its prose.

    Returns the text plus the citation records so the generator knows exactly what
    a correct parser should recover — this is what makes the golden counts real
    rather than a restatement of the parser's own behaviour.
    """
    blocks, cites = [], []
    for qnum in range(1, n_blocks + 1):
        if rng.random() < null_rate:
            blocks.append(answer_block(qnum, "NULL", "NULL"))
            continue
        answer = rng.choice(["Yes", "No", "Likely", "Unclear"])
        domain = f"{m['se_toc_name'].split()[0].lower()}-source{qnum % 5}.example"
        url = f"https://{domain}/page{qnum}"
        if rng.random() < 0.1:
            # Empty markdown placeholder — the observed `([]())` artefact.
            evidence = "Found supporting detail ([]())"
            cites.append({"qnum": qnum, "url": "", "empty": True})
        else:
            evidence = f"Reported publicly ([{domain} report]({url}))"
            cites.append({"qnum": qnum, "url": url, "empty": False})
        blocks.append(answer_block(qnum, answer, evidence))
    return "\n\n".join(blocks), cites


def build_output_record(
    m: dict,
    questions: list[str],
    rng: random.Random,
    *,
    n_runs: int,
    drop_one_block: bool = False,
    drop_citation_from_dict: bool = False,
    vote_list: bool = False,
    empty_voted_final: bool = False,
) -> dict:
    """One output/*.jsonl record, with optional planted defects."""
    answers, answer_dict, citation_evidence = {}, {}, {}
    for run in range(n_runs):
        key = f"run_{run}"
        n_blocks = N_QUESTIONS - 1 if (drop_one_block and run == 0) else N_QUESTIONS
        text, cites = build_run_text(m, rng, n_blocks=n_blocks)
        answers[key] = text
        answer_dict[key] = {f"A{c['qnum']}": "Yes" for c in cites}
        # citation_evidence is the second, independent citation source. Dropping
        # entries here (but not from the prose) is what CITATION_SOURCE_MISMATCH
        # must catch — it measures how lossy their post-processing is.
        keep = cites[:-2] if (drop_citation_from_dict and run == 0) else cites
        citation_evidence[key] = [
            {
                "question": f"Q{c['qnum']}",
                "a_key": f"A{c['qnum']}",
                "answer": "Yes",
                "citation": c["url"] or None,
                "evidence": "supporting text",
                "full_answer_block": answer_block(c["qnum"], "Yes", "e"),
            }
            for c in keep
        ]

    voted_majority = {f"A{q}": "Yes" for q in range(1, N_QUESTIONS + 1)}
    if vote_list:
        # Observed shape: a list of identical values. Normalizes to a scalar.
        voted_majority["A2"] = ["NULL", "NULL"]
        # Genuinely mixed list must NOT normalize — it stays JSON + anomaly.
        voted_majority["A3"] = ["Yes", "No"]
    voted_final = {} if empty_voted_final else dict(voted_majority)
    if not empty_voted_final:
        voted_final["A5"] = "No"  # deliberate majority/final divergence

    return {
        "se10": m["se10"],
        "n_runs": n_runs,
        "question": [[SYSTEM_PROMPT, user_prompt(questions)]],
        "answers": answers,
        "answer_dict": {**answer_dict, "citation_evidence": citation_evidence},
        "voted_majority": voted_majority,
        "voted_final": voted_final,
        "website": m["website"],
        "industry": m["industry_tagged"],
        "state": m["state"],
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def write_fixtures() -> dict:
    """Generate the whole tree and return the golden counts."""
    rng = random.Random(SEED)
    merchants = build_merchants(rng)
    questions = build_questions()

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
    out_rng = random.Random(SEED + 3)
    records_a = [
        build_output_record(m, questions, out_rng, n_runs=1 + (i % 4))
        for i, m in enumerate(merchants[:15])
    ]
    # Planted defects, one merchant each so counts stay assertable.
    records_b = [
        build_output_record(merchants[15], questions, out_rng, n_runs=3, drop_one_block=True),
        build_output_record(
            merchants[16], questions, out_rng, n_runs=2, drop_citation_from_dict=True
        ),
        build_output_record(merchants[17], questions, out_rng, n_runs=2, vote_list=True),
        build_output_record(merchants[18], questions, out_rng, n_runs=1, empty_voted_final=True),
    ]
    for m in merchants[19:]:
        records_b.append(build_output_record(m, questions, out_rng, n_runs=2))

    # Question-set drift in exactly one record. Applied to a record that already
    # exists rather than appended as a new one: appending would give merchants[19]
    # a second record, which both makes dup_output_se10 wrong and lets the
    # "keep the record with most runs" rule discard the drifted copy, so the
    # QUESTION_SET_DRIFT hazard would never fire.
    drifted = list(questions)
    drifted[7] = "Has the merchant changed its legal name recently?"
    drift_rec = next(r for r in records_b if r["se10"] == merchants[19]["se10"])
    drift_rec["question"] = [[SYSTEM_PROMPT, user_prompt(drifted)]]

    # Duplicate se10 across output files, with differing run counts so the
    # "use the record with most runs" tie-break is actually exercised.
    records_b.append(build_output_record(merchants[0], questions, out_rng, n_runs=4))

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
    print(f"  anomaly codes    {len(g['expected_anomaly_codes'])}")
    print(f"  fingerprint      {fingerprint()}")
