"""coordination/tasks.py -- Tasks mixin for CoordinationStore."""
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
from src.tandem_agents.core.task_contract import dependency_status_for_task, task_contract_completeness

# Helper function imports needed by some mixins
def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def _json_loads(value: str | None) -> Any:
    if not value: return {}
    try: return json.loads(value)
    except Exception: return {}

def _nonempty(value: Any) -> str:
    return str(value or "").strip()

class CoordinationTasksMixin:
    """Tasks mixin."""
    def _task_key(self, task: dict[str, Any]) -> tuple[str, str]:
        source = dict(task.get("source") or {})
        source_type = _nonempty(source.get("type")) or "unknown"
        task_id = _nonempty(task.get("task_id")) or _nonempty(source.get("card_id")) or _nonempty(source.get("item")) or _nonempty(source.get("project_item_id"))
        if source_type == "github_project":
            owner = _nonempty(source.get("owner"))
            project = _nonempty(source.get("project"))
            issue_number = _nonempty(source.get("issue_number"))
            project_item_id = _nonempty(source.get("project_item_id")) or _nonempty(source.get("item_id"))
            ref = f"{owner}/{project}:{project_item_id or issue_number or task_id or slugify(task.get('title', 'task'))}"
        elif source_type == "linear":
            team = _nonempty(source.get("team"))
            project = _nonempty(source.get("project")) or _nonempty(source.get("project_name"))
            issue_id = _nonempty(source.get("issue_id")) or _nonempty(source.get("identifier"))
            ref = f"{team}/{project or 'issues'}:{issue_id or task_id or slugify(task.get('title', 'task'))}"
        elif source_type == "kanban_board":
            board_path = _nonempty(source.get("board_path")) or _nonempty(source.get("path"))
            ref = f"{board_path}:{task_id or slugify(task.get('title', 'task'))}"
        elif source_type == "local_backlog":
            path = _nonempty(source.get("path"))
            ref = f"{path}:{task_id or slugify(task.get('title', 'task'))}"
        elif source_type == "manual":
            prompt = _nonempty(source.get("prompt"))
            ref = f"manual:{slugify(prompt or task.get('title', 'task'))}:{task_id or slugify(task.get('title', 'task'))}"
        else:
            ref = f"{source_type}:{_nonempty(source.get('source_name')) or task_id or slugify(task.get('title', 'task'))}"
        return f"{source_type}:{ref}", ref
    def _task_payload(self, task: dict[str, Any], repo: dict[str, Any] | None = None) -> dict[str, Any]:
        source = dict(task.get("source") or {})
        repo = dict(repo or {})
        task_key, source_ref = self._task_key(task)
        payload = {
            "task_key": task_key,
            "task_id": _nonempty(task.get("task_id")) or None,
            "source_type": _nonempty(source.get("type")) or "unknown",
            "source_ref": source_ref,
            "title": _nonempty(task.get("title")) or "Untitled task",
            "repo_slug": _nonempty((task.get("repo") or {}).get("slug")) or _nonempty(repo.get("slug")),
            "repo_path": _nonempty((task.get("repo") or {}).get("path")) or _nonempty(repo.get("path")),
            "board_path": _nonempty(source.get("board_path")),
            "metadata_json": _json_dumps(
                {
                    "task": task,
                    "repo": repo,
                }
            ),
        }
        return payload
    def _normalize_task_state(self, state: str) -> str:
        normalized = _nonempty(state).lower()
        return normalized if normalized in TASK_STATES else "queued"
    def _task_state_from_lease_status(self, status: str) -> str:
        normalized = _nonempty(status).lower()
        if normalized == "completed":
            return "done"
        if normalized in {"blocked", "failed", "cancelled"}:
            return "blocked"
        if normalized == "stale":
            return "stale"
        return self._normalize_task_state(normalized)
    def _apply_task_state_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_key: str,
        state: str,
        now: int,
        status: str | None = None,
        lease_expires_at_ms: int | None = None,
        run_id: str | None = None,
        lease_id: str | None = None,
        worker_id: str | None = None,
        host_id: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        clear_claim: bool = False,
    ) -> None:
        row = conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
        if row is None:
            return
        current_state = self._normalize_task_state(row["state"] or row["status"])
        next_state = self._normalize_task_state(state)
        current_metadata = _json_loads(row["metadata_json"])
        if metadata:
            current_metadata = {**current_metadata, **metadata}
        if reason:
            current_metadata["state_reason"] = reason
        update_status = status or next_state
        conn.execute(
            """
            UPDATE tasks
            SET status = ?,
                state = ?,
                claimed_run_id = COALESCE(?, claimed_run_id),
                claimed_lease_id = COALESCE(?, claimed_lease_id),
                claimed_by = COALESCE(?, claimed_by),
                claimed_host_id = COALESCE(?, claimed_host_id),
                lease_expires_at_ms = ?,
                updated_at_ms = ?,
                metadata_json = ?
            WHERE task_key = ?
            """,
            (
                update_status,
                next_state,
                run_id,
                lease_id,
                worker_id,
                host_id,
                lease_expires_at_ms,
                now,
                _json_dumps(current_metadata),
                task_key,
            ),
        )
        if current_state != next_state:
            conn.execute(
                "UPDATE tasks SET updated_at_ms = ? WHERE task_key = ?",
                (now, task_key),
            )
        if clear_claim:
            active_lease_rows = conn.execute(
                "SELECT lease_id, worker_id FROM leases WHERE task_key = ? AND status = 'active'",
                (task_key,),
            ).fetchall()
            if active_lease_rows:
                conn.execute(
                    """
                    UPDATE leases
                    SET status = 'stale',
                        released_at_ms = ?,
                        release_reason = ?
                    WHERE task_key = ? AND status = 'active'
                    """,
                    (now, reason or "operator cleared task claim", task_key),
                )
                for lease_row in active_lease_rows:
                    conn.execute(
                        """
                        UPDATE workers
                        SET status = 'idle',
                            current_lease_id = NULL,
                            current_run_id = NULL,
                            updated_at_ms = ?
                        WHERE worker_id = ? AND current_lease_id = ?
                        """,
                        (now, lease_row["worker_id"], lease_row["lease_id"]),
                    )
            conn.execute(
                """
                UPDATE tasks
                SET claimed_run_id = NULL,
                    claimed_lease_id = NULL,
                    claimed_by = NULL,
                    claimed_host_id = NULL,
                    lease_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE task_key = ?
                """,
                (now, task_key),
            )
    def transition_task_state(
        self,
        task_key: str,
        state: str,
        *,
        status: str | None = None,
        lease_expires_at_ms: int | None = None,
        run_id: str | None = None,
        lease_id: str | None = None,
        worker_id: str | None = None,
        host_id: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        clear_claim: bool = False,
    ) -> dict[str, Any] | None:
        now = now_ms()
        with self.connection() as conn:
            self.ensure_schema()
            self._apply_task_state_locked(
                conn,
                task_key=task_key,
                state=state,
                now=now,
                status=status,
                lease_expires_at_ms=lease_expires_at_ms,
                run_id=run_id,
                lease_id=lease_id,
                worker_id=worker_id,
                host_id=host_id,
                reason=reason,
                metadata=metadata,
                clear_claim=clear_claim,
            )
        return self.get_task(task_key)
    def mark_task_claimed(
        self,
        task_key: str,
        *,
        run_id: str,
        lease_id: str,
        worker_id: str,
        host_id: str,
        lease_expires_at_ms: int,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self.transition_task_state(
            task_key,
            "claimed",
            status="claimed",
            lease_expires_at_ms=lease_expires_at_ms,
            run_id=run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            reason=reason,
        )
    def mark_task_active(
        self,
        task_key: str,
        *,
        run_id: str,
        lease_id: str,
        worker_id: str,
        host_id: str,
        lease_expires_at_ms: int,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self.transition_task_state(
            task_key,
            "active",
            status="active",
            lease_expires_at_ms=lease_expires_at_ms,
            run_id=run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            reason=reason,
        )
    def mark_task_review(
        self,
        task_key: str,
        *,
        run_id: str,
        lease_id: str,
        worker_id: str,
        host_id: str,
        lease_expires_at_ms: int,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self.transition_task_state(
            task_key,
            "review",
            status="review",
            lease_expires_at_ms=lease_expires_at_ms,
            run_id=run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            reason=reason,
        )
    def mark_task_blocked(
        self,
        task_key: str,
        *,
        run_id: str,
        lease_id: str | None,
        worker_id: str | None,
        host_id: str | None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self.transition_task_state(
            task_key,
            "blocked",
            status="blocked",
            run_id=run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            reason=reason,
        )
    def mark_task_done(
        self,
        task_key: str,
        *,
        run_id: str,
        lease_id: str | None,
        worker_id: str | None,
        host_id: str | None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self.transition_task_state(
            task_key,
            "done",
            status="done",
            run_id=run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            reason=reason,
        )
    def register_task(self, task: dict[str, Any], *, repo: dict[str, Any] | None = None, status: str = "queued") -> dict[str, Any]:
        now = now_ms()
        payload = self._task_payload(task, repo)
        task_state = self._normalize_task_state(status)
        with self.connection() as conn:
            self.ensure_schema()
            conn.execute(
                """
                INSERT INTO tasks (
                    task_key, task_id, source_type, source_ref, title,
                    repo_slug, repo_path, board_path, status,
                    state,
                    claimed_run_id, claimed_lease_id, claimed_by, claimed_host_id, lease_expires_at_ms,
                    created_at_ms, updated_at_ms, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    task_id=excluded.task_id,
                    source_type=excluded.source_type,
                    source_ref=excluded.source_ref,
                    title=excluded.title,
                    repo_slug=excluded.repo_slug,
                    repo_path=excluded.repo_path,
                    board_path=excluded.board_path,
                    status=excluded.status,
                    state=excluded.state,
                    updated_at_ms=excluded.updated_at_ms,
                    metadata_json=excluded.metadata_json
                """,
                (
                    payload["task_key"],
                    payload["task_id"],
                    payload["source_type"],
                    payload["source_ref"],
                    payload["title"],
                    payload["repo_slug"],
                    payload["repo_path"],
                    payload["board_path"],
                    task_state,
                    task_state,
                    now,
                    now,
                    payload["metadata_json"],
                ),
            )
        return self.get_task(payload["task_key"]) or {"task_key": payload["task_key"], **payload, "status": task_state, "state": task_state}
    def get_task(self, task_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            self.ensure_schema()
            row = conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
        return self._row_to_task(row) if row else None
    def task_ownership(self, task_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            self.ensure_schema()
            task_row = conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
            if task_row is None:
                return None
            task = self._row_to_task(task_row)
            lease_row = None
            claimed_lease_id = str(task.get("claimed_lease_id") or "").strip()
            if claimed_lease_id:
                lease_row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (claimed_lease_id,)).fetchone()
            if lease_row is None:
                lease_row = conn.execute(
                    "SELECT * FROM leases WHERE task_key = ? ORDER BY acquired_at_ms DESC LIMIT 1",
                    (task_key,),
                ).fetchone()
            lease = self._row_to_lease(lease_row) if lease_row else None
            run = None
            run_id = str(task.get("claimed_run_id") or (lease or {}).get("run_id") or "").strip()
            if run_id:
                run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if run_row is not None:
                    run = self._row_to_run(run_row)
            worker = None
            worker_id = str((lease or {}).get("worker_id") or task.get("claimed_by") or "").strip()
            if worker_id:
                worker_row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
                if worker_row is not None:
                    worker = self._row_to_worker(worker_row)
            task_state = self._normalize_task_state(task.get("state") or task.get("status") or "")
            lease_state = self._normalize_task_state((lease or {}).get("status") or "")
            ownership_state = task_state
            if lease_state == "active" and task_state in {"claimed", "active", "review"}:
                ownership_state = "owned"
            elif lease_state == "stale" or task_state == "stale":
                ownership_state = "reclaimable"
            elif task_state in {"blocked", "done"}:
                ownership_state = task_state
            elif task_state in {"claimed", "active", "review"}:
                ownership_state = task_state
            state_reason = str((task.get("metadata") or {}).get("state_reason") or "").strip()
            release_reason = str((lease or {}).get("release_reason") or "").strip()
            reason = state_reason or release_reason or None
            return {
                "task_key": task_key,
                "task": task,
                "lease": lease,
                "run": run,
                "worker": worker,
                "task_state": task_state,
                "lease_state": lease_state,
                "ownership_state": ownership_state,
                "is_current": bool(lease and lease.get("status") == "active"),
                "reclaimable": ownership_state == "reclaimable",
                "blocked_reason": reason,
            }
    def list_tasks(
        self,
        *,
        state: str | None = None,
        status: str | None = None,
        source_type: str | None = None,
        repo_slug: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if state:
            clauses.append("state = ?")
            values.append(str(state).strip())
        if status:
            clauses.append("status = ?")
            values.append(str(status).strip())
        if source_type:
            clauses.append("source_type = ?")
            values.append(str(source_type).strip())
        if repo_slug:
            clauses.append("repo_slug = ?")
            values.append(str(repo_slug).strip())
        query = "SELECT * FROM tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at_ms DESC LIMIT ?"
        values.append(max(1, int(limit or 1)))
        with self.connection() as conn:
            self.ensure_schema()
            rows = conn.execute(query, tuple(values)).fetchall()
        tasks = [self._row_to_task(row) for row in rows]
        for index, task in enumerate(tasks):
            known_tasks = [candidate for candidate in tasks if candidate.get("task_key") != task.get("task_key")]
            task["contract_completeness"] = task_contract_completeness(task)
            task["dependency_status"] = dependency_status_for_task(task, known_tasks if known_tasks else None)
            tasks[index] = task
        return tasks
    def claim_task(
        self,
        task: dict[str, Any],
        *,
        run_id: str,
        worker_id: str,
        host_id: str,
        role: str = "coordinator",
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        branch_name: str | None = None,
        repo: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = now_ms()
        ttl = max(1, int(lease_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS)) * 1000
        payload = self._task_payload(task, repo)
        task_key = payload["task_key"]
        with self.connection() as conn:
            self.ensure_schema()
            self._begin_transaction(conn)
            self._reap_expired_leases_locked(conn, now)
            self._reap_stale_workers_locked(conn, now)
            task_row = conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
            known_tasks = [
                self._row_to_task(row)
                for row in conn.execute("SELECT * FROM tasks").fetchall()
            ]
            known_tasks = [candidate for candidate in known_tasks if candidate.get("task_key") != task_key]
            dependency_status = dependency_status_for_task(task, known_tasks if known_tasks else None)
            if dependency_status.get("blocked"):
                task_payload = self._row_to_task(task_row) if task_row is not None else self._task_payload(task, repo)
                task_payload["dependency_status"] = dependency_status
                task_payload["contract_completeness"] = task_contract_completeness(task_payload)
                return {
                    "claimed": False,
                    "reason": "dependency_blocked",
                    "blocked_reason": dependency_status.get("blocked_reason"),
                    "dependency_status": dependency_status,
                    "task": task_payload,
                }
            active = conn.execute(
                """
                SELECT * FROM leases
                WHERE task_key = ? AND status = 'active' AND expires_at_ms > ?
                ORDER BY expires_at_ms DESC
                LIMIT 1
                """,
                (task_key, now),
            ).fetchone()
            if active:
                return {
                    "claimed": False,
                    "reason": "active_lease_exists",
                    "task": self._row_to_task(conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()),
                    "active_lease": self._row_to_lease(active),
                }
            attempt = self._next_attempt_locked(conn, task_key)
            lease_id = f"lease-{short_id()}"
            expires_at = now + ttl
            conn.execute(
                """
                INSERT INTO leases (
                    lease_id, task_key, task_id, run_id, worker_id, host_id, role, status,
                    attempt, acquired_at_ms, heartbeat_at_ms, expires_at_ms, released_at_ms,
                    release_reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(lease_id) DO UPDATE SET
                    task_key=excluded.task_key,
                    task_id=excluded.task_id,
                    run_id=excluded.run_id,
                    worker_id=excluded.worker_id,
                    host_id=excluded.host_id,
                    role=excluded.role,
                    status=excluded.status,
                    attempt=excluded.attempt,
                    acquired_at_ms=excluded.acquired_at_ms,
                    heartbeat_at_ms=excluded.heartbeat_at_ms,
                    expires_at_ms=excluded.expires_at_ms,
                    released_at_ms=NULL,
                    release_reason=NULL,
                    metadata_json=excluded.metadata_json
                """,
                (
                    lease_id,
                    task_key,
                    payload["task_id"],
                    run_id,
                    worker_id,
                    host_id,
                    role,
                    attempt,
                    now,
                    now,
                    expires_at,
                    _json_dumps(
                        {
                            "task": task,
                            "repo": repo or {},
                            "branch_name": branch_name,
                        }
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_key, task_id, repo_slug, repo_path, branch_name, status, phase,
                    worker_id, host_id, lease_id, created_at_ms, updated_at_ms, started_at_ms,
                    completed_at_ms, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'created', 'bootstrap', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    task_key=excluded.task_key,
                    task_id=excluded.task_id,
                    repo_slug=excluded.repo_slug,
                    repo_path=excluded.repo_path,
                    branch_name=excluded.branch_name,
                    worker_id=excluded.worker_id,
                    host_id=excluded.host_id,
                    lease_id=excluded.lease_id,
                    updated_at_ms=excluded.updated_at_ms,
                    metadata_json=excluded.metadata_json
                """,
                (
                    run_id,
                    task_key,
                    payload["task_id"],
                    payload["repo_slug"],
                    payload["repo_path"],
                    branch_name,
                    worker_id,
                    host_id,
                    lease_id,
                    now,
                    now,
                    _json_dumps({"task": task, "repo": repo or {}}),
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                    task_key, task_id, source_type, source_ref, title, repo_slug, repo_path, board_path, status, state,
                    claimed_run_id, claimed_lease_id, claimed_by, claimed_host_id, lease_expires_at_ms,
                    created_at_ms, updated_at_ms, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', 'claimed', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    task_id=excluded.task_id,
                    source_type=excluded.source_type,
                    source_ref=excluded.source_ref,
                    title=excluded.title,
                    repo_slug=excluded.repo_slug,
                    repo_path=excluded.repo_path,
                    board_path=excluded.board_path,
                    status='claimed',
                    state='claimed',
                    claimed_run_id=excluded.claimed_run_id,
                    claimed_lease_id=excluded.claimed_lease_id,
                    claimed_by=excluded.claimed_by,
                    claimed_host_id=excluded.claimed_host_id,
                    lease_expires_at_ms=excluded.lease_expires_at_ms,
                    updated_at_ms=excluded.updated_at_ms,
                    metadata_json=excluded.metadata_json
                """,
                (
                    task_key,
                    payload["task_id"],
                    payload["source_type"],
                    payload["source_ref"],
                    payload["title"],
                    payload["repo_slug"],
                    payload["repo_path"],
                    payload["board_path"],
                    run_id,
                    lease_id,
                    worker_id,
                    host_id,
                    expires_at,
                    now,
                    now,
                    payload["metadata_json"],
                ),
            )
            conn.execute(
                """
                INSERT INTO workers (
                    worker_id, host_id, role, status, capabilities_json,
                    current_run_id, current_lease_id, last_seen_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, 'busy', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    host_id=excluded.host_id,
                    role=excluded.role,
                    status='busy',
                    current_run_id=excluded.current_run_id,
                    current_lease_id=excluded.current_lease_id,
                    last_seen_at_ms=excluded.last_seen_at_ms,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    worker_id,
                    host_id,
                    role,
                    _json_dumps({}),
                    run_id,
                    lease_id,
                    now,
                    now,
                    now,
                ),
            )
            self._apply_task_state_locked(
                conn,
                task_key=task_key,
                state="claimed",
                status="claimed",
                now=now,
                lease_expires_at_ms=expires_at,
                run_id=run_id,
                lease_id=lease_id,
                worker_id=worker_id,
                host_id=host_id,
                metadata={"task": task, "repo": repo or {}},
            )
            conn.commit()
        return {
            "claimed": True,
            "lease": self.get_lease(lease_id),
            "task": self.get_task(task_key),
        }
