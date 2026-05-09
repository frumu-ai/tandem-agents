"""coordination/outbox.py -- Outbox mixin for CoordinationStore."""
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

class CoordinationOutboxMixin:
    """Outbox mixin."""
    def enqueue_outbox(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                INSERT INTO outbox (
                    kind, aggregate_type, aggregate_id, payload_json, status,
                    attempts, next_attempt_at_ms, last_error, created_at_ms, updated_at_ms, dedupe_key
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status='pending',
                    next_attempt_at_ms=excluded.next_attempt_at_ms,
                    updated_at_ms=excluded.updated_at_ms,
                    last_error=NULL
                """,
                (kind, aggregate_type, aggregate_id, _json_dumps(payload), now, now, now, dedupe_key),
            )
            row = conn.execute(
                """
                SELECT * FROM outbox
                WHERE (dedupe_key = ? AND dedupe_key IS NOT NULL)
                   OR (dedupe_key IS NULL AND kind = ? AND aggregate_id = ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (dedupe_key, kind, aggregate_id),
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        return self._row_to_outbox(row) if row else {}
    def mark_outbox_dispatched(self, outbox_id: int) -> None:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                UPDATE outbox
                SET status = 'dispatched',
                    updated_at_ms = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, outbox_id),
            )
    def list_pending_outbox(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            self.ensure_schema()
            rows = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status = 'pending'
                ORDER BY id ASC
                """
            ).fetchall()
        return [self._row_to_outbox(row) for row in rows]
    def claim_pending_outbox(self, *, limit: int = 25) -> list[dict[str, Any]]:
        now = now_ms()
        limit = max(1, int(limit or 1))
        claimed: list[dict[str, Any]] = []
        with self.connection() as conn:
            self.ensure_schema()
            self._begin_transaction(conn)
            rows = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status = 'pending' AND next_attempt_at_ms <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                attempts = int(row["attempts"] or 0) + 1
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = 'processing',
                        attempts = ?,
                        updated_at_ms = ?,
                        last_error = NULL
                    WHERE id = ?
                    """,
                    (attempts, now, row["id"]),
                )
                claimed.append(
                    {
                        **self._row_to_outbox(row),
                        "status": "processing",
                        "attempts": attempts,
                        "updated_at_ms": now,
                    }
                )
            conn.commit()
        return claimed
    def complete_outbox(self, outbox_id: int) -> dict[str, Any] | None:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE outbox
                SET status = 'dispatched',
                    updated_at_ms = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, outbox_id),
            )
        return self._row_to_outbox(row)
    def retry_outbox(
        self,
        outbox_id: int,
        *,
        error: str,
        terminal: bool = False,
        delay_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"] or 0)
            if terminal:
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = 'failed',
                        updated_at_ms = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    (now, error, outbox_id),
                )
            else:
                wait_seconds = delay_seconds
                if wait_seconds is None:
                    wait_seconds = min(300, max(5, attempts * 5))
                next_attempt = now + max(1, int(wait_seconds)) * 1000
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = 'pending',
                        next_attempt_at_ms = ?,
                        updated_at_ms = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    (next_attempt, now, error, outbox_id),
                )
        return self._row_to_outbox(row)
    def reap_stale_outbox_claims(self, *, stale_after_seconds: int = DEFAULT_OUTBOX_STALE_AFTER_SECONDS) -> list[dict[str, Any]]:
        now = now_ms()
        stale_after_ms = max(1, int(stale_after_seconds or DEFAULT_OUTBOX_STALE_AFTER_SECONDS)) * 1000
        reset: list[dict[str, Any]] = []
        with self.connection() as conn:
            self.ensure_schema()
            self._begin_transaction(conn)
            rows = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status = 'processing' AND updated_at_ms <= ?
                ORDER BY id ASC
                """,
                (now - stale_after_ms,),
            ).fetchall()
            for row in rows:
                reset.append(self._row_to_outbox(row))
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = 'pending',
                        next_attempt_at_ms = ?,
                        updated_at_ms = ?,
                        last_error = COALESCE(last_error, 'processing claim expired')
                    WHERE id = ?
                    """,
                    (now, now, row["id"]),
                )
            conn.commit()
        return reset
