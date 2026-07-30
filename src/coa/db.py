"""SQLite schema and connection handling.

Storage is stdlib `sqlite3` throughout: ~20k merchants and ~2 GB of zipped logs
land well inside what SQLite handles comfortably, and it keeps the whole analysis
layer as SQL over two ingested tables rather than a dataframe stack.

Two conventions hold across every table:

* `se10` is stored as TEXT everywhere. Inputs arrive as both int and str; they are
  normalized on the way in so joins never silently miss.
* Every row carries `src_file` + `src_line` provenance, so any figure in a report
  can be traced back to the line that produced it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- Resumable ingest bookkeeping. A source file is recorded only after its
-- transaction commits, so a crash mid-file leaves no partial rows and the
-- re-run redoes exactly that file.
CREATE TABLE IF NOT EXISTS ingested_files (
    src_file    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- log | output | input
    n_lines     INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

-- Central anomaly store. This is the only channel through which reality reaches
-- the parser, so it keeps enough context to diagnose a format surprise blind.
CREATE TABLE IF NOT EXISTS anomalies (
    id          INTEGER PRIMARY KEY,
    stage       TEXT NOT NULL,          -- logs | outputs | inputs | analyze
    code        TEXT NOT NULL,
    se10        TEXT,
    src_file    TEXT,
    src_line    INTEGER,
    raw_excerpt TEXT,                   -- truncated to config.anomalies.max_excerpt_chars
    context     TEXT,                   -- +/- N surrounding raw lines, newline-joined
    detail      TEXT,
    ts_recorded TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_anomalies_code ON anomalies(code);

CREATE TABLE IF NOT EXISTS merchants (
    se10            TEXT PRIMARY KEY,
    opening_date    TEXT,
    city            TEXT,
    industry_tagged TEXT,
    sub_category    TEXT,
    email           TEXT,
    phone           TEXT,
    street          TEXT,
    signer_name     TEXT,
    owner_name      TEXT,
    owner_city      TEXT,
    owner_postal    TEXT,
    owner_street    TEXT,
    website         TEXT,
    country         TEXT,
    state           TEXT,
    raw_json        TEXT NOT NULL,      -- full record; schema has "many more" keys
    src_file        TEXT,
    src_line        INTEGER
);

-- Exploded, normalized merchant values used to template PII out of queries.
CREATE TABLE IF NOT EXISTS pii_terms (
    se10       TEXT NOT NULL,
    field      TEXT NOT NULL,           -- name | street | city | zip | phone | email | owner
    value_norm TEXT NOT NULL,
    PRIMARY KEY (se10, field, value_norm)
);
CREATE INDEX IF NOT EXISTS ix_pii_se10 ON pii_terms(se10);

-- One row per (merchant, run). `usage_metadata` gives measured token volume, so
-- the cost model is metered rather than inferred from call counts.
--
-- The token fields are SUBSETS, not addends -- verified against the corpus
-- (input + output == total holds exactly) and against OpenAI's docs:
--   * `reasoning` is inside `output_tokens` and bills at the output rate;
--   * `cache_read` is inside `input_tokens` and bills at the cached-input rate.
-- Adding either as a separate line item double-counts. See CLAUDE.md
-- "Cost model" for the formula this implies.
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    se10           TEXT NOT NULL,
    run_id         INTEGER,             -- exact, from the `run_<n>` key
    run_key        TEXT NOT NULL,       -- verbatim key, so an odd one is still traceable
    service_tier   TEXT,                -- standard | flex | priority: ~4x rate spread
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    total_tokens   INTEGER,
    cache_read     INTEGER,             -- subset of input_tokens
    reasoning      INTEGER,             -- subset of output_tokens
    src_file       TEXT NOT NULL,
    src_line       INTEGER NOT NULL,
    UNIQUE (se10, run_key)
);
CREATE INDEX IF NOT EXISTS ix_runs_se10 ON runs(se10);
CREATE INDEX IF NOT EXISTS ix_runs_tier ON runs(service_tier);

-- One row per entry in a run's `web_search_calls` array. Merchant and run are
-- structural keys in the source, so attribution is an exact join -- there is no
-- pairing, no orphans, and no heuristic run assignment.
CREATE TABLE IF NOT EXISTS search_calls (
    id           INTEGER PRIMARY KEY,
    se10         TEXT NOT NULL,
    run_pk       INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    run_id       INTEGER,
    call_index   INTEGER NOT NULL,      -- position in the array; this is what
                                        -- position-in-run analysis uses, not a clock
    call_id      TEXT,                  -- the API's `id`
    action_type  TEXT NOT NULL,         -- search | open_page | find_in_page, verbatim
    status       TEXT,                  -- `completed` is the norm; anything else is a finding
    query_raw    TEXT,                  -- singular `query` -- the ONLY billed unit
    queries_json TEXT,                  -- plural `queries`: sub-queries, never cost
    url          TEXT,                  -- open_page
    details      TEXT,                  -- find_in_page
    raw_json     TEXT NOT NULL,         -- retained pristine: makes `coa reparse` possible
    parse_conf   TEXT NOT NULL,         -- clean | heuristic | failed
    src_file     TEXT NOT NULL,
    src_line     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_search_calls_se10 ON search_calls(se10);
CREATE INDEX IF NOT EXISTS ix_search_calls_action ON search_calls(action_type);
CREATE INDEX IF NOT EXISTS ix_search_calls_run ON search_calls(run_pk);

-- One row per (search_call, query). A call carries a singular `query` plus a
-- plural `queries` list; `is_billed_query` marks the singular one. Cost
-- attribution uses ONLY the billed query, so archetype shares sum to 100%.
CREATE TABLE IF NOT EXISTS query_instances (
    id              INTEGER PRIMARY KEY,
    search_call_id  INTEGER NOT NULL REFERENCES search_calls(id) ON DELETE CASCADE,
    se10            TEXT,
    query_text      TEXT NOT NULL,
    template        TEXT,               -- PII replaced with <FIELD> placeholders
    n_placeholders  INTEGER,            -- 0 => template is verbatim, may hold unmasked PII
    archetype       TEXT,               -- from archetype_groups.csv, NULL if unmapped
    is_billed_query INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_qi_call ON query_instances(search_call_id);
CREATE INDEX IF NOT EXISTS ix_qi_template ON query_instances(template);

CREATE TABLE IF NOT EXISTS output_records (
    id                     INTEGER PRIMARY KEY,
    se10                   TEXT NOT NULL,
    n_runs                 INTEGER,
    question_system_prompt TEXT,
    question_user_prompt   TEXT,
    raw_json_hash          TEXT,
    dup_flag               INTEGER NOT NULL DEFAULT 0,
    src_file               TEXT NOT NULL,
    src_line               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_output_records_se10 ON output_records(se10);

-- Canonical 48-question set, established from the first record seen. Every later
-- record is compared against it; divergence raises QUESTION_SET_DRIFT.
CREATE TABLE IF NOT EXISTS questions (
    qnum INTEGER PRIMARY KEY,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    id              INTEGER PRIMARY KEY,
    se10            TEXT NOT NULL,
    output_id       INTEGER NOT NULL REFERENCES output_records(id) ON DELETE CASCADE,
    run_id          INTEGER,
    qnum            INTEGER,
    answer_text     TEXT,
    evidence_text   TEXT,
    is_null         INTEGER NOT NULL DEFAULT 0,
    parsed_from     TEXT,               -- answers_text | answer_dict
    agree_with_dict INTEGER             -- NULL when there is nothing to compare against
);
CREATE INDEX IF NOT EXISTS ix_answers_q ON answers(qnum);
CREATE INDEX IF NOT EXISTS ix_answers_se10 ON answers(se10);
-- Indexing the FK column is not optional here. SQLite has no automatic index on
-- the child side of a foreign key, so ON DELETE CASCADE scans the whole child
-- table once per deleted parent row -- quadratic. Measured at 2k parents /
-- 100k children: 2703 ms unindexed vs 34 ms indexed, and it worsens with scale,
-- which would make `coa ingest --force` unusable on a real corpus.
CREATE INDEX IF NOT EXISTS ix_answers_output ON answers(output_id);

CREATE TABLE IF NOT EXISTS citations (
    id                INTEGER PRIMARY KEY,
    se10              TEXT NOT NULL,
    output_id         INTEGER NOT NULL REFERENCES output_records(id) ON DELETE CASCADE,
    run_id            INTEGER,
    qnum              INTEGER,
    url               TEXT,
    domain            TEXT,
    title             TEXT,
    empty_placeholder INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL,    -- citation_evidence | markdown_prose
    char_pos          INTEGER
);
CREATE INDEX IF NOT EXISTS ix_citations_domain ON citations(domain);
CREATE INDEX IF NOT EXISTS ix_citations_q ON citations(qnum);
CREATE INDEX IF NOT EXISTS ix_citations_output ON citations(output_id);

CREATE TABLE IF NOT EXISTS votes (
    id              INTEGER PRIMARY KEY,
    se10            TEXT NOT NULL,
    output_id       INTEGER NOT NULL REFERENCES output_records(id) ON DELETE CASCADE,
    qnum            INTEGER,
    voted_majority  TEXT,
    voted_final     TEXT,
    differs         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_votes_output ON votes(output_id);
CREATE INDEX IF NOT EXISTS ix_votes_q ON votes(qnum);

-- Reserved for ground-truth labels. Empty until they exist; declared now so the
-- analysis upgrade needs no migration.
CREATE TABLE IF NOT EXISTS labels (
    se10   TEXT NOT NULL,
    label  TEXT NOT NULL,
    source TEXT,
    ts     TEXT
);

-- Archetypes are a rollup, not stored state, until the hand-grouping outgrows it.
CREATE VIEW IF NOT EXISTS archetypes AS
SELECT
    COALESCE(archetype, template)     AS archetype_id,
    MIN(query_text)                   AS exemplar,
    COUNT(*)                          AS n_queries,
    COUNT(DISTINCT se10)              AS n_merchants,
    SUM(is_billed_query)              AS n_billed_calls,
    MAX(archetype IS NULL)            AS is_unmapped
FROM query_instances
WHERE template IS NOT NULL
GROUP BY COALESCE(archetype, template);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the analysis DB with the schema applied.

    WAL plus a relaxed synchronous mode: ingest is a bulk append of a few million
    rows, and a crash is recovered by re-running the affected source file anyway,
    so durability per-transaction is not worth the write amplification.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def already_ingested(conn: sqlite3.Connection, src_file: str) -> bool:
    """Whether this source file completed a previous ingest run."""
    row = conn.execute("SELECT 1 FROM ingested_files WHERE src_file = ?", (src_file,)).fetchone()
    return row is not None


def mark_ingested(conn: sqlite3.Connection, src_file: str, kind: str, n_lines: int) -> None:
    """Record a source file as done. Called inside that file's transaction."""
    conn.execute(
        "INSERT OR REPLACE INTO ingested_files (src_file, kind, n_lines, ingested_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (src_file, kind, n_lines),
    )


def forget_file(conn: sqlite3.Connection, src_file: str) -> None:
    """Drop every row sourced from one file, so `--force` can cleanly redo it.

    Children (query_instances, answers, citations, votes) have no src_file of their
    own and go via ON DELETE CASCADE from search_calls / output_records — which is
    why `PRAGMA foreign_keys=ON` in connect() is load-bearing, not decoration.
    """
    for table in ("search_calls", "runs", "output_records", "merchants", "anomalies"):
        conn.execute(f"DELETE FROM {table} WHERE src_file = ?", (src_file,))
    conn.execute("DELETE FROM ingested_files WHERE src_file = ?", (src_file,))
