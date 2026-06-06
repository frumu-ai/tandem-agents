from __future__ import annotations

import json
from src.tandem_agents.core.coordination.constants import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_OUTBOX_STALE_AFTER_SECONDS,
    DEFAULT_WORKER_STALE_AFTER_SECONDS,
    TASK_STATES,
    COORDINATION_BACKENDS,
)
import os
import socket
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.utils.utils import now_ms, short_id, slugify
from src.tandem_agents.core.coordination.schema import apply_sqlite_schema, apply_postgres_schema
from src.tandem_agents.core.coordination.rows import (
    row_to_task,
    row_to_run,
    row_to_worker,
    row_to_lease,
    row_to_outbox,
    row_to_scheduler_event,
    row_to_external_action_approval,
)

from src.tandem_agents.core.coordination.tasks import CoordinationTasksMixin
from src.tandem_agents.core.coordination.runners import CoordinationRunnersMixin
from src.tandem_agents.core.coordination.workers import CoordinationWorkersMixin
from src.tandem_agents.core.coordination.leases import CoordinationLeasesMixin
from src.tandem_agents.core.coordination.outbox import CoordinationOutboxMixin
from src.tandem_agents.core.coordination.approvals import CoordinationApprovalsMixin
from src.tandem_agents.core.coordination.scheduler import CoordinationSchedulerMixin
from src.tandem_agents.core.coordination.snapshot import CoordinationSnapshotMixin

def _nonempty(value: Any) -> str:
    return str(value or "").strip()







def _translate_placeholders(sql: str) -> str:
    return sql.replace("?", "%s")

def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    for chunk in script.split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements

class _PostgresConnectionAdapter:
    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> Any:
        cursor = self._connection.cursor()
        cursor.execute(_translate_placeholders(sql), tuple(params or ()))
        return cursor

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def _connect_postgres(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Postgres coordination requires the `psycopg` package. "
            "Install Tandem Agents with Postgres support to use storage.profile=shared."
        ) from exc

    connection = psycopg.connect(dsn)
    connection.autocommit = True
    connection.row_factory = dict_row
    return connection


def coordination_db_path(cfg: ResolvedConfig) -> Path:
    raw = _nonempty(cfg.coordination.sqlite_path)
    if not raw:
        raw = str(cfg.output_root() / "state" / "coordination.sqlite3")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cfg.root_dir / path
    return path.resolve()


def default_worker_id(cfg: ResolvedConfig) -> str:
    explicit = _nonempty(cfg.coordination.worker_id) or _nonempty(cfg.env.get("ACA_WORKER_ID"))
    if explicit:
        return explicit
    return f"{socket.gethostname()}-{os.getpid()}"


def default_host_id(cfg: ResolvedConfig) -> str:
    explicit = _nonempty(cfg.coordination.host_id) or _nonempty(cfg.env.get("ACA_HOST_ID"))
    if explicit:
        return explicit
    return socket.gethostname()


class CoordinationStore(
    CoordinationTasksMixin,
    CoordinationRunnersMixin,
    CoordinationWorkersMixin,
    CoordinationLeasesMixin,
    CoordinationOutboxMixin,
    CoordinationApprovalsMixin,
    CoordinationSchedulerMixin,
    CoordinationSnapshotMixin,
):
    def __init__(self, *, backend: str, db_path: Path | None = None, postgres_url: str = ""):
        self.backend = backend
        self.db_path = db_path
        self.postgres_url = postgres_url
        if self.backend == "sqlite" and self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    @classmethod
    def from_config(cls, cfg: ResolvedConfig) -> "CoordinationStore":
        backend = str(cfg.coordination.backend or "").strip().lower()
        if backend not in COORDINATION_BACKENDS:
            raise RuntimeError(
                f"coordination.backend={backend or 'unknown'} is not supported. "
                f"Expected one of: {', '.join(sorted(COORDINATION_BACKENDS))}."
            )
        if backend == "postgres":
            postgres_url = _nonempty(cfg.storage.postgres_url)
            if not postgres_url:
                raise RuntimeError("Postgres coordination backend requires storage.postgres_url.")
            return cls(backend=backend, postgres_url=postgres_url)
        return cls(backend=backend, db_path=coordination_db_path(cfg))

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self.backend == "postgres":
            conn = _connect_postgres(self.postgres_url)
            wrapped = _PostgresConnectionAdapter(conn)
            try:
                yield wrapped
            finally:
                wrapped.close()
            return
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _begin_transaction(self, conn: Any) -> None:
        conn.execute("BEGIN" if self.backend == "postgres" else "BEGIN IMMEDIATE")

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self.connection() as conn:
            if self.backend == "postgres":
                apply_postgres_schema(conn)
            else:
                apply_sqlite_schema(conn)
        self._schema_ready = True










































    def _row_to_task(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_task(row)

    def _row_to_run(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_run(row)

    def _row_to_worker(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_worker(row)

    def _row_to_lease(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_lease(row)

    def _row_to_outbox(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_outbox(row)
    def _row_to_external_action_approval(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_external_action_approval(row)

    def _row_to_scheduler_event(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return row_to_scheduler_event(row)
