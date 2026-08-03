"""P2: output record parsing, answer blocks, citations, votes (PLAN.md §4).

The unit layer is where the real work is. Several branches here exist for shapes
the corpus does not contain — an omitted Evidence line, an answer only their dict
knows about, an unparseable run key — because the real format is only partly
confirmed and those branches are what keep a wrong guess from losing data
silently. A six-line in-memory record is the only cheap way to reach them.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coa.anomalies import AnomalyRecorder
from coa.config import Config
from coa.db import connect
from coa.outputs import (
    evidence_shape,
    extract_questions,
    ingest_output,
    is_null_answer,
    normalize_vote,
    parse_answer_blocks,
)

CFG = Config.load(Path(__file__).parent.parent / "config.yaml")

SYSTEM = "You are a merchant risk analyst."


def prompt(*questions: str) -> str:
    return "Answer each question.\n" + "\n".join(f"Q{i}. {q}" for i, q in enumerate(questions, 1))


def record(**over) -> dict:
    """A minimal two-question output record; override any key."""
    base = {
        "se10": "1000000001",
        "n_runs": 1,
        "question": [[SYSTEM, prompt("Is it legitimate?", "What is the address?")]],
        "answers": {
            "run_0": "Q1. t\nA1. 2\nEvidence. seen ([src](https://a.example/1))\n\nQ2. t\nA2. NULL"
        },
        "answer_dict": {"run_0": {"A1": "2", "A2": "NULL"}, "citation_evidence": {"run_0": []}},
        "voted_majority": {"A1": "Yes", "A2": "No"},
        "voted_final": {"A1": "Yes", "A2": "No"},
    }
    return {**base, **over}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def run(conn: sqlite3.Connection, records: list) -> tuple[dict, AnomalyRecorder]:
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
    rec = AnomalyRecorder(conn, "outputs")
    stats = ingest_output(conn, rec, "output/t.jsonl", lines, CFG)
    rec.flush()
    return stats, rec


def rows(conn: sqlite3.Connection, table: str, where: str = "") -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table} {where} ORDER BY id").fetchall()


# --- question extraction (pure) -------------------------------------------


def test_multiline_questions_are_extracted_whole() -> None:
    """Real questions carry their answer-format instruction on following lines."""
    text = prompt("Is it legitimate?\nAnswer 1-5 (default: 3)", "Address?\nReturn: value | NULL")
    got = extract_questions(text)
    assert len(got) == 2
    assert "Answer 1-5" in got[1]
    assert got[2].endswith("value | NULL")


# --- answer block parsing (pure) ------------------------------------------


def test_evidence_present_binds_to_its_own_group() -> None:
    (block,) = parse_answer_blocks("Q1. t\nA1. 2\nEvidence. found it")
    assert (block.qnum, block.answer, block.evidence) == (1, "2", "found it")


def test_evidence_line_omitted_still_yields_the_answer() -> None:
    """The dangerous shape: a required-Evidence regex loses the whole block."""
    blocks = parse_answer_blocks("Q1. t\nA1. 4\n\nQ2. t\nA2. 5\nEvidence. NULL")
    assert [(b.qnum, b.answer, b.evidence) for b in blocks] == [(1, "4", None), (2, "5", "NULL")]


def test_bare_evidence_label_is_distinct_from_an_absent_one() -> None:
    """Empty vs absent are different facts about the exporter; keep them apart."""
    (block,) = parse_answer_blocks("Q1. t\nA1. 4\nEvidence.")
    assert block.evidence == ""
    assert evidence_shape(block.evidence) == "evidence_empty"
    assert evidence_shape(None) == "evidence_absent"


def test_free_text_answer_with_commas_survives() -> None:
    (block,) = parse_answer_blocks("Q1. t\nA1. 1400 Main St, Springfield, IL\nEvidence. deed")
    assert block.answer == "1400 Main St, Springfield, IL"


def test_multiline_evidence_is_kept_whole() -> None:
    blocks = parse_answer_blocks("Q1. t\nA1. 3\nEvidence. one\ntwo\n\nQ2. t\nA2. NULL")
    assert blocks[0].evidence == "one\ntwo"


def test_scale_and_null_answers_classify() -> None:
    """is_null keys on the ANSWER; evidence NULL is a specified outcome, not a gap."""
    assert is_null_answer("NULL") and is_null_answer(" null ")
    assert not is_null_answer("3")
    assert evidence_shape("NULL") == "evidence_null"


# --- vote normalization (pure) --------------------------------------------


def test_identical_list_collapses_to_scalar() -> None:
    assert normalize_vote(["NULL", "NULL"]) == ("NULL", False)


def test_mixed_list_stays_json_and_is_flagged() -> None:
    text, odd = normalize_vote(["Yes", "No"])
    assert odd and json.loads(text) == ["Yes", "No"]


def test_scalar_and_empty_votes() -> None:
    assert normalize_vote("Yes") == ("Yes", False)
    assert normalize_vote(None) == (None, False)
    assert normalize_vote([]) == (None, False)


# --- record ingest ---------------------------------------------------------


def test_answers_and_evidence_land_with_run_id(conn: sqlite3.Connection) -> None:
    run(conn, [record()])
    answers = rows(conn, "answers")
    assert [(a["qnum"], a["answer_text"], a["run_id"]) for a in answers] == [
        (1, "2", 0),
        (2, "NULL", 0),
    ]
    assert answers[1]["is_null"] == 1
    assert answers[0]["parsed_from"] == "answers_text"


def test_answer_only_in_answer_dict_is_still_stored(conn: sqlite3.Connection) -> None:
    """The backstop: if the block regex is wrong about the real format, answers
    still land, tagged with where they came from, instead of vanishing."""
    rec = record(answer_dict={"run_0": {"A1": "2", "A2": "NULL", "A3": "5"}})
    run(conn, [rec])
    recovered = [a for a in rows(conn, "answers") if a["parsed_from"] == "answer_dict"]
    assert [(a["qnum"], a["answer_text"]) for a in recovered] == [(3, "5")]


def test_agree_with_dict_is_null_when_there_is_nothing_to_compare(
    conn: sqlite3.Connection,
) -> None:
    run(conn, [record(answer_dict={"run_0": {}})])
    assert {a["agree_with_dict"] for a in rows(conn, "answers")} == {None}


def test_disagreement_is_one_anomaly_per_run_not_per_answer(
    conn: sqlite3.Connection,
) -> None:
    """A systematic normalization difference must not bury every other code."""
    rec = record(answer_dict={"run_0": {"A1": "5", "A2": "something else"}})
    stats, recorder = run(conn, [rec])
    assert stats["out_answer_disagreements"] == 2
    assert recorder.counts["ANSWER_PARSE_MISMATCH"] == 1
    assert [a["agree_with_dict"] for a in rows(conn, "answers")] == [0, 0]


def test_short_run_is_flagged_with_the_missing_qnums(conn: sqlite3.Connection) -> None:
    rec = record(answers={"run_0": "Q1. t\nA1. 2\nEvidence. x"})
    _, recorder = run(conn, [rec])
    assert recorder.counts["ANSWER_BLOCK_COUNT"] == 1
    detail = conn.execute(
        "SELECT detail FROM anomalies WHERE code = 'ANSWER_BLOCK_COUNT'"
    ).fetchone()["detail"]
    assert "Q2" in detail


def test_unparseable_run_key_is_flagged_not_silently_nulled(
    conn: sqlite3.Connection,
) -> None:
    """A NULL run_id disables every per-run metric; that must never be quiet."""
    rec = record(answers={"batch7": "Q1. t\nA1. 2\nEvidence. x\n\nQ2. t\nA2. NULL"})
    _, recorder = run(conn, [rec])
    assert recorder.counts["RUN_KEY_UNPARSED"] == 1
    assert {a["run_id"] for a in rows(conn, "answers")} == {None}


# --- citations -------------------------------------------------------------


def test_prose_citation_records_domain_and_position(conn: sqlite3.Connection) -> None:
    run(conn, [record()])
    (cite,) = rows(conn, "citations", "WHERE source = 'markdown_prose'")
    assert cite["domain"] == "a.example"
    assert cite["title"] == "src"
    assert cite["char_pos"] > 0


def test_empty_placeholder_is_marked_not_dropped(conn: sqlite3.Connection) -> None:
    rec = record(answers={"run_0": "Q1. t\nA1. 2\nEvidence. found ([]())\n\nQ2. t\nA2. NULL"})
    run(conn, [rec])
    (cite,) = rows(conn, "citations", "WHERE source = 'markdown_prose'")
    assert cite["empty_placeholder"] == 1 and cite["url"] is None


def test_list_shaped_citation_is_flagged_and_every_url_kept(
    conn: sqlite3.Connection,
) -> None:
    rec = record(
        answer_dict={
            "run_0": {"A1": "2", "A2": "NULL"},
            "citation_evidence": {
                "run_0": [
                    {
                        "a_key": "A1",
                        "citation": ["https://a.example/1", "https://b.example/2"],
                    }
                ]
            },
        }
    )
    _, recorder = run(conn, [rec])
    assert recorder.counts["CITATION_SHAPE_UNEXPECTED"] == 1
    urls = {c["url"] for c in rows(conn, "citations", "WHERE source = 'citation_evidence'")}
    assert urls == {"https://a.example/1", "https://b.example/2"}


def test_unknown_shape_is_excluded_from_the_cross_check(conn: sqlite3.Connection) -> None:
    """We do not know its true URL set, so a difference is unknown, not loss."""
    rec = record(
        answer_dict={
            "run_0": {"A1": "2", "A2": "NULL"},
            "citation_evidence": {"run_0": [{"a_key": "A1", "citation": {"url": "x"}}]},
        }
    )
    _, recorder = run(conn, [rec])
    assert recorder.counts["CITATION_SHAPE_UNEXPECTED"] == 1
    assert "CITATION_SOURCE_MISMATCH" not in recorder.counts


def test_empty_prose_placeholder_and_null_dict_citation_agree(
    conn: sqlite3.Connection,
) -> None:
    """Same fact recorded twice. Comparing them raw would fire on every record."""
    rec = record(
        answers={"run_0": "Q1. t\nA1. 2\nEvidence. found ([]())\n\nQ2. t\nA2. NULL"},
        answer_dict={
            "run_0": {"A1": "2", "A2": "NULL"},
            "citation_evidence": {"run_0": [{"a_key": "A1", "citation": None}]},
        },
    )
    _, recorder = run(conn, [rec])
    assert "CITATION_SOURCE_MISMATCH" not in recorder.counts


def test_citation_dropped_from_dict_but_present_in_prose_is_caught(
    conn: sqlite3.Connection,
) -> None:
    _, recorder = run(conn, [record()])  # prose has a URL, citation_evidence is empty
    assert recorder.counts["CITATION_SOURCE_MISMATCH"] == 1


# --- votes -----------------------------------------------------------------


def test_majority_and_final_divergence_is_flagged(conn: sqlite3.Connection) -> None:
    run(conn, [record(voted_final={"A1": "Yes", "A2": "Yes"})])
    differs = {v["qnum"]: v["differs"] for v in rows(conn, "votes")}
    assert differs == {1: 0, 2: 1}


def test_empty_voted_final_is_treated_as_absent_and_counted(
    conn: sqlite3.Connection,
) -> None:
    stats, recorder = run(conn, [record(voted_final={})])
    assert stats["out_empty_voted_final"] == 1
    assert {v["voted_final"] for v in rows(conn, "votes")} == {None}
    assert {v["differs"] for v in rows(conn, "votes")} == {0}
    assert "VOTE_VALUE_LIST" not in recorder.counts


def test_mixed_vote_list_fires_once_per_qnum_not_once_per_field(
    conn: sqlite3.Connection,
) -> None:
    """The same unresolved value normally appears in BOTH majority and final."""
    rec = record(
        voted_majority={"A1": ["Yes", "No"], "A2": ["NULL", "NULL"]},
        voted_final={"A1": ["Yes", "No"], "A2": ["NULL", "NULL"]},
    )
    _, recorder = run(conn, [rec])
    assert recorder.counts["VOTE_VALUE_LIST"] == 1
    votes = {v["qnum"]: v["voted_majority"] for v in rows(conn, "votes")}
    assert votes[2] == "NULL"  # identical list collapsed
    assert json.loads(votes[1]) == ["Yes", "No"]


# --- record-level bookkeeping ---------------------------------------------


def test_bad_json_is_recorded_with_a_capped_excerpt(conn: sqlite3.Connection) -> None:
    stats, recorder = run(conn, ['{"se10": "1", "answers": {trunc', record()])
    assert recorder.counts["BAD_JSON_LINE"] == 1
    assert stats["out_records"] == 1
    excerpt = conn.execute(
        "SELECT raw_excerpt FROM anomalies WHERE code = 'BAD_JSON_LINE'"
    ).fetchone()["raw_excerpt"]
    assert len(excerpt) <= 500


def test_duplicate_se10_keeps_both_and_flags_both(conn: sqlite3.Connection) -> None:
    """§4 forbids dropping either; analysis picks the one with the most runs."""
    _, recorder = run(conn, [record(n_runs=1), record(n_runs=4)])
    assert recorder.counts["DUP_OUTPUT_SE10"] == 1
    assert [r["dup_flag"] for r in rows(conn, "output_records")] == [1, 1]


def test_question_set_drift_is_detected_against_the_canonical_set(
    conn: sqlite3.Connection,
) -> None:
    drifted = record(
        se10="1000000002",
        question=[[SYSTEM, prompt("Is it legitimate?", "Has it changed its legal name?")]],
    )
    _, recorder = run(conn, [record(), drifted])
    assert recorder.counts["QUESTION_SET_DRIFT"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"] == 2


def test_convenience_key_conflict_reports_and_never_overwrites_input(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO merchants (se10, website, raw_json) VALUES ('1000000001', ?, '{}')",
        ("https://real.example",),
    )
    _, recorder = run(conn, [record(website="https://conflicting.example")])
    assert recorder.counts["INPUT_OUTPUT_FIELD_CONFLICT"] == 1
    stored = conn.execute("SELECT website FROM merchants").fetchone()["website"]
    assert stored == "https://real.example"


def test_record_without_se10_is_flagged(conn: sqlite3.Connection) -> None:
    _, recorder = run(conn, [{"answers": {"run_0": "Q1. t\nA1. 2"}}])
    assert recorder.counts["OUTPUT_NO_SE10"] == 1


@pytest.mark.parametrize("question", [[[]], {"a": 1}, ["sys", "usr"], None, "text"])
def test_malformed_question_field_never_crashes(conn: sqlite3.Connection, question: object) -> None:
    """Indexing `question` blind killed the whole file: a dict raised KeyError and
    an empty inner list raised IndexError, taking every later record with them."""
    stats, _ = run(conn, [record(question=question)])
    assert stats["out_records"] == 1


# --- golden corpus ---------------------------------------------------------


def test_golden_output_record_and_run_counts(corpus, golden: dict) -> None:
    expected = golden["outputs"]
    conn = corpus.conn
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM output_records").fetchone()["n"]
        == (expected["n_records"])
    )
    assert corpus.totals["out_runs"] == expected["n_runs"]
    assert corpus.totals["out_answer_blocks"] == expected["answer_blocks"]


def test_golden_answer_and_vote_counts(corpus, golden: dict) -> None:
    conn = corpus.conn
    expected = golden["outputs"]
    n_answers = conn.execute("SELECT COUNT(*) AS n FROM answers").fetchone()["n"]
    assert n_answers == expected["answer_blocks"]
    n_null = conn.execute("SELECT COUNT(*) AS n FROM answers WHERE is_null").fetchone()["n"]
    assert n_null == expected["null_answers"]
    n_votes = conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
    assert n_votes == expected["n_records"] * golden["n_questions"]


def test_golden_evidence_shape_distribution(corpus, golden: dict) -> None:
    """All three no-evidence renderings are planted, so all three branches ran."""
    expected = golden["outputs"]
    for shape in ("evidence_present", "evidence_null", "evidence_empty", "evidence_absent"):
        assert corpus.totals[shape] == expected[shape], shape


def test_golden_citation_counts(corpus, golden: dict) -> None:
    expected = golden["outputs"]
    assert corpus.totals["out_prose_citations"] == expected["prose_citations"]
    assert corpus.totals["out_empty_placeholders"] == expected["empty_placeholders"]
    assert corpus.totals["out_citations_outside_blocks"] == 0


def test_citation_mismatch_is_confined_to_the_planted_record(corpus, golden: dict) -> None:
    """Golden counts records; the anomaly fires once per (run, qnum) inside one."""
    n = corpus.conn.execute(
        "SELECT COUNT(DISTINCT se10) AS n FROM anomalies WHERE code = 'CITATION_SOURCE_MISMATCH'"
    ).fetchone()["n"]
    assert n == golden["outputs"]["citation_source_mismatch_records"]


def test_every_planted_output_hazard_fired(corpus, golden: dict) -> None:
    codes = {
        r["code"]
        for r in corpus.conn.execute("SELECT DISTINCT code FROM anomalies WHERE stage = 'outputs'")
    }
    assert {
        "BAD_JSON_LINE",
        "DUP_OUTPUT_SE10",
        "QUESTION_SET_DRIFT",
        "ANSWER_BLOCK_COUNT",
        "CITATION_SOURCE_MISMATCH",
        "VOTE_VALUE_LIST",
        "ANSWER_PARSE_MISMATCH",
        "CITATION_SHAPE_UNEXPECTED",
        "INPUT_OUTPUT_FIELD_CONFLICT",
    } <= codes


def test_no_answer_row_is_left_without_provenance(corpus) -> None:
    orphaned = corpus.conn.execute(
        "SELECT COUNT(*) AS n FROM answers a "
        "WHERE NOT EXISTS (SELECT 1 FROM output_records o WHERE o.id = a.output_id)"
    ).fetchone()["n"]
    assert orphaned == 0


@pytest.mark.parametrize("answers", [None, {}, [], "text", {"run_0": "Q1. t\nA1. 2"}])
def test_record_without_usable_answers_is_flagged(
    conn: sqlite3.Connection, answers: object
) -> None:
    """2000 records yielding 0 runs must not look like a normal ingest.

    Without this the only signal was an unprinted counter, so a renamed or
    restructured `answers` key would produce an empty analysis that reported
    itself as success.
    """
    _, recorder = run(conn, [record(answers=answers)])
    expected = 0 if isinstance(answers, dict) and answers else 1
    assert recorder.counts.get("OUTPUT_NO_ANSWERS", 0) == expected


def test_no_answers_anomaly_names_the_keys_that_were_present(
    conn: sqlite3.Connection,
) -> None:
    """The operator needs to see what the record DOES have, or diagnosing the
    rename costs another round-trip across the air gap."""
    run(conn, [record(answers=None)])
    detail = conn.execute(
        "SELECT detail FROM anomalies WHERE code = 'OUTPUT_NO_ANSWERS'"
    ).fetchone()["detail"]
    assert "voted_majority" in detail and "answer_dict" in detail
