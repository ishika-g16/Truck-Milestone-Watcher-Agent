"""SQLite connection + schema for the internal Watcher DB."""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "data/watcher_internal.db"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS watches (
    watch_id                TEXT PRIMARY KEY,
    load_id                 TEXT NOT NULL,
    load_num                TEXT,
    vh_num                  TEXT,

    watch_state             TEXT NOT NULL,
    progress_state          TEXT,
    commitment_state        TEXT,
    evidence_state          TEXT,

    created_at              TEXT NOT NULL,
    last_evaluated_at       TEXT
);

CREATE INDEX IF NOT EXISTS ix_watches_load_state
    ON watches (load_id, watch_state);

-- Invariant: at most one ACTIVE watch per load.
CREATE UNIQUE INDEX IF NOT EXISTS ux_watches_one_active_per_load
    ON watches (load_id)
    WHERE watch_state = 'ACTIVE';

CREATE TABLE IF NOT EXISTS incidents (
    incident_id         TEXT PRIMARY KEY,
    correlation_key     TEXT NOT NULL UNIQUE,
    watch_id            TEXT,
    load_id             TEXT NOT NULL,

    incident_type       TEXT NOT NULL,
    status              TEXT NOT NULL,

    opened_at           TEXT NOT NULL,
    resolved_at         TEXT,
    last_updated_at     TEXT NOT NULL,

    recommended_action  TEXT,
    times_seen          INTEGER NOT NULL DEFAULT 1,

    FOREIGN KEY (watch_id) REFERENCES watches (watch_id)
);

CREATE INDEX IF NOT EXISTS ix_incidents_load_status
    ON incidents (load_id, status);

CREATE INDEX IF NOT EXISTS ix_incidents_watch
    ON incidents (watch_id);

-- Five rows per load_id (WATCH_STATES). Catch-up = ORDER BY seq, first completed=0.
CREATE TABLE IF NOT EXISTS state_rows (
    load_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL CHECK (seq >= 0 AND seq <= 4),
    current_state   TEXT NOT NULL,
    next_state      TEXT,
    planned         TEXT,
    committed       TEXT,
    forecasted      TEXT,
    actual          TEXT,
    completed       INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (load_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_state_rows_load_state
    ON state_rows (load_id, current_state);

CREATE INDEX IF NOT EXISTS ix_state_rows_load_open
    ON state_rows (load_id, completed, seq);

CREATE TABLE IF NOT EXISTS pattern_store (
    pattern_key             TEXT PRIMARY KEY,
    sample_count            INTEGER NOT NULL DEFAULT 0,
    avg_delta_min           REAL,
    incident_rate           REAL,
    commitment_breach_rate  REAL,
    last_updated            TEXT,
    first_seen              TEXT
);

CREATE TABLE IF NOT EXISTS load_memory (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id                 TEXT NOT NULL,
    carrier_name            TEXT,
    shipper_name            TEXT,
    origin                  TEXT,
    destination             TEXT,
    closed_at               TEXT,
    commitment_met          INTEGER,
    total_delay_min         INTEGER,
    highest_severity        TEXT,
    state_actuals           TEXT,
    investigator_summary    TEXT
);

CREATE INDEX IF NOT EXISTS ix_load_memory_carrier
    ON load_memory (carrier_name, closed_at);

CREATE TABLE IF NOT EXISTS investigator_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id         TEXT NOT NULL,
    input_context   TEXT NOT NULL,
    output          TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_investigator_runs_load
    ON investigator_runs (load_id, timestamp);
"""


def db_path() -> Path:
    raw = os.environ.get("INTERNAL_DB_PATH", DEFAULT_DB_PATH)
    return Path(raw).expanduser().resolve()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open SQLite, create parent dirs + schema if needed."""
    target = Path(path) if path is not None else db_path()
    conn = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_schema(conn)
        return conn
    except (sqlite3.Error, OSError) as exc:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        raise RuntimeError(f"Could not open internal DB at {target}: {exc}") from exc


def init_schema(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "agent_decisions" in tables and "investigator_runs" not in tables:
        conn.execute("ALTER TABLE agent_decisions RENAME TO investigator_runs")
        try:
            conn.execute(
                "UPDATE sqlite_sequence SET name = 'investigator_runs' WHERE name = 'agent_decisions'"
            )
        except sqlite3.OperationalError:
            pass
    conn.executescript(SCHEMA_SQL)
    conn.execute("DROP INDEX IF EXISTS ix_agent_decisions_load")
    # Drop leftover AT_DROPOFF rows from the old 6-row ledger.
    # CREATE TABLE IF NOT EXISTS does not rewrite CHECK on existing files.
    conn.execute(
        "DELETE FROM state_rows WHERE seq > 4 OR current_state = 'AT_DROPOFF'"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(watches)").fetchall()}
    if "next_check_at" in cols:
        try:
            conn.execute("ALTER TABLE watches DROP COLUMN next_check_at")
        except sqlite3.OperationalError:
            pass
    incident_cols = {row[1] for row in conn.execute("PRAGMA table_info(incidents)").fetchall()}
    if "severity" not in incident_cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN severity TEXT")
    if "narrative" not in incident_cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN narrative TEXT")
    if "suppressed" not in incident_cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0")
    if "times_seen" not in incident_cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN times_seen INTEGER NOT NULL DEFAULT 1")
    leftover = [
        ("vh_id", "ALTER TABLE watches DROP COLUMN vh_id", "PRAGMA table_info(watches)"),
        ("vh_id", "ALTER TABLE incidents DROP COLUMN vh_id", "PRAGMA table_info(incidents)"),
        ("llm_explanation", "ALTER TABLE incidents DROP COLUMN llm_explanation", "PRAGMA table_info(incidents)"),
        ("final_state", "ALTER TABLE load_memory DROP COLUMN final_state", "PRAGMA table_info(load_memory)"),
        ("incident_count", "ALTER TABLE load_memory DROP COLUMN incident_count", "PRAGMA table_info(load_memory)"),
        ("wake_id", "ALTER TABLE investigator_runs DROP COLUMN wake_id", "PRAGMA table_info(investigator_runs)"),
        ("model", "ALTER TABLE investigator_runs DROP COLUMN model", "PRAGMA table_info(investigator_runs)"),
        ("latency_ms", "ALTER TABLE investigator_runs DROP COLUMN latency_ms", "PRAGMA table_info(investigator_runs)"),
        ("decision_type", "ALTER TABLE investigator_runs DROP COLUMN decision_type", "PRAGMA table_info(investigator_runs)"),
        ("p75_delta_min", "ALTER TABLE pattern_store DROP COLUMN p75_delta_min", "PRAGMA table_info(pattern_store)"),
        ("p95_delta_min", "ALTER TABLE pattern_store DROP COLUMN p95_delta_min", "PRAGMA table_info(pattern_store)"),
        ("stddev_delta_min", "ALTER TABLE pattern_store DROP COLUMN stddev_delta_min", "PRAGMA table_info(pattern_store)"),
        ("avg_dwell_min", "ALTER TABLE pattern_store DROP COLUMN avg_dwell_min", "PRAGMA table_info(pattern_store)"),
        ("p95_dwell_min", "ALTER TABLE pattern_store DROP COLUMN p95_dwell_min", "PRAGMA table_info(pattern_store)"),
    ]
    run_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(investigator_runs)").fetchall()
    }
    if "decision_type" in run_cols:
        conn.execute("DROP INDEX IF EXISTS ix_investigator_runs_load")
    pragma_cache: dict[str, set[str]] = {}
    for col, drop, pragma in leftover:
        if pragma not in pragma_cache:
            pragma_cache[pragma] = {row[1] for row in conn.execute(pragma).fetchall()}
        if col in pragma_cache[pragma]:
            try:
                conn.execute(drop)
            except sqlite3.OperationalError:
                pass
            pragma_cache.pop(pragma, None)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_investigator_runs_load
        ON investigator_runs (load_id, timestamp)
        """
    )
    conn.commit()
