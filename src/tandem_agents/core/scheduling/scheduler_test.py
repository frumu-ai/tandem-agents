from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.scheduling.scheduler import (
    plan_task_admissions,
    scheduler_integration_blockers,
    scheduler_snapshot,
    task_project_key,
    task_repo_key,
)
from src.tandem_agents.runtime.runstate import initial_blackboard, initial_status, save_blackboard, write_status


class SchedulerTest(unittest.TestCase):
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

    def test_scheduler_admits_across_projects_and_serializes_repo_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 4
            cfg.scheduler.max_active_tasks_per_project = 1
            cfg.scheduler.max_active_tasks_per_repo = 1

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
                {
                    "task_id": "task-c",
                    "title": "Task C",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-c"},
                    "repo": {"slug": "frumu-ai/project-b", "path": str(root / "repo-b")},
                },
                {
                    "task_id": "task-d",
                    "title": "Task D",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-d"},
                    "repo": {"slug": "frumu-ai/project-d", "path": str(root / "repo-d")},
                },
            ]
            for task in tasks:
                store.register_task(task, repo=task["repo"], status="queued")

            snapshot = scheduler_snapshot(cfg, coordination=store, limit=10)
            plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertEqual(snapshot["queued_tasks"], 4)
            self.assertEqual(plan["policy"], "fair_round_robin")
            self.assertEqual(len(plan["admitted"]), 3)
            admitted_projects = {item["project_key"] for item in plan["admitted"]}
            self.assertEqual(
                admitted_projects,
                {
                    task_project_key(tasks[0]),
                    task_project_key(tasks[1]),
                    task_project_key(tasks[3]),
                },
            )
            for item in plan["admitted"]:
                self.assertIn(item["execution_backend"], {"legacy", "coder"})
            blocked_reasons = {item["reason"] for item in plan["blocked"]}
            self.assertIn("repo_capacity_reached", blocked_reasons)
            self.assertGreaterEqual(store.snapshot()["summary"]["scheduler_events"], 1)
            self.assertEqual(task_repo_key(tasks[1]), task_repo_key(tasks[2]))

    def test_scheduler_allows_disjoint_file_scopes_and_blocks_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 4
            cfg.scheduler.max_active_tasks_per_project = 1
            cfg.scheduler.max_active_tasks_per_repo = 4

            store = CoordinationStore.from_config(cfg)

            tasks = [
                {
                    "task_id": "task-a",
                    "title": "Task A",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-a"},
                    "repo": {"slug": "frumu-ai/shared-repo", "path": str(root / "repo")},
                    "files": ["src/app.py"],
                },
                {
                    "task_id": "task-b",
                    "title": "Task B",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-b"},
                    "repo": {"slug": "frumu-ai/shared-repo", "path": str(root / "repo")},
                    "files": ["docs/notes.md"],
                },
                {
                    "task_id": "task-c",
                    "title": "Task C",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-c"},
                    "repo": {"slug": "frumu-ai/shared-repo", "path": str(root / "repo")},
                    "files": ["src/app.py"],
                },
                {
                    "task_id": "task-d",
                    "title": "Task D",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-d"},
                    "repo": {"slug": "frumu-ai/shared-repo", "path": str(root / "repo")},
                    "files": ["tests/test_app.py"],
                },
            ]
            registered = []
            for task in tasks:
                registered.append(store.register_task(task, repo=task["repo"], status="queued"))

            plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertEqual(len(plan["admitted"]), 3)
            admitted_task_ids = {item["task_key"] for item in plan["admitted"]}
            self.assertNotIn(registered[2]["task_key"], admitted_task_ids)
            blocked_reasons = {item["reason"] for item in plan["blocked"]}
            self.assertIn("file_overlap_reached", blocked_reasons)
            self.assertTrue(any(item.get("scope_mode") == "files" for item in plan["admitted"]))

    def test_scheduler_blocks_duplicate_admission_when_coder_run_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            task = {
                "task_id": "task-a",
                "title": "Task A",
                "source": {"type": "manual", "prompt": "Do the thing", "source_name": "project-a"},
                "repo": {"slug": "frumu-ai/project-a", "path": str(root / "repo-a")},
            }
            registered = store.register_task(task, repo=task["repo"], status="queued")
            run_dir = cfg.output_root() / "run-active-coder"
            run_dir.mkdir(parents=True, exist_ok=True)
            status = initial_status(
                "run-active-coder",
                {**task, "task_key": registered["task_key"]},
                task["repo"],
                {"version": "engine"},
                {"id": "openai", "model": "gpt-4.1-mini"},
                {},
                run_dir,
            )
            status["run"]["status"] = "running"
            status["phase"] = {"name": "coder_execution", "detail": None, "role": "worker", "updated_at_ms": 1}
            status["coordination"] = {"task_key": registered["task_key"]}
            write_status(run_dir / "status.json", status)
            blackboard = initial_blackboard("run-active-coder", task, task["repo"], {}, {}, {})
            blackboard["execution_backend"] = "coder"
            blackboard["coder_run"] = {"coder_run_id": "run-active-coder", "status": "running"}
            save_blackboard(run_dir / "blackboard.yaml", blackboard)

            plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertFalse(plan["admitted"])
            self.assertEqual(plan["blocked"][0]["reason"], "coder_run_active")

    def test_scheduler_increases_parallel_admission_without_breaking_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 6
            cfg.scheduler.max_concurrent_worker_runs = 99
            cfg.scheduler.max_active_tasks_per_project = 1
            cfg.scheduler.max_active_tasks_per_repo = 1

            store = CoordinationStore.from_config(cfg)
            tasks = [
                {
                    "task_id": f"task-{suffix}",
                    "title": f"Task {suffix}",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": f"project-{suffix}"},
                    "repo": {"slug": f"frumu-ai/project-{suffix}", "path": str(root / f"repo-{suffix}")},
                    "files": [f"src/file-{suffix}.py"],
                }
                for suffix in ("a", "b", "c", "d", "e", "f")
            ]
            for task in tasks:
                store.register_task(task, repo=task["repo"], status="queued")

            plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertEqual(len(plan["admitted"]), 6)
            self.assertFalse(plan["blocked"])
            self.assertTrue(all(item["scope_mode"] == "files" for item in plan["admitted"]))
            self.assertEqual({item["repo_key"] for item in plan["admitted"]}, {task["repo"]["slug"] for task in tasks})

    def test_scheduler_worker_concurrency_cap_blocks_remaining_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 6
            cfg.scheduler.max_concurrent_worker_runs = 2
            cfg.scheduler.max_active_tasks_per_project = 6
            cfg.scheduler.max_active_tasks_per_repo = 6

            store = CoordinationStore.from_config(cfg)
            tasks = [
                {
                    "task_id": f"task-{suffix}",
                    "title": f"Task {suffix}",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": f"project-{suffix}"},
                    "repo": {"slug": f"frumu-ai/project-{suffix}", "path": str(root / f"repo-{suffix}")},
                    "files": [f"src/file-{suffix}.py"],
                }
                for suffix in ("a", "b", "c")
            ]
            for task in tasks:
                store.register_task(task, repo=task["repo"], status="queued")

            plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertEqual(len(plan["admitted"]), 2)
            self.assertEqual(plan["limits"]["max_concurrent_worker_runs"], 2)
            blocked_reasons = {item["reason"] for item in plan["blocked"]}
            self.assertIn("worker_concurrency_reached", blocked_reasons)

    def test_scheduler_worker_concurrency_cap_counts_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 10
            cfg.scheduler.max_concurrent_worker_runs = 4
            cfg.scheduler.max_active_tasks_per_project = 10
            cfg.scheduler.max_active_tasks_per_repo = 10

            store = CoordinationStore.from_config(cfg)
            for suffix in ("a", "b", "c", "d"):
                active = {
                    "task_id": f"active-{suffix}",
                    "title": f"Active {suffix}",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": f"project-{suffix}"},
                    "repo": {"slug": f"frumu-ai/active-{suffix}", "path": str(root / f"active-{suffix}")},
                    "files": [f"src/active-{suffix}.py"],
                }
                store.register_task(active, repo=active["repo"], status="active")
            for suffix in ("e", "f", "g"):
                queued = {
                    "task_id": f"queued-{suffix}",
                    "title": f"Queued {suffix}",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": f"project-{suffix}"},
                    "repo": {"slug": f"frumu-ai/queued-{suffix}", "path": str(root / f"queued-{suffix}")},
                    "files": [f"src/queued-{suffix}.py"],
                }
                store.register_task(queued, repo=queued["repo"], status="queued")

            plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertFalse(plan["admitted"])
            self.assertEqual(plan["limits"]["remaining_worker_slots"], 0)
            self.assertEqual({item["reason"] for item in plan["blocked"]}, {"worker_concurrency_reached"})

    def test_scheduler_scans_active_coder_runs_once_per_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 4
            store = CoordinationStore.from_config(cfg)
            for suffix in ("a", "b", "c"):
                task = {
                    "task_id": f"task-{suffix}",
                    "title": f"Task {suffix}",
                    "source": {"type": "manual", "prompt": "Do the thing", "source_name": f"project-{suffix}"},
                    "repo": {"slug": f"frumu-ai/project-{suffix}", "path": str(root / f"repo-{suffix}")},
                }
                store.register_task(task, repo=task["repo"], status="queued")

            with patch(
                "src.tandem_agents.core.scheduling.scheduler.list_active_coder_task_refs",
                return_value=[],
            ) as active_runs:
                plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertEqual(len(plan["admitted"]), 3)
            self.assertEqual(active_runs.call_count, 1)

    def test_scheduler_filters_to_requested_project_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            target = {
                "task_id": "target",
                "title": "Target task",
                "source": {"type": "linear", "team": "team-1", "project": "project-target"},
                "repo": {"slug": "frumu-ai/target", "path": str(root / "target")},
            }
            stale = {
                "task_id": "stale",
                "title": "Stale task",
                "source": {"type": "linear", "team": "team-1", "project": "project-stale"},
                "repo": {"slug": "frumu-ai/stale", "path": str(root / "stale")},
            }
            store.register_task(stale, repo=stale["repo"], status="queued")
            store.register_task(target, repo=target["repo"], status="queued")

            project_key = task_project_key(target)
            snapshot = scheduler_snapshot(cfg, coordination=store, limit=10, project_keys={project_key})
            plan = plan_task_admissions(cfg, coordination=store, limit=10, project_keys={project_key})

            self.assertEqual(snapshot["queued_tasks"], 1)
            self.assertEqual(snapshot["queued"][0]["task_id"], "target")
            self.assertEqual(len(plan["admitted"]), 1)
            self.assertEqual(plan["admitted"][0]["task_key"], snapshot["queued"][0]["task_key"])
            self.assertFalse(plan["blocked"])

    def test_scheduler_blocks_linear_admission_when_mcp_auth_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.linear_mcp.enabled = True
            store = CoordinationStore.from_config(cfg)
            task = {
                "task_id": "linear-task",
                "title": "Linear task",
                "source": {"type": "linear", "team": "team-1", "project": "project-target"},
                "repo": {"slug": "frumu-ai/target", "path": str(root / "target")},
            }
            registered = store.register_task(task, repo=task["repo"], status="queued")

            with patch(
                "src.tandem_agents.core.scheduling.scheduler.get_mcp_server",
                return_value={
                    "name": "linear",
                    "auth_kind": "oauth",
                    "connected": False,
                    "last_auth_challenge": {
                        "authorization_url": "https://linear.example.test/authorize"
                    },
                    "last_error": "Authorization required.",
                },
            ):
                plan = plan_task_admissions(cfg, coordination=store, limit=10)

            self.assertFalse(plan["admitted"])
            self.assertEqual(plan["blocked"][0]["task_key"], registered["task_key"])
            self.assertEqual(plan["blocked"][0]["reason"], "linear_mcp_auth_required")
            self.assertEqual(
                plan["blocked"][0]["authorization_url"],
                "https://linear.example.test/authorize",
            )

    def test_scheduler_reports_linear_integration_blocker_without_queued_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.task_source.type = "linear"
            cfg.task_source.team = "team-1"
            cfg.task_source.project = "project-target"
            cfg.linear_mcp.enabled = True

            with patch(
                "src.tandem_agents.core.scheduling.scheduler.get_mcp_server",
                return_value={
                    "name": "linear",
                    "auth_kind": "oauth",
                    "connected": False,
                    "last_auth_challenge": {
                        "authorization_url": "https://linear.example.test/authorize"
                    },
                    "last_error": "Authorization required.",
                },
            ):
                blockers = scheduler_integration_blockers(cfg)

            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["reason"], "linear_mcp_auth_required")
            self.assertEqual(blockers[0]["project_key"], "linear:team-1/project-target")

    def test_scheduler_requests_linear_auth_url_when_server_list_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.task_source.type = "linear"
            cfg.task_source.team = "team-1"
            cfg.task_source.project = "project-target"
            cfg.linear_mcp.enabled = True

            with patch(
                "src.tandem_agents.core.scheduling.scheduler.get_mcp_server",
                return_value={
                    "name": "linear",
                    "auth_kind": "oauth",
                    "connected": False,
                    "last_error": (
                        'MCP endpoint returned HTTP 401: {"error":"invalid_token",'
                        '"error_description":"Missing or invalid access token"}'
                    ),
                },
            ), patch(
                "src.tandem_agents.core.scheduling.scheduler._request_linear_auth_url",
                return_value="https://linear.example.test/authorize",
            ) as request_auth:
                blockers = scheduler_integration_blockers(cfg)

            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["reason"], "linear_mcp_auth_required")
            self.assertEqual(blockers[0]["authorization_url"], "https://linear.example.test/authorize")
            request_auth.assert_called_once_with(cfg, "linear", refresh=False)

    def test_scheduler_refreshes_stale_linear_auth_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.task_source.type = "linear"
            cfg.task_source.team = "team-1"
            cfg.task_source.project = "project-target"
            cfg.linear_mcp.enabled = True

            with patch(
                "src.tandem_agents.core.scheduling.scheduler.get_mcp_server",
                return_value={
                    "name": "linear",
                    "auth_kind": "oauth",
                    "connected": False,
                    "last_auth_challenge": {
                        "authorization_url": "https://linear.example.test/old-authorize",
                        "requested_at_ms": 1_000,
                    },
                    "last_error": "Authorization required.",
                },
            ), patch(
                "src.tandem_agents.core.scheduling.scheduler.time.time",
                return_value=400.0,
            ), patch(
                "src.tandem_agents.core.scheduling.scheduler._request_linear_auth_url",
                return_value="https://linear.example.test/new-authorize",
            ) as request_auth:
                blockers = scheduler_integration_blockers(cfg)

            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["authorization_url"], "https://linear.example.test/new-authorize")
            request_auth.assert_called_once_with(cfg, "linear", refresh=True)

    def test_scheduler_project_filter_keeps_global_active_repo_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.scheduler.max_active_tasks = 4
            cfg.scheduler.max_active_tasks_per_project = 4
            cfg.scheduler.max_active_tasks_per_repo = 4
            store = CoordinationStore.from_config(cfg)
            active_other_project = {
                "task_id": "active-other",
                "title": "Active other project",
                "source": {"type": "linear", "team": "team-1", "project": "project-other"},
                "repo": {"slug": "frumu-ai/shared", "path": str(root / "shared")},
            }
            target = {
                "task_id": "target",
                "title": "Target task",
                "source": {"type": "linear", "team": "team-1", "project": "project-target"},
                "repo": {"slug": "frumu-ai/shared", "path": str(root / "shared")},
            }
            store.register_task(active_other_project, repo=active_other_project["repo"], status="active")
            registered_target = store.register_task(target, repo=target["repo"], status="queued")

            plan = plan_task_admissions(
                cfg,
                coordination=store,
                limit=10,
                project_keys={task_project_key(target)},
            )

            self.assertFalse(plan["admitted"])
            self.assertEqual(plan["active_tasks"], 0)
            self.assertEqual(plan["blocked"][0]["task_key"], registered_target["task_key"])
            self.assertEqual(plan["blocked"][0]["reason"], "repo_overlap_reached")
            self.assertEqual(plan["blocked"][0]["repo_key"], "frumu-ai/shared")


if __name__ == "__main__":
    unittest.main()
