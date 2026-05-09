from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.tandem_agents.config.config import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore


POSTGRES_URL = os.environ.get("ACA_POSTGRES_TEST_URL") or os.environ.get("ACA_COORDINATION_POSTGRES_URL", "").strip()


@unittest.skipUnless(POSTGRES_URL, "Set ACA_POSTGRES_TEST_URL or ACA_COORDINATION_POSTGRES_URL to run Postgres integration tests")
class CoordinationPostgresIntegrationTest(unittest.TestCase):
    def _config_root(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_STORAGE_PROFILE=shared",
                    "ACA_COORDINATION_BACKEND=postgres",
                    f"ACA_COORDINATION_POSTGRES_URL={POSTGRES_URL}",
                    "ACA_REPO_SLUG=acme/demo",
                    "ACA_PROVIDER=openai",
                    "ACA_MODEL=gpt-4.1-mini",
                    "ACA_TASK_SOURCE_TYPE=manual",
                    "ACA_TASK_SOURCE_PROMPT=coordination integration test",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return root

    def test_postgres_backend_claims_releases_and_drains_outbox(self):
        root = self._config_root()
        cfg = resolve_config(root)
        store = CoordinationStore.from_config(cfg)
        self.assertEqual(store.backend, "postgres")
        store.ensure_schema()

        task = {
            "task_id": "task-1",
            "title": "Postgres integration task",
            "source": {
                "type": "manual",
                "prompt": "coordination integration test",
                "card_id": "task-1",
            },
            "repo": {"slug": "acme/demo", "path": ""},
        }
        registered = store.register_task(task, repo={"slug": "acme/demo", "path": ""})
        self.assertEqual(registered["state"], "queued")

        claim = store.claim_task(
            task,
            run_id="run-1",
            worker_id="worker-1",
            host_id="host-1",
            lease_ttl_seconds=2,
            repo={"slug": "acme/demo", "path": ""},
        )
        self.assertTrue(claim["claimed"])
        lease = claim["lease"]
        self.assertIsNotNone(lease)
        self.assertEqual(lease["status"], "active")

        heartbeat = store.heartbeat_lease(lease["lease_id"], lease_ttl_seconds=2)
        self.assertIsNotNone(heartbeat)
        self.assertGreaterEqual(heartbeat["expires_at_ms"], lease["expires_at_ms"])

        released = store.release_lease(lease["lease_id"], status="completed", reason="done")
        self.assertIsNotNone(released)
        refreshed_task = store.get_task(registered["task_key"])
        self.assertEqual(refreshed_task["state"], "done")

        outbox = store.enqueue_outbox(
            kind="github_project.status",
            aggregate_type="task",
            aggregate_id=registered["task_key"],
            payload={"status": "done"},
            dedupe_key="integration-task-status",
        )
        self.assertEqual(outbox["status"], "pending")

        claimed = store.claim_pending_outbox(limit=1)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["status"], "processing")
        completed = store.complete_outbox(claimed[0]["id"])
        self.assertIsNotNone(completed)

        store.record_scheduler_event("scheduler.plan", {"task_key": registered["task_key"]})
        snapshot = store.snapshot(limit=10)
        self.assertEqual(snapshot["backend"], "postgres")
        self.assertGreaterEqual(snapshot["summary"]["tasks"], 1)
        self.assertGreaterEqual(snapshot["summary"]["scheduler_events"], 1)

    def tearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
