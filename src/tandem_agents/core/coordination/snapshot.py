"""coordination/snapshot.py -- Snapshot mixin for CoordinationStore."""
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

class CoordinationSnapshotMixin:
    """Snapshot mixin."""
    def snapshot(self, *, limit: int = 25) -> dict[str, Any]:
        with self.connection() as conn:
            self.ensure_schema()
            runs = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
            leases = conn.execute(
                "SELECT * FROM leases ORDER BY heartbeat_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
            workers = conn.execute(
                "SELECT * FROM workers ORDER BY updated_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
            outbox = conn.execute(
                "SELECT * FROM outbox ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            scheduler_events = conn.execute(
                "SELECT * FROM scheduler_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            queued_tasks = conn.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE state = 'queued'",
            ).fetchone()
            active_tasks = conn.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE state IN ('claimed', 'active', 'review')",
            ).fetchone()
        tasks = self.list_tasks(limit=limit)
        return {
            "backend": self.backend,
            "db_path": str(self.db_path) if self.db_path is not None else "postgresql://configured",
            "summary": {
                "tasks": len(tasks),
                "queued_tasks": int((queued_tasks["count"] if queued_tasks else 0) or 0),
                "active_tasks": int((active_tasks["count"] if active_tasks else 0) or 0),
                "runs": len(runs),
                "leases": len(leases),
                "workers": len(workers),
                "pending_outbox": sum(1 for row in outbox if row["status"] == "pending"),
                "processing_outbox": sum(1 for row in outbox if row["status"] == "processing"),
                "failed_outbox": sum(1 for row in outbox if row["status"] == "failed"),
                "dispatched_outbox": sum(1 for row in outbox if row["status"] == "dispatched"),
                "scheduler_events": len(scheduler_events),
            },
            "tasks": tasks,
            "runs": [self._row_to_run(row) for row in runs],
            "leases": [self._row_to_lease(row) for row in leases],
            "workers": [self._row_to_worker(row) for row in workers],
            "outbox": [self._row_to_outbox(row) for row in outbox],
            "scheduler_events": [self._row_to_scheduler_event(row) for row in scheduler_events],
        }
