"""coordination/leases.py -- Leases mixin for CoordinationStore."""
from __future__ import annotations
import json
import logging
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

logger = logging.getLogger("aca.coordination.leases")

# Helper function imports needed by some mixins
def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def _json_loads(value: str | None) -> Any:
    if not value: return {}
    try: return json.loads(value)
    except Exception: return {}

def _nonempty(value: Any) -> str:
    return str(value or "").strip()

class CoordinationLeasesMixin:
    """Leases mixin."""
    def _next_attempt_locked(self, conn: sqlite3.Connection, task_key: str) -> int:
        row = conn.execute("SELECT COUNT(*) AS count FROM leases WHERE task_key = ?", (task_key,)).fetchone()
        return int((row["count"] if row else 0) or 0) + 1
    def list_leases(
        self,
        *,
        status: str | None = None,
        task_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List leases, optionally filtered by status and/or task_key.

        status:
            - None / "all": all leases
            - "active": active leases only
            - "expired": active leases past their expires_at_ms
            - any literal status value ("active", "stale", "completed", "blocked", "failed"): exact match
        """
        clauses: list[str] = []
        params: list[Any] = []
        normalized_status = (status or "").strip().lower()
        if normalized_status == "expired":
            clauses.append("status = 'active' AND expires_at_ms <= ?")
            params.append(now_ms())
        elif normalized_status and normalized_status != "all":
            clauses.append("status = ?")
            params.append(normalized_status)
        if task_key:
            clauses.append("task_key = ?")
            params.append(task_key)
        sql = "SELECT * FROM leases"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY heartbeat_at_ms DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            self.ensure_schema()
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_lease(row) for row in rows]
    def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        return self._row_to_lease(row) if row else None
    def heartbeat_lease(self, lease_id: str, *, lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> dict[str, Any] | None:
        """Refresh a lease's expiration if still active.

        Returns the updated lease dict on success, or None if the lease no
        longer exists OR is no longer in 'active' status (e.g. already reaped
        or released). Callers should treat None as a heartbeat MISS — the
        worker has lost ownership of the task and should not continue mutating
        run state on a dead lease. See runner_core._touch_coordination for the
        consecutive-miss tracking that uses this signal.
        """
        now = now_ms()
        ttl = max(1, int(lease_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS)) * 1000
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                logger.warning(
                    "Heartbeat for unknown lease_id=%s — lease row missing.",
                    lease_id,
                )
                return None
            expires_at = now + ttl
            cur = conn.execute(
                """
                UPDATE leases
                SET heartbeat_at_ms = ?, expires_at_ms = ?, metadata_json = metadata_json
                WHERE lease_id = ? AND status = 'active'
                """,
                (now, expires_at, lease_id),
            )
            if (cur.rowcount or 0) == 0:
                # Lease exists but is no longer active (e.g. reaped or released).
                # Don't refresh task / worker rows — they reflect the new state.
                logger.warning(
                    "Heartbeat for inactive lease_id=%s status=%s task_key=%s — lease was likely reaped.",
                    lease_id,
                    str(row["status"]),
                    str(row["task_key"]),
                )
                return None
            conn.execute(
                """
                UPDATE tasks
                SET lease_expires_at_ms = ?, updated_at_ms = ?
                WHERE task_key = ?
                """,
                (expires_at, now, row["task_key"]),
            )
            conn.execute(
                """
                UPDATE workers
                SET last_seen_at_ms = ?, updated_at_ms = ?, current_lease_id = ?
                WHERE worker_id = ?
                """,
                (now, now, lease_id, row["worker_id"]),
            )
        return self.get_lease(lease_id)
    def release_lease(self, lease_id: str, *, status: str, reason: str | None = None) -> dict[str, Any] | None:
        now = now_ms()
        task_state = self._task_state_from_lease_status(status)
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                return None
            # Filter on status='active' so a second release is a safe no-op
            # rather than overwriting the original release reason.
            cur = conn.execute(
                """
                UPDATE leases
                SET status = ?, released_at_ms = ?, release_reason = ?
                WHERE lease_id = ? AND status = 'active'
                """,
                (status, now, reason, lease_id),
            )
            if (cur.rowcount or 0) == 0:
                # Already released — return the existing state without touching
                # the dependent task/worker rows.
                return self.get_lease(lease_id)
            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    state = ?,
                    claimed_run_id = CASE WHEN claimed_lease_id = ? THEN NULL ELSE claimed_run_id END,
                    claimed_lease_id = CASE WHEN claimed_lease_id = ? THEN NULL ELSE claimed_lease_id END,
                    claimed_by = CASE WHEN claimed_lease_id = ? THEN NULL ELSE claimed_by END,
                    claimed_host_id = CASE WHEN claimed_lease_id = ? THEN NULL ELSE claimed_host_id END,
                    lease_expires_at_ms = CASE WHEN claimed_lease_id = ? THEN NULL ELSE lease_expires_at_ms END,
                    updated_at_ms = ?
                WHERE task_key = ?
                """,
                (
                    task_state,
                    task_state,
                    lease_id,
                    lease_id,
                    lease_id,
                    lease_id,
                    lease_id,
                    now,
                    row["task_key"],
                ),
            )
            conn.execute(
                """
                UPDATE workers
                SET status = ?, current_lease_id = NULL, current_run_id = NULL, updated_at_ms = ?
                WHERE worker_id = ?
                """,
                ("idle" if status in {"completed", "blocked", "failed"} else status, now, row["worker_id"]),
            )
        return self.get_lease(lease_id)
    def reap_expired_leases(self) -> list[dict[str, Any]]:
        now = now_ms()
        expired: list[dict[str, Any]] = []
        with self.connection() as conn:
            self.ensure_schema()
            self._begin_transaction(conn)
            rows = conn.execute(
                "SELECT * FROM leases WHERE status = 'active' AND expires_at_ms <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                expired.append(self._row_to_lease(row))
                conn.execute(
                    "UPDATE leases SET status = 'stale', released_at_ms = ?, release_reason = 'expired' WHERE lease_id = ?",
                    (now, row["lease_id"]),
                )
                self._clear_task_claim_for_lease_locked(
                    conn,
                    task_key=str(row["task_key"]),
                    lease_id=str(row["lease_id"]),
                    now=now,
                    state="stale",
                    status="stale",
                )
                conn.execute(
                    "UPDATE workers SET status = 'idle', current_lease_id = NULL, current_run_id = NULL, updated_at_ms = ? WHERE worker_id = ?",
                    (now, row["worker_id"]),
                )
            conn.commit()
        return expired
    def _reap_expired_leases_locked(self, conn: sqlite3.Connection, now: int) -> None:
        rows = conn.execute(
            "SELECT * FROM leases WHERE status = 'active' AND expires_at_ms <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE leases SET status = 'stale', released_at_ms = ?, release_reason = 'expired' WHERE lease_id = ?",
                (now, row["lease_id"]),
            )
            self._clear_task_claim_for_lease_locked(
                conn,
                task_key=str(row["task_key"]),
                lease_id=str(row["lease_id"]),
                now=now,
                state="stale",
                status="stale",
            )
            conn.execute(
                "UPDATE workers SET status = 'idle', current_lease_id = NULL, current_run_id = NULL, updated_at_ms = ? WHERE worker_id = ?",
                (now, row["worker_id"]),
            )
