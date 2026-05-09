"""coordination/runners.py -- Runners mixin for CoordinationStore."""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Iterator

from src.tandem_agents.utils.utils import now_ms, short_id, slugify
from src.tandem_agents.core.coordination.constants import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_OUTBOX_STALE_AFTER_SECONDS,
    DEFAULT_WORKER_STALE_AFTER_SECONDS,
    TASK_STATES,
)

# Helper function imports needed by some mixins
def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def _json_loads(value: str | None) -> Any:
    if not value: return {}
    try: return json.loads(value)
    except Exception: return {}

def _nonempty(value: Any) -> str:
    return str(value or "").strip()

class CoordinationRunnersMixin:
    """Runners mixin."""
    def record_run_start(
        self,
        *,
        run_id: str,
        task: dict[str, Any],
        repo: dict[str, Any],
        worker_id: str,
        host_id: str,
        lease_id: str | None,
        branch_name: str | None,
        phase: str = "bootstrap",
        status: str = "running",
    ) -> dict[str, Any]:
        now = now_ms()
        payload = self._task_payload(task, repo)
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_key, task_id, repo_slug, repo_path, branch_name, status, phase,
                    worker_id, host_id, lease_id, created_at_ms, updated_at_ms, started_at_ms,
                    completed_at_ms, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    task_key=excluded.task_key,
                    task_id=excluded.task_id,
                    repo_slug=excluded.repo_slug,
                    repo_path=excluded.repo_path,
                    branch_name=excluded.branch_name,
                    status=excluded.status,
                    phase=excluded.phase,
                    worker_id=excluded.worker_id,
                    host_id=excluded.host_id,
                    lease_id=excluded.lease_id,
                    updated_at_ms=excluded.updated_at_ms,
                    started_at_ms=COALESCE(runs.started_at_ms, excluded.started_at_ms),
                    metadata_json=excluded.metadata_json
                """,
                (
                    run_id,
                    payload["task_key"],
                    payload["task_id"],
                    payload["repo_slug"],
                    payload["repo_path"],
                    branch_name,
                    status,
                    phase,
                    worker_id,
                    host_id,
                    lease_id,
                    now,
                    now,
                    now,
                    _json_dumps({"task": task, "repo": repo}),
                ),
            )
        return self.get_run(run_id) or {"run_id": run_id, "status": status, "phase": phase}
    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        error: str | None = None,
        lease_id: str | None = None,
        branch_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        started: bool = False,
        completed: bool = False,
    ) -> dict[str, Any] | None:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            updates: list[str] = ["updated_at_ms = ?"]
            values: list[Any] = [now]
            if status is not None:
                updates.append("status = ?")
                values.append(status)
            if phase is not None:
                updates.append("phase = ?")
                values.append(phase)
            if error is not None:
                updates.append("error = ?")
                values.append(error)
            if lease_id is not None:
                updates.append("lease_id = ?")
                values.append(lease_id)
            if branch_name is not None:
                updates.append("branch_name = ?")
                values.append(branch_name)
            if started:
                updates.append("started_at_ms = COALESCE(started_at_ms, ?)")
                values.append(now)
            if completed:
                updates.append("completed_at_ms = ?")
                values.append(now)
            if metadata is not None:
                updates.append("metadata_json = ?")
                values.append(_json_dumps(metadata))
            values.append(run_id)
            conn.execute(f"UPDATE runs SET {', '.join(updates)} WHERE run_id = ?", values)
        return self.get_run(run_id)
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None
