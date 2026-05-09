"""coordination/scheduler.py -- Scheduler mixin for CoordinationStore."""
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

class CoordinationSchedulerMixin:
    """Scheduler mixin."""
    def record_scheduler_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                INSERT INTO scheduler_events (event_type, payload_json, created_at_ms)
                VALUES (?, ?, ?)
                """,
                (event_type, _json_dumps(payload), now),
            )
            row = conn.execute("SELECT * FROM scheduler_events ORDER BY id DESC LIMIT 1").fetchone()
        return self._row_to_scheduler_event(row) if row else {}
    def list_scheduler_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            self.ensure_schema()
            rows = conn.execute(
                "SELECT * FROM scheduler_events ORDER BY id DESC LIMIT ?",
                (max(1, int(limit or 1)),),
            ).fetchall()
        return [self._row_to_scheduler_event(row) for row in rows]
