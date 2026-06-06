"""coordination/schema.py -- SQL DDL for the ACA coordination store.

Provides the SQLite and Postgres schema strings and the ``ensure_schema``
implementation as a standalone function so it can be tested independently
of the full ``CoordinationStore`` class.

The tables are:
- tasks        -- one row per unique task (keyed by source type + ref)
- runs         -- one row per run execution
- workers      -- one row per registered worker process
- leases       -- one row per coordination lease
- outbox       -- async outbox queue for GitHub / remote sync events
- scheduler_events -- audit log for reaper / scheduler ticks
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# SQLite DDL
# ---------------------------------------------------------------------------
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_key TEXT PRIMARY KEY,
    task_id TEXT,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    repo_slug TEXT,
    repo_path TEXT,
    board_path TEXT,
    status TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    claimed_run_id TEXT,
    claimed_lease_id TEXT,
    claimed_by TEXT,
    claimed_host_id TEXT,
    lease_expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_key TEXT,
    task_id TEXT,
    repo_slug TEXT,
    repo_path TEXT,
    branch_name TEXT,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    worker_id TEXT,
    host_id TEXT,
    lease_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    started_at_ms INTEGER,
    completed_at_ms INTEGER,
    error TEXT,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    current_run_id TEXT,
    current_lease_id TEXT,
    last_seen_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    acquired_at_ms INTEGER NOT NULL,
    heartbeat_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    released_at_ms INTEGER,
    release_reason TEXT,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ms INTEGER NOT NULL,
    last_error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS external_action_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT,
    source_type TEXT NOT NULL,
    adapter TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    verification_marker TEXT,
    status TEXT NOT NULL,
    requested_by TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    result_json TEXT NOT NULL,
    error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    decided_at_ms INTEGER,
    executed_at_ms INTEGER,
    expires_at_ms INTEGER,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS scheduler_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leases_status_expires
    ON leases(status, expires_at_ms);

CREATE INDEX IF NOT EXISTS idx_runs_status
    ON runs(status, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_tasks_state_status
    ON tasks(state, status, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_external_action_approvals_status
    ON external_action_approvals(status, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_external_action_approvals_run
    ON external_action_approvals(run_id, status);
"""

SQLITE_MIGRATIONS = [
    # Migration 1: add state column if missing (handled in ensure_schema_sqlite)
]


# ---------------------------------------------------------------------------
# Postgres DDL
# ---------------------------------------------------------------------------
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_key TEXT PRIMARY KEY,
    task_id TEXT,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    repo_slug TEXT,
    repo_path TEXT,
    board_path TEXT,
    status TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    claimed_run_id TEXT,
    claimed_lease_id TEXT,
    claimed_by TEXT,
    claimed_host_id TEXT,
    lease_expires_at_ms BIGINT,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_key TEXT,
    task_id TEXT,
    repo_slug TEXT,
    repo_path TEXT,
    branch_name TEXT,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    worker_id TEXT,
    host_id TEXT,
    lease_id TEXT,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    started_at_ms BIGINT,
    completed_at_ms BIGINT,
    error TEXT,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    current_run_id TEXT,
    current_lease_id TEXT,
    last_seen_at_ms BIGINT NOT NULL,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    acquired_at_ms BIGINT NOT NULL,
    heartbeat_at_ms BIGINT NOT NULL,
    expires_at_ms BIGINT NOT NULL,
    released_at_ms BIGINT,
    release_reason TEXT,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ms BIGINT NOT NULL,
    last_error TEXT,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS external_action_approvals (
    id BIGSERIAL PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT,
    source_type TEXT NOT NULL,
    adapter TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    verification_marker TEXT,
    status TEXT NOT NULL,
    requested_by TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    result_json TEXT NOT NULL,
    error TEXT,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    decided_at_ms BIGINT,
    executed_at_ms BIGINT,
    expires_at_ms BIGINT,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS scheduler_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leases_status_expires
    ON leases(status, expires_at_ms);

CREATE INDEX IF NOT EXISTS idx_runs_status
    ON runs(status, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_tasks_state_status
    ON tasks(state, status, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_external_action_approvals_status
    ON external_action_approvals(status, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_external_action_approvals_run
    ON external_action_approvals(run_id, status);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'queued';
"""


# ---------------------------------------------------------------------------
# Schema application helpers
# ---------------------------------------------------------------------------

def apply_sqlite_schema(conn: "sqlite3.Connection") -> None:  # noqa: F821
    """Apply the SQLite schema and any pending migrations to an open connection."""
    import sqlite3 as _sqlite3

    conn.executescript(SQLITE_SCHEMA)
    # Migration: add state column if it doesn't exist (pre-state-machine rows)
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "state" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN state TEXT NOT NULL DEFAULT 'queued'")
        conn.execute(
            "UPDATE tasks SET state = status WHERE state = 'queued' AND status IS NOT NULL"
        )


def apply_postgres_schema(conn: "Any") -> None:  # noqa: F821
    """Apply the Postgres schema to an open connection adapter."""
    conn.executescript(POSTGRES_SCHEMA)
