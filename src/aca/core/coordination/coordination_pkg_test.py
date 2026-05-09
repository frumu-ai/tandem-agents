"""Tests for coordination standalone utilities.

Covers:
- schema constants exist and contain expected table names
- apply_sqlite_schema creates all required tables on a fresh in-memory DB
- row_to_* helpers return empty dict for None and correct keys for a row
"""
from __future__ import annotations

import json
import sqlite3
import unittest


class SchemaTest(unittest.TestCase):
    """Schema constants and apply_sqlite_schema."""

    def test_sqlite_schema_contains_required_tables(self) -> None:
        from src.aca.core.coordination.schema import SQLITE_SCHEMA

        for table in ("tasks", "runs", "workers", "leases", "outbox", "scheduler_events"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", SQLITE_SCHEMA, table)

    def test_postgres_schema_contains_required_tables(self) -> None:
        from src.aca.core.coordination.schema import POSTGRES_SCHEMA

        for table in ("tasks", "runs", "workers", "leases", "outbox", "scheduler_events"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", POSTGRES_SCHEMA, table)

    def test_apply_sqlite_schema_creates_tables(self) -> None:
        from src.aca.core.coordination.schema import apply_sqlite_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_sqlite_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for expected in ("tasks", "runs", "workers", "leases", "outbox", "scheduler_events"):
            self.assertIn(expected, tables, expected)
        conn.close()

    def test_apply_sqlite_schema_idempotent(self) -> None:
        """Calling apply_sqlite_schema twice must not raise."""
        from src.aca.core.coordination.schema import apply_sqlite_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_sqlite_schema(conn)
        apply_sqlite_schema(conn)  # second call must be safe
        conn.close()


class RowHelpersTest(unittest.TestCase):
    """row_to_* standalone serialization functions."""

    def _make_row(self, **kwargs: object) -> sqlite3.Row:
        """Build a sqlite3.Row from keyword args via an in-memory query."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" * len(kwargs))
        cols = ", ".join(kwargs.keys())
        row = conn.execute(
            f"SELECT {placeholders} AS {', '.join(f'? AS {k}' for k in kwargs.keys())}",
        )
        # Simpler: use a real table
        conn.execute(f"CREATE TABLE t ({cols})")
        conn.execute(f"INSERT INTO t VALUES ({placeholders})", list(kwargs.values()))
        result = conn.execute("SELECT * FROM t").fetchone()
        conn.close()
        return result

    def test_row_to_task_none(self) -> None:
        from src.aca.core.coordination.rows import row_to_task

        self.assertEqual(row_to_task(None), {})

    def test_row_to_run_none(self) -> None:
        from src.aca.core.coordination.rows import row_to_run

        self.assertEqual(row_to_run(None), {})

    def test_row_to_worker_none(self) -> None:
        from src.aca.core.coordination.rows import row_to_worker

        self.assertEqual(row_to_worker(None), {})

    def test_row_to_lease_none(self) -> None:
        from src.aca.core.coordination.rows import row_to_lease

        self.assertEqual(row_to_lease(None), {})

    def test_row_to_outbox_none(self) -> None:
        from src.aca.core.coordination.rows import row_to_outbox

        self.assertEqual(row_to_outbox(None), {})

    def test_row_to_scheduler_event_none(self) -> None:
        from src.aca.core.coordination.rows import row_to_scheduler_event

        self.assertEqual(row_to_scheduler_event(None), {})

    def test_row_to_task_real_row(self) -> None:
        """row_to_task returns expected keys from a real sqlite3.Row."""
        from src.aca.core.coordination.schema import apply_sqlite_schema
        from src.aca.core.coordination.rows import row_to_task

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_sqlite_schema(conn)
        conn.execute(
            """INSERT INTO tasks VALUES (
                'k1', 'tid1', 'kanban_board', 'ref1', 'My Task',
                'org/repo', '/path', '/board.yaml',
                'queued', 'queued', NULL, NULL, NULL, NULL, NULL,
                1000, 1001, '{"task":{},"repo":{}}'
            )"""
        )
        row = conn.execute("SELECT * FROM tasks WHERE task_key='k1'").fetchone()
        result = row_to_task(row)
        conn.close()

        self.assertEqual(result["task_key"], "k1")
        self.assertEqual(result["title"], "My Task")
        self.assertEqual(result["state"], "queued")
        self.assertIn("metadata", result)

    def test_row_to_outbox_real_row(self) -> None:
        """row_to_outbox parses payload_json correctly."""
        from src.aca.core.coordination.schema import apply_sqlite_schema
        from src.aca.core.coordination.rows import row_to_outbox

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_sqlite_schema(conn)
        payload = json.dumps({"run_id": "r1"})
        conn.execute(
            """INSERT INTO outbox
               (kind, aggregate_type, aggregate_id, payload_json, status,
                attempts, next_attempt_at_ms, last_error,
                created_at_ms, updated_at_ms, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("github_pull_request.create", "task", "t1", payload,
             "pending", 0, 2000, None, 1000, 1001, "dedup-1"),
        )
        row = conn.execute("SELECT * FROM outbox").fetchone()
        result = row_to_outbox(row)
        conn.close()

        self.assertEqual(result["kind"], "github_pull_request.create")
        self.assertEqual(result["payload"]["run_id"], "r1")
        self.assertEqual(result["dedupe_key"], "dedup-1")


if __name__ == "__main__":
    unittest.main()
