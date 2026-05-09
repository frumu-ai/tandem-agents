"""coordination/rows.py -- Row-to-dict serialization helpers.

These standalone functions convert raw database Row objects (sqlite3.Row or
Postgres dict_row) to clean Python dicts. Extracted from CoordinationStore
so they can be unit-tested without a live database connection.

All functions accept ``None`` and return an empty dict, making them safe to
call directly on ``.fetchone()`` results.
"""
from __future__ import annotations

import json
from typing import Any

from src.aca.core.task_contract import apply_task_contract


# ---------------------------------------------------------------------------
# JSON helpers (copied from coordination.py to keep this module self-contained)
# ---------------------------------------------------------------------------

def _json_loads(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Row → dict converters
# ---------------------------------------------------------------------------

def row_to_task(row: Any | None) -> dict[str, Any]:
    """Convert a ``tasks`` table row to a task dict."""
    if row is None:
        return {}
    metadata = _json_loads(row["metadata_json"])
    keys = row.keys() if hasattr(row, "keys") else list(row)
    task = dict(metadata.get("task") or {}) if isinstance(metadata, dict) else {}
    if not isinstance(task, dict):
        task = {}
    task.update(
        {
        "task_key": row["task_key"],
        "task_id": row["task_id"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "title": row["title"],
        "repo_slug": row["repo_slug"],
        "repo_path": row["repo_path"],
        "board_path": row["board_path"],
        "status": row["status"],
        "state": row["state"] if "state" in keys else row["status"],
        "claimed_run_id": row["claimed_run_id"],
        "claimed_lease_id": row["claimed_lease_id"],
        "claimed_by": row["claimed_by"],
        "claimed_host_id": row["claimed_host_id"],
        "lease_expires_at_ms": row["lease_expires_at_ms"],
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
        "metadata": metadata,
        }
    )
    if not isinstance(task.get("source"), dict):
        task["source"] = {}
    if not isinstance(task.get("repo"), dict):
        task["repo"] = {}
    task.setdefault("task_contract", dict(task.get("task_contract") or {}))
    return apply_task_contract(task)


def row_to_run(row: Any | None) -> dict[str, Any]:
    """Convert a ``runs`` table row to a run dict."""
    if row is None:
        return {}
    metadata = _json_loads(row["metadata_json"])
    return {
        "run_id": row["run_id"],
        "task_key": row["task_key"],
        "task_id": row["task_id"],
        "repo_slug": row["repo_slug"],
        "repo_path": row["repo_path"],
        "branch_name": row["branch_name"],
        "status": row["status"],
        "phase": row["phase"],
        "worker_id": row["worker_id"],
        "host_id": row["host_id"],
        "lease_id": row["lease_id"],
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
        "started_at_ms": row["started_at_ms"],
        "completed_at_ms": row["completed_at_ms"],
        "error": row["error"],
        "metadata": metadata,
    }


def row_to_worker(row: Any | None) -> dict[str, Any]:
    """Convert a ``workers`` table row to a worker dict."""
    if row is None:
        return {}
    return {
        "worker_id": row["worker_id"],
        "host_id": row["host_id"],
        "role": row["role"],
        "status": row["status"],
        "capabilities": _json_loads(row["capabilities_json"]),
        "current_run_id": row["current_run_id"],
        "current_lease_id": row["current_lease_id"],
        "last_seen_at_ms": row["last_seen_at_ms"],
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
    }


def row_to_lease(row: Any | None) -> dict[str, Any]:
    """Convert a ``leases`` table row to a lease dict."""
    if row is None:
        return {}
    return {
        "lease_id": row["lease_id"],
        "task_key": row["task_key"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "worker_id": row["worker_id"],
        "host_id": row["host_id"],
        "role": row["role"],
        "status": row["status"],
        "attempt": row["attempt"],
        "acquired_at_ms": row["acquired_at_ms"],
        "heartbeat_at_ms": row["heartbeat_at_ms"],
        "expires_at_ms": row["expires_at_ms"],
        "released_at_ms": row["released_at_ms"],
        "release_reason": row["release_reason"],
        "metadata": _json_loads(row["metadata_json"]),
    }


def row_to_outbox(row: Any | None) -> dict[str, Any]:
    """Convert an ``outbox`` table row to an outbox dict."""
    if row is None:
        return {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "aggregate_type": row["aggregate_type"],
        "aggregate_id": row["aggregate_id"],
        "payload": _json_loads(row["payload_json"]),
        "status": row["status"],
        "attempts": row["attempts"],
        "next_attempt_at_ms": row["next_attempt_at_ms"],
        "last_error": row["last_error"],
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
        "dedupe_key": row["dedupe_key"],
    }


def row_to_scheduler_event(row: Any | None) -> dict[str, Any]:
    """Convert a ``scheduler_events`` table row to an event dict."""
    if row is None:
        return {}
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "payload": _json_loads(row["payload_json"]),
        "created_at_ms": row["created_at_ms"],
    }
