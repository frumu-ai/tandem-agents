"""coordination/workers.py -- Workers mixin for CoordinationStore."""
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

class CoordinationWorkersMixin:
    """Workers mixin."""
    def register_worker(
        self,
        *,
        worker_id: str,
        host_id: str,
        role: str,
        status: str = "idle",
        capabilities: dict[str, Any] | None = None,
        current_run_id: str | None = None,
        current_lease_id: str | None = None,
    ) -> dict[str, Any]:
        now = now_ms()
        capabilities_json = _json_dumps(capabilities or {})
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                INSERT INTO workers (
                    worker_id, host_id, role, status, capabilities_json,
                    current_run_id, current_lease_id, last_seen_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    host_id=excluded.host_id,
                    role=excluded.role,
                    status=excluded.status,
                    capabilities_json=excluded.capabilities_json,
                    current_run_id=excluded.current_run_id,
                    current_lease_id=excluded.current_lease_id,
                    last_seen_at_ms=excluded.last_seen_at_ms,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    worker_id,
                    host_id,
                    role,
                    status,
                    capabilities_json,
                    current_run_id,
                    current_lease_id,
                    now,
                    now,
                    now,
                ),
            )
        return {
            "worker_id": worker_id,
            "host_id": host_id,
            "role": role,
            "status": status,
            "capabilities": capabilities or {},
            "current_run_id": current_run_id,
            "current_lease_id": current_lease_id,
            "last_seen_at_ms": now,
        }
    def heartbeat_worker(
        self,
        worker_id: str,
        *,
        host_id: str | None = None,
        role: str | None = None,
        status: str | None = None,
        capabilities: dict[str, Any] | None = None,
        current_run_id: str | None = None,
        current_lease_id: str | None = None,
    ) -> dict[str, Any] | None:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if row is None:
                return None
            updates: list[str] = ["last_seen_at_ms = ?", "updated_at_ms = ?"]
            values: list[Any] = [now, now]
            if host_id is not None:
                updates.append("host_id = ?")
                values.append(host_id)
            if role is not None:
                updates.append("role = ?")
                values.append(role)
            if status is not None:
                updates.append("status = ?")
                values.append(status)
            if capabilities is not None:
                updates.append("capabilities_json = ?")
                values.append(_json_dumps(capabilities))
            if current_run_id is not None:
                updates.append("current_run_id = ?")
                values.append(current_run_id)
            if current_lease_id is not None:
                updates.append("current_lease_id = ?")
                values.append(current_lease_id)
            values.append(worker_id)
            conn.execute(f"UPDATE workers SET {', '.join(updates)} WHERE worker_id = ?", values)
        return self.get_worker(worker_id)
    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row_to_worker(row) if row else None
    def list_workers(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            self.ensure_schema()
            rows = conn.execute(
                "SELECT * FROM workers ORDER BY updated_at_ms DESC LIMIT ?",
                (max(1, int(limit or 1)),),
            ).fetchall()
        return [self._row_to_worker(row) for row in rows]
    def reap_stale_workers(self, *, stale_after_seconds: int = DEFAULT_WORKER_STALE_AFTER_SECONDS) -> list[dict[str, Any]]:
        now = now_ms()
        stale_after_ms = max(1, int(stale_after_seconds or DEFAULT_WORKER_STALE_AFTER_SECONDS)) * 1000
        stale: list[dict[str, Any]] = []
        with self.connection() as conn:
            self.ensure_schema()
            self._begin_transaction(conn)
            rows = conn.execute(
                """
                SELECT * FROM workers
                WHERE current_lease_id IS NOT NULL AND last_seen_at_ms <= ?
                ORDER BY last_seen_at_ms ASC
                """,
                (now - stale_after_ms,),
            ).fetchall()
            for row in rows:
                lease_id = str(row["current_lease_id"] or "").strip()
                lease = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone() if lease_id else None
                if lease and lease["status"] == "active":
                    conn.execute(
                        "UPDATE leases SET status = 'stale', released_at_ms = ?, release_reason = 'worker stale' WHERE lease_id = ?",
                        (now, lease_id),
                    )
                    self._clear_task_claim_for_lease_locked(
                        conn,
                        task_key=str(lease["task_key"]),
                        lease_id=lease_id,
                        now=now,
                        state="stale",
                        status="stale",
                    )
                conn.execute(
                    "UPDATE workers SET status = 'idle', current_lease_id = NULL, current_run_id = NULL, updated_at_ms = ? WHERE worker_id = ?",
                    (now, row["worker_id"]),
                )
                stale.append(self._row_to_worker(row))
            conn.commit()
        return stale
    def _reap_stale_workers_locked(self, conn: sqlite3.Connection, now: int, *, stale_after_seconds: int = DEFAULT_WORKER_STALE_AFTER_SECONDS) -> None:
        stale_after_ms = max(1, int(stale_after_seconds or DEFAULT_WORKER_STALE_AFTER_SECONDS)) * 1000
        rows = conn.execute(
            """
            SELECT * FROM workers
            WHERE current_lease_id IS NOT NULL AND last_seen_at_ms <= ?
            """,
            (now - stale_after_ms,),
        ).fetchall()
        for row in rows:
            lease_id = str(row["current_lease_id"] or "").strip()
            lease = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone() if lease_id else None
            if lease and lease["status"] == "active":
                conn.execute(
                    "UPDATE leases SET status = 'stale', released_at_ms = ?, release_reason = 'worker stale' WHERE lease_id = ?",
                    (now, lease_id),
                )
                self._clear_task_claim_for_lease_locked(
                    conn,
                    task_key=str(lease["task_key"]),
                    lease_id=lease_id,
                    now=now,
                    state="stale",
                    status="stale",
                )
            conn.execute(
                "UPDATE workers SET status = 'idle', current_lease_id = NULL, current_run_id = NULL, updated_at_ms = ? WHERE worker_id = ?",
                (now, row["worker_id"]),
            )
