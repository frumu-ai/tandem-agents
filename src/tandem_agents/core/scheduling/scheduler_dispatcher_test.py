from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.scheduling.scheduler_dispatcher import _task_source_overrides, dispatch_scheduled_runs
from src.tandem_agents.runtime.workspace_registry import load_workspace


class SchedulerDispatcherTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_TASK_SOURCE_TYPE=manual",
                    "ACA_TASK_SOURCE_PROMPT=Do the thing",
                    "ACA_REPO_SLUG=frumu-ai/example",
                    "ACA_PROVIDER=openai",
                    "ACA_MODEL=gpt-4.1-mini",
                    "ACA_SCHEDULER_MAX_ACTIVE_TASKS=2",
                    "ACA_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT=1",
                    "ACA_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO=1",
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

    def test_task_source_overrides_include_linear_routing_fields(self) -> None:
        task = {
            "task_id": "TAN-170",
            "source": {
                "type": "linear",
                "team": "team-1",
                "project": "project-1",
                "statuses": "Backlog,Ready,In Progress",
                "labels": "Runtime Security",
                "query": "TAN-170",
                "item": "TAN-170",
            },
            "repo": {
                "slug": "frumu-ai/tandem-agents",
                "path": "/workspace/repos/tandem-agents",
                "clone_url": "https://github.com/frumu-ai/tandem-agents.git",
            },
        }

        overrides = _task_source_overrides(task)

        self.assertEqual(overrides["ACA_TASK_SOURCE_TYPE"], "linear")
        self.assertEqual(overrides["ACA_TASK_SOURCE_TEAM"], "team-1")
        self.assertEqual(overrides["ACA_TASK_SOURCE_PROJECT"], "project-1")
        self.assertEqual(overrides["ACA_TASK_SOURCE_STATUSES"], "Backlog,Ready,In Progress")
        self.assertEqual(overrides["ACA_TASK_SOURCE_LABELS"], "Runtime Security")
        self.assertEqual(overrides["ACA_TASK_SOURCE_QUERY"], "TAN-170")
        self.assertEqual(overrides["ACA_TASK_SOURCE_ITEM"], "TAN-170")
        self.assertEqual(overrides["ACA_REPO_SLUG"], "frumu-ai/tandem-agents")
        self.assertEqual(overrides["ACA_REPO_PATH"], "/workspace/repos/tandem-agents")
        self.assertEqual(overrides["ACA_REPO_URL"], "https://github.com/frumu-ai/tandem-agents.git")

    def test_dispatcher_launches_multiple_admitted_runs_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            tasks = [
                {
                    "task_id": "task-a",
                    "title": "Task A",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-a"},
                    "repo": {"slug": "frumu-ai/project-a", "path": str(root / "repo-a")},
                },
                {
                    "task_id": "task-b",
                    "title": "Task B",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-b"},
                    "repo": {"slug": "frumu-ai/project-b", "path": str(root / "repo-b")},
                },
            ]
            for task in tasks:
                store.register_task(task, repo=task["repo"], status="queued")

            started: list[str] = []
            finished: list[str] = []
            gate = threading.Event()
            two_started = threading.Event()
            lock = threading.Lock()

            def fake_run_worker(run_cfg):  # noqa: ANN001
                run_id = str(run_cfg.env.get("ACA_RUN_ID") or "")
                self.assertEqual(run_cfg.env.get("ACA_COORDINATION_ROLE"), "worker")
                self.assertEqual(run_cfg.env.get("ACA_RUNTIME_ROLE"), "worker")
                with lock:
                    started.append(run_id)
                    if len(started) >= 2:
                        two_started.set()
                gate.wait(timeout=5)
                with lock:
                    finished.append(run_id)
                return {"run_id": run_id, "status": {"run": {"status": "completed"}}}

            result_holder: list[dict[str, object]] = []

            def _dispatch() -> None:
                result_holder.append(dispatch_scheduled_runs(cfg, coordination=store, wait=True))

            with patch("src.tandem_agents.core.scheduling.scheduler_dispatcher.run_worker", side_effect=fake_run_worker):
                thread = threading.Thread(target=_dispatch)
                thread.start()
                self.assertTrue(two_started.wait(timeout=5))
                gate.set()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(started), 2)
            self.assertEqual(len(finished), 2)
            self.assertEqual(len(result_holder), 1)
            result = result_holder[0]
            self.assertEqual(len(result["started"]), 2)
            self.assertEqual(len(result["completed"]), 2)
            self.assertEqual(len(result["errors"]), 0)
            for item in result["started"]:
                self.assertIn(item["execution_backend"], {"legacy", "coder", "auto"})
            for item in result["completed"]:
                self.assertIn(item["execution_backend"], {"legacy", "coder", "auto"})
            self.assertGreaterEqual(store.snapshot()["summary"]["scheduler_events"], 1)
            workspace = load_workspace(root)
            self.assertEqual(len(workspace["workspace"]["runs"]), 2)
            for run in workspace["workspace"]["runs"]:
                self.assertEqual(run["status"], "completed")
                self.assertIn(run["execution_backend"], {"legacy", "coder", "auto"})
                self.assertEqual(run["admission_role"], "aca_scheduler")
                self.assertIn(run["execution_path"], {"tandem_coder", "aca_admission_only"})
                self.assertTrue(run["project_id"])


if __name__ == "__main__":
    unittest.main()
