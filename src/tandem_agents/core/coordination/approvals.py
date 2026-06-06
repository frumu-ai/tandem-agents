"""coordination/approvals.py -- External action approval queue."""
from __future__ import annotations

import json
from typing import Any

from src.tandem_agents.utils.utils import now_ms, short_id


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class CoordinationApprovalsMixin:
    def enqueue_external_action_approval(
        self,
        *,
        run_id: str,
        task_id: str,
        source_type: str,
        adapter: str,
        action_type: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        risk_level: str,
        verification_marker: str = "",
        requested_by: str = "aca",
        expires_at_ms: int | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        now = now_ms()
        approval_id = f"approval-{short_id('act')}"
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                INSERT INTO external_action_approvals (
                    approval_id, run_id, task_id, source_type, adapter, action_type,
                    target_json, payload_json, risk_level, verification_marker, status,
                    requested_by, decided_by, decision_reason, result_json, error,
                    created_at_ms, updated_at_ms, decided_at_ms, executed_at_ms,
                    expires_at_ms, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, '{}', NULL, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    target_json=excluded.target_json,
                    payload_json=excluded.payload_json,
                    risk_level=excluded.risk_level,
                    verification_marker=excluded.verification_marker,
                    status=CASE
                        WHEN external_action_approvals.status IN ('executed', 'approved') THEN external_action_approvals.status
                        ELSE 'pending'
                    END,
                    updated_at_ms=excluded.updated_at_ms,
                    error=NULL
                """,
                (
                    approval_id,
                    run_id,
                    task_id,
                    source_type,
                    adapter,
                    action_type,
                    _json_dumps(target),
                    _json_dumps(payload),
                    risk_level,
                    verification_marker,
                    requested_by,
                    now,
                    now,
                    expires_at_ms,
                    dedupe_key,
                ),
            )
            if dedupe_key:
                row = conn.execute(
                    "SELECT * FROM external_action_approvals WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM external_action_approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
        return self._row_to_external_action_approval(row)

    def list_external_action_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            self.ensure_schema()
            rows = conn.execute(
                f"""
                SELECT * FROM external_action_approvals
                {where}
                ORDER BY created_at_ms DESC
                LIMIT ?
                """,
                (*values, max(1, int(limit or 1))),
            ).fetchall()
        return [self._row_to_external_action_approval(row) for row in rows]

    def get_external_action_approval(self, approval_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute(
                "SELECT * FROM external_action_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._row_to_external_action_approval(row)

    def decide_external_action_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        decision_status = "approved" if str(decision).strip().lower() == "approve" else "rejected"
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE external_action_approvals
                SET status = ?,
                    decided_by = ?,
                    decision_reason = ?,
                    decided_at_ms = ?,
                    updated_at_ms = ?,
                    error = NULL
                WHERE approval_id = ? AND status = 'pending'
                """,
                (decision_status, actor, reason, now, now, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM external_action_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._row_to_external_action_approval(row)

    def approve_pending_external_action_approvals(
        self,
        *,
        run_id: str,
        actor: str,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE external_action_approvals
                SET status = 'approved',
                    decided_by = ?,
                    decision_reason = ?,
                    decided_at_ms = ?,
                    updated_at_ms = ?,
                    error = NULL
                WHERE run_id = ? AND status = 'pending'
                """,
                (actor, reason, now, now, run_id),
            )
            rows = conn.execute(
                """
                SELECT * FROM external_action_approvals
                WHERE run_id = ? AND status = 'approved'
                ORDER BY created_at_ms ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_external_action_approval(row) for row in rows]

    def retry_external_action_approval(
        self,
        approval_id: str,
        *,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE external_action_approvals
                SET status = 'approved',
                    decided_by = ?,
                    decision_reason = ?,
                    decided_at_ms = ?,
                    executed_at_ms = NULL,
                    result_json = '{}',
                    error = NULL,
                    updated_at_ms = ?
                WHERE approval_id = ? AND status = 'failed'
                """,
                (actor, reason, now, now, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM external_action_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._row_to_external_action_approval(row)

    def retry_failed_external_action_approvals(
        self,
        *,
        run_id: str,
        actor: str,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE external_action_approvals
                SET status = 'approved',
                    decided_by = ?,
                    decision_reason = ?,
                    decided_at_ms = ?,
                    executed_at_ms = NULL,
                    result_json = '{}',
                    error = NULL,
                    updated_at_ms = ?
                WHERE run_id = ? AND status = 'failed'
                """,
                (actor, reason, now, now, run_id),
            )
            rows = conn.execute(
                """
                SELECT * FROM external_action_approvals
                WHERE run_id = ? AND status = 'approved'
                ORDER BY created_at_ms ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_external_action_approval(row) for row in rows]

    def mark_external_action_executed(
        self,
        approval_id: str,
        *,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE external_action_approvals
                SET status = 'executed',
                    result_json = ?,
                    error = NULL,
                    executed_at_ms = ?,
                    updated_at_ms = ?
                WHERE approval_id = ?
                """,
                (_json_dumps(result), now, now, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM external_action_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._row_to_external_action_approval(row)

    def mark_external_action_failed(
        self,
        approval_id: str,
        *,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE external_action_approvals
                SET status = 'failed',
                    result_json = ?,
                    error = ?,
                    executed_at_ms = ?,
                    updated_at_ms = ?
                WHERE approval_id = ?
                """,
                (_json_dumps(result or {}), error, now, now, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM external_action_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._row_to_external_action_approval(row)
