from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.coordination.coordination_reaper import coordination_reaper_tick


class CoordinationStoreTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "tandem-data").mkdir(parents=True, exist_ok=True)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_COORDINATION_SQLITE_PATH=tandem-data/coordination.sqlite3",
                    "ACA_TASK_SOURCE_TYPE=manual",
                    "ACA_TASK_SOURCE_PROMPT=Do the thing",
                    "ACA_REPO_SLUG=frumu-ai/example",
                    "ACA_PROVIDER=openai",
                    "ACA_MODEL=gpt-4.1-mini",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "tandem:",
                    "  base_url: http://127.0.0.1:39733",
                    "task_source:",
                    "  type: manual",
                    "  prompt: Do the thing",
                    "repository:",
                    "  slug: frumu-ai/example",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-4.1-mini",
                    "swarm:",
                    "  enabled: false",
                    "output:",
                    "  root: runs",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def test_claim_release_and_reclaim_after_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            task = {
                "task_id": "task-1",
                "title": "Task One",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task, repo=task["repo"])
            self.assertEqual(store.get_task("manual:manual:do-the-thing:task-1")["state"], "queued")

            first = store.claim_task(
                task,
                run_id="run-1",
                worker_id="worker-1",
                host_id="host-a",
                lease_ttl_seconds=1,
                repo=task["repo"],
            )
            self.assertTrue(first["claimed"])
            self.assertEqual(first["task"]["state"], "claimed")
            lease_id = first["lease"]["lease_id"]
            ownership = store.task_ownership(first["task"]["task_key"])
            self.assertIsNotNone(ownership)
            self.assertEqual(ownership["ownership_state"], "owned")
            self.assertEqual(ownership["worker"]["worker_id"], "worker-1")
            self.assertEqual(ownership["lease"]["lease_id"], lease_id)

            second = store.claim_task(
                task,
                run_id="run-2",
                worker_id="worker-2",
                host_id="host-b",
                lease_ttl_seconds=1,
                repo=task["repo"],
            )
            self.assertFalse(second["claimed"])
            self.assertEqual(second["active_lease"]["lease_id"], lease_id)

            with store.connection() as conn:
                conn.execute("UPDATE leases SET expires_at_ms = 0 WHERE lease_id = ?", (lease_id,))
            expired = store.reap_expired_leases()
            self.assertEqual(len(expired), 1)
            stale_task = store.get_task("manual:manual:do-the-thing:task-1")
            self.assertEqual(stale_task["state"], "stale")
            self.assertIsNone(stale_task["claimed_lease_id"])
            self.assertIsNone(stale_task["claimed_run_id"])
            reclaimable = store.task_ownership(first["task"]["task_key"])
            self.assertIsNotNone(reclaimable)
            self.assertEqual(reclaimable["ownership_state"], "reclaimable")
            self.assertTrue(reclaimable["reclaimable"])

            third = store.claim_task(
                task,
                run_id="run-3",
                worker_id="worker-3",
                host_id="host-c",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            self.assertTrue(third["claimed"])
            self.assertNotEqual(third["lease"]["lease_id"], lease_id)
            self.assertEqual(third["task"]["state"], "claimed")

            released = store.release_lease(third["lease"]["lease_id"], status="completed", reason="done")
            self.assertEqual(released["status"], "completed")
            self.assertEqual(store.get_task("manual:manual:do-the-thing:task-1")["state"], "done")
            final_ownership = store.task_ownership(first["task"]["task_key"])
            self.assertIsNotNone(final_ownership)
            self.assertEqual(final_ownership["ownership_state"], "done")
            self.assertFalse(final_ownership["is_current"])
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["pending_outbox"], 0)

    def test_release_lease_is_idempotent(self) -> None:
        # Once a lease is released, a second release_lease call must not
        # overwrite the original release reason. This guards the runner_core
        # finally block: explicit releases on specific block paths must win
        # over the catch-all release in the wrapper.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            task = {
                "task_id": "task-idem",
                "title": "Idempotent release",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task, repo=task["repo"])
            claim = store.claim_task(
                task,
                run_id="run-1",
                worker_id="worker-1",
                host_id="host-a",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            self.assertTrue(claim["claimed"])
            lease_id = claim["lease"]["lease_id"]

            first = store.release_lease(lease_id, status="blocked", reason="explicit blocker reason")
            self.assertEqual(first["status"], "blocked")
            self.assertEqual(first["release_reason"], "explicit blocker reason")

            second = store.release_lease(lease_id, status="failed", reason="crashed")
            # The second call must NOT mutate the lease state: the explicit
            # reason from the first call should win.
            self.assertEqual(second["status"], "blocked")
            self.assertEqual(second["release_reason"], "explicit blocker reason")

    def test_outbox_deduplicates_by_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            first = store.enqueue_outbox(
                kind="github_project.status_update",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={"status": "In progress"},
                dedupe_key="run-1:claim",
            )
            second = store.enqueue_outbox(
                kind="github_project.status_update",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={"status": "In progress"},
                dedupe_key="run-1:claim",
            )
            self.assertEqual(first["id"], second["id"])
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["pending_outbox"], 1)

    def test_worker_registry_lists_registered_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            created = store.register_worker(
                worker_id="worker-1",
                host_id="host-a",
                role="worker",
                status="idle",
                capabilities={"mode": "worker", "provider": "openai", "model": "gpt-4.1-mini"},
            )
            self.assertEqual(created["worker_id"], "worker-1")

            touched = store.heartbeat_worker(
                "worker-1",
                host_id="host-a",
                role="worker",
                status="busy",
                capabilities={"mode": "worker", "provider": "openai", "model": "gpt-4.1-mini"},
                current_run_id="run-1",
                current_lease_id="lease-1",
            )
            self.assertIsNotNone(touched)
            self.assertEqual(touched["status"], "busy")

            workers = store.list_workers(limit=10)
            self.assertEqual(len(workers), 1)
            self.assertEqual(workers[0]["worker_id"], "worker-1")
            self.assertEqual(workers[0]["host_id"], "host-a")
            self.assertEqual(workers[0]["capabilities"]["mode"], "worker")

    def test_reaper_tick_reclaims_expired_active_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            task = {
                "task_id": "task-2",
                "title": "Task Two",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task, repo=task["repo"])
            claim = store.claim_task(
                task,
                run_id="run-1",
                worker_id="worker-1",
                host_id="host-a",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            self.assertTrue(claim["claimed"])
            lease_id = claim["lease"]["lease_id"]

            with store.connection() as conn:
                conn.execute("UPDATE leases SET expires_at_ms = 0 WHERE lease_id = ?", (lease_id,))

            expired = coordination_reaper_tick(cfg)
            self.assertEqual(len(expired), 1)

            lease = store.get_lease(lease_id)
            self.assertEqual(lease["status"], "stale")
            task_row = store.get_task(claim["task"]["task_key"])
            self.assertEqual(task_row["status"], "stale")
            self.assertIsNone(task_row["claimed_lease_id"])
            self.assertIsNone(task_row["claimed_run_id"])
            worker = store.snapshot()["workers"][0]
            self.assertEqual(worker["status"], "idle")

    def test_reaper_tick_reclaims_stale_worker_before_lease_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            task = {
                "task_id": "task-worker-death",
                "title": "Worker Death",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task, repo=task["repo"])
            claim = store.claim_task(
                task,
                run_id="run-worker-death",
                worker_id="worker-dead",
                host_id="host-a",
                lease_ttl_seconds=3600,
                repo=task["repo"],
            )
            lease_id = claim["lease"]["lease_id"]

            with store.connection() as conn:
                conn.execute("UPDATE workers SET last_seen_at_ms = 0 WHERE worker_id = ?", ("worker-dead",))

            reaped = coordination_reaper_tick(cfg)
            self.assertEqual(len(reaped), 1)
            self.assertEqual(store.get_lease(lease_id)["status"], "stale")
            stale_task = store.get_task(claim["task"]["task_key"])
            self.assertEqual(stale_task["status"], "stale")
            self.assertIsNone(stale_task["claimed_lease_id"])
            self.assertIsNone(stale_task["claimed_run_id"])
            self.assertEqual(store.snapshot()["workers"][0]["status"], "idle")

    def test_reaper_tick_reclaims_all_stale_workers_on_dead_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            task_a = {
                "task_id": "task-host-a",
                "title": "Host Death A",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            task_b = {
                "task_id": "task-host-b",
                "title": "Host Death B",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task_a, repo=task_a["repo"])
            store.register_task(task_b, repo=task_b["repo"])
            claim_a = store.claim_task(
                task_a,
                run_id="run-host-a",
                worker_id="worker-a",
                host_id="host-dead",
                lease_ttl_seconds=3600,
                repo=task_a["repo"],
            )
            claim_b = store.claim_task(
                task_b,
                run_id="run-host-b",
                worker_id="worker-b",
                host_id="host-dead",
                lease_ttl_seconds=3600,
                repo=task_b["repo"],
            )

            with store.connection() as conn:
                conn.execute("UPDATE workers SET last_seen_at_ms = 0 WHERE host_id = ?", ("host-dead",))

            reaped = coordination_reaper_tick(cfg)
            self.assertEqual(len(reaped), 2)
            self.assertEqual(store.get_lease(claim_a["lease"]["lease_id"])["status"], "stale")
            self.assertEqual(store.get_lease(claim_b["lease"]["lease_id"])["status"], "stale")
            task_a_row = store.get_task(claim_a["task"]["task_key"])
            task_b_row = store.get_task(claim_b["task"]["task_key"])
            self.assertEqual(task_a_row["status"], "stale")
            self.assertEqual(task_b_row["status"], "stale")
            self.assertIsNone(task_a_row["claimed_lease_id"])
            self.assertIsNone(task_b_row["claimed_lease_id"])
            workers = store.snapshot()["workers"]
            self.assertTrue(all(worker["status"] == "idle" for worker in workers))

    def test_reaper_tick_runs_for_postgres_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SimpleNamespace(
                coordination=SimpleNamespace(
                    backend="postgres",
                    heartbeat_interval_seconds=30,
                    lease_ttl_seconds=60,
                )
            )
            store = CoordinationStore(backend="sqlite", db_path=root / "coordination.sqlite3")
            store.ensure_schema()

            task = {
                "task_id": "task-postgres",
                "title": "Task Postgres",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task, repo=task["repo"])
            claim = store.claim_task(
                task,
                run_id="run-postgres",
                worker_id="worker-postgres",
                host_id="host-postgres",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            lease_id = claim["lease"]["lease_id"]

            with store.connection() as conn:
                conn.execute("UPDATE leases SET expires_at_ms = 0 WHERE lease_id = ?", (lease_id,))

            with patch("src.tandem_agents.core.coordination.coordination_reaper.CoordinationStore.from_config", return_value=store):
                expired = coordination_reaper_tick(cfg)

            self.assertEqual(len(expired), 1)
            self.assertEqual(store.get_lease(lease_id)["status"], "stale")
            self.assertEqual(store.get_task(claim["task"]["task_key"])["status"], "stale")

    def test_task_state_machine_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            task = {
                "task_id": "task-3",
                "title": "Task Three",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            registered = store.register_task(task, repo=task["repo"])
            task_key = registered["task_key"]

            claim = store.claim_task(
                task,
                run_id="run-1",
                worker_id="worker-1",
                host_id="host-a",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            lease = claim["lease"]
            self.assertEqual(store.get_task(task_key)["state"], "claimed")

            store.mark_task_active(
                task_key,
                run_id="run-1",
                lease_id=lease["lease_id"],
                worker_id="worker-1",
                host_id="host-a",
                lease_expires_at_ms=lease["expires_at_ms"],
                reason="execution started",
            )
            self.assertEqual(store.get_task(task_key)["state"], "active")

            store.mark_task_review(
                task_key,
                run_id="run-1",
                lease_id=lease["lease_id"],
                worker_id="worker-1",
                host_id="host-a",
                lease_expires_at_ms=lease["expires_at_ms"],
                reason="ready for review",
            )
            self.assertEqual(store.get_task(task_key)["state"], "review")

            store.mark_task_done(
                task_key,
                run_id="run-1",
                lease_id=lease["lease_id"],
                worker_id="worker-1",
                host_id="host-a",
                reason="task completed",
            )
            self.assertEqual(store.get_task(task_key)["state"], "done")

    def test_transition_task_state_can_clear_stale_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            task = {
                "task_id": "task-reset",
                "title": "Task Reset",
                "source": {"type": "manual", "prompt": "Reset the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            registered = store.register_task(task, repo=task["repo"])
            task_key = registered["task_key"]
            claim = store.claim_task(
                task,
                run_id="run-1",
                worker_id="worker-1",
                host_id="host-a",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            lease = claim["lease"]
            store.mark_task_active(
                task_key,
                run_id="run-1",
                lease_id=lease["lease_id"],
                worker_id="worker-1",
                host_id="host-a",
                lease_expires_at_ms=lease["expires_at_ms"],
            )

            reset = store.transition_task_state(task_key, "queued", status="queued", clear_claim=True)

            self.assertEqual(reset["state"], "queued")
            self.assertIsNone(reset["claimed_run_id"])
            self.assertIsNone(reset["claimed_lease_id"])
            self.assertIsNone(reset["lease_expires_at_ms"])
            released_lease = store.get_lease(lease["lease_id"])
            self.assertEqual((released_lease or {}).get("status"), "stale")
            self.assertEqual((released_lease or {}).get("release_reason"), "operator cleared task claim")

    def test_register_task_clears_claim_pointing_to_non_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            task = {
                "task_id": "task-stale-registration",
                "title": "Task Stale Registration",
                "source": {"type": "manual", "prompt": "Reset stale registration"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            claim = store.claim_task(
                task,
                run_id="run-stale-registration",
                worker_id="worker-stale-registration",
                host_id="host-a",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            task_key = claim["task"]["task_key"]
            lease_id = claim["lease"]["lease_id"]
            with store.connection() as conn:
                conn.execute(
                    "UPDATE leases SET status = 'stale', released_at_ms = ?, release_reason = 'worker stale' WHERE lease_id = ?",
                    (1, lease_id),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'stale', state = 'stale', lease_expires_at_ms = NULL WHERE task_key = ?",
                    (task_key,),
                )

            registered = store.register_task(task, repo=task["repo"], status="queued")

            self.assertEqual(registered["state"], "queued")
            self.assertIsNone(registered["claimed_lease_id"])
            self.assertIsNone(registered["claimed_run_id"])
            next_claim = store.claim_task(
                task,
                run_id="run-next",
                worker_id="worker-next",
                host_id="host-a",
                lease_ttl_seconds=60,
                repo=task["repo"],
            )
            self.assertTrue(next_claim["claimed"])
            self.assertNotEqual(next_claim["lease"]["lease_id"], lease_id)

    def test_postgres_backend_uses_postgres_connection_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Do the thing",
                        "repository:",
                        "  slug: frumu-ai/example",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "storage:",
                        "  profile: shared",
                        "  postgres_url: postgres://localhost/aca",
                        "coordination:",
                        "  backend: postgres",
                        "swarm:",
                        "  enabled: false",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            recorded: list[str] = []

            class FakeCursor:
                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            class FakeConnection:
                autocommit = True
                row_factory = None

                def cursor(self):
                    return self

                def execute(self, sql, params=None):  # noqa: ANN001
                    recorded.append(str(sql))
                    return FakeCursor()

                def executescript(self, script):  # noqa: ANN001
                    recorded.append(str(script))

                def commit(self):
                    return None

                def rollback(self):
                    return None

                def close(self):
                    return None

            with patch("src.tandem_agents.core.coordination.coordination._connect_postgres", return_value=FakeConnection()) as connect_mock:
                store = CoordinationStore.from_config(cfg)
                self.assertEqual(store.backend, "postgres")
                store.ensure_schema()

            connect_mock.assert_called_once_with("postgres://localhost/aca")
            self.assertTrue(recorded)
            self.assertTrue(any("CREATE TABLE IF NOT EXISTS tasks" in sql for sql in recorded))


if __name__ == "__main__":
    unittest.main()
