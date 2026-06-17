from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.phases.worker_dispatch import (
    _cancel_active_worker_engine_session,
    _changed_python_test_modules,
    _changed_python_tests_result,
    _diff_add_delete_counts,
    _diff_has_substantive_required_test_addition,
    _diff_is_destructive_rewrite,
    _failed_result_has_reviewable_source_and_test_diff,
    _reviewable_failed_diff_rejection,
    _subtask_is_no_change_guard_candidate,
    _subtask_is_repair_no_change_guard_candidate,
    _tool_loop_summary_from_messages,
    _worker_no_change_abort_seconds,
    _worker_repair_no_change_abort_seconds,
    dispatch_workers,
)
from src.tandem_agents.runtime.runstate import ensure_layout, initial_status


class _FakeCoordination:
    def heartbeat_lease(self, *_args, **_kwargs):
        return {}

    def register_worker(self, *_args, **_kwargs) -> None:
        return None

    def heartbeat_worker(self, *_args, **_kwargs) -> None:
        return None

    def update_run(self, *_args, **_kwargs) -> None:
        return None


class WorkerDispatchTest(unittest.TestCase):
    def test_no_change_timeout_defaults_and_ignores_invalid_env(self) -> None:
        self.assertEqual(_worker_no_change_abort_seconds(SimpleNamespace(cfg=SimpleNamespace(env={}))), 180.0)
        self.assertEqual(
            _worker_no_change_abort_seconds(
                SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "90"}))
            ),
            90.0,
        )
        self.assertEqual(
            _worker_no_change_abort_seconds(
                SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "bad"}))
            ),
            180.0,
        )

    def test_tool_loop_summary_detects_invalid_patch_churn(self) -> None:
        messages = [
            {
                "parts": [
                    {
                        "type": "tool",
                        "tool": "apply_patch",
                        "args": {
                            "patchText": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-noop\n+noop\n*** End Patch"
                        },
                        "result": "error: No valid patches in input (allow with \"--allow-empty\")",
                    },
                    {
                        "type": "tool",
                        "tool": "apply_patch",
                        "args": {
                            "patchText": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-noop\n+noop\n*** End Patch"
                        },
                        "result": "error: No valid patches in input (allow with \"--allow-empty\")",
                    },
                    {
                        "type": "tool",
                        "tool": "apply_patch",
                        "args": {
                            "patchText": "*** Begin Patch\n*** Update File: src/app_test.py\n@@\n-noop\n+noop\n*** End Patch"
                        },
                        "result": "error: No valid patches in input (allow with \"--allow-empty\")",
                    },
                ]
            }
        ]

        summary = _tool_loop_summary_from_messages(messages)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["invalid_patch_count"], 3)
        self.assertEqual(summary["paths"], ["src/app.py", "src/app_test.py"])

    def test_repair_no_change_timeout_defaults_and_ignores_invalid_env(self) -> None:
        self.assertEqual(_worker_repair_no_change_abort_seconds(SimpleNamespace(cfg=SimpleNamespace(env={}))), 180.0)
        self.assertEqual(
            _worker_repair_no_change_abort_seconds(
                SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS": "45"}))
            ),
            45.0,
        )
        self.assertEqual(
            _worker_repair_no_change_abort_seconds(
                SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS": "nope"}))
            ),
            180.0,
        )

    def test_repair_no_change_guard_only_targets_write_required_repairs(self) -> None:
        self.assertTrue(
            _subtask_is_repair_no_change_guard_candidate(
                {"write_required": True, "deterministic_partial_diff_repair": True}
            )
        )
        self.assertFalse(
            _subtask_is_repair_no_change_guard_candidate(
                {"write_required": False, "deterministic_partial_diff_repair": True}
            )
        )
        self.assertFalse(
            _subtask_is_repair_no_change_guard_candidate(
                {"write_required": True, "title": "Normal worker"}
            )
        )

    def test_no_change_guard_targets_normal_write_required_workers(self) -> None:
        self.assertTrue(_subtask_is_no_change_guard_candidate({"write_required": True}))
        self.assertFalse(_subtask_is_no_change_guard_candidate({"write_required": False}))
        self.assertFalse(
            _subtask_is_no_change_guard_candidate(
                {"write_required": True, "deterministic_partial_diff_repair": True}
            )
        )

    def test_changed_python_test_modules_targets_changed_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_path = root / "src" / "tandem_agents" / "config" / "config_loader_test.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text("import unittest\n", encoding="utf-8")
            (test_path.parent / "config_loader.py").write_text("# source\n", encoding="utf-8")

            self.assertEqual(
                _changed_python_test_modules(
                    root,
                    [
                        "src/tandem_agents/config/config_loader.py",
                        "src/tandem_agents/config/config_loader_test.py",
                    ],
                ),
                ["src.tandem_agents.config.config_loader_test"],
            )

    def test_changed_python_tests_result_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            for init_path in [
                root / "src" / "__init__.py",
                root / "src" / "tandem_agents" / "__init__.py",
                package / "__init__.py",
            ]:
                init_path.write_text("", encoding="utf-8")
            test_path = package / "config_loader_test.py"
            test_path.write_text(
                "import unittest\n\n"
                "class ConfigLoaderTest(unittest.TestCase):\n"
                "    def test_fails(self):\n"
                "        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )

            result = _changed_python_tests_result(root, ["src/tandem_agents/config/config_loader_test.py"])

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 1)
        self.assertIn("src.tandem_agents.config.config_loader_test", " ".join(result["command"]))
        self.assertIn("FAILED", result["output"])

    def test_substantive_required_test_addition_rejects_import_only_diff(self) -> None:
        import_only = "\n".join(
            [
                "diff --git a/src/tandem_agents/config/config_loader_test.py b/src/tandem_agents/config/config_loader_test.py",
                "--- a/src/tandem_agents/config/config_loader_test.py",
                "+++ b/src/tandem_agents/config/config_loader_test.py",
                "+from unittest.mock import patch",
            ]
        )
        assertion_diff = "\n".join(
            [
                "diff --git a/src/tandem_agents/config/config_loader_test.py b/src/tandem_agents/config/config_loader_test.py",
                "--- a/src/tandem_agents/config/config_loader_test.py",
                "+++ b/src/tandem_agents/config/config_loader_test.py",
                "+    def test_loads_scheduler_caps(self):",
                "+        self.assertEqual(config.scheduler.max_concurrent_worker_runs, 2)",
            ]
        )

        required = ["src/tandem_agents/config/config_loader_test.py"]

        self.assertFalse(_diff_has_substantive_required_test_addition(import_only, required))
        self.assertTrue(_diff_has_substantive_required_test_addition(assertion_diff, required))

    def test_reviewable_failed_diff_rejection_runs_changed_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            for init_path in [
                root / "src" / "__init__.py",
                root / "src" / "tandem_agents" / "__init__.py",
                package / "__init__.py",
            ]:
                init_path.write_text("", encoding="utf-8")
            (package / "config_loader.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "config_loader_test.py").write_text("import unittest\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            (package / "config_loader.py").write_text("VALUE = 2\n", encoding="utf-8")
            (package / "config_loader_test.py").write_text(
                "import unittest\n\n"
                "class ConfigLoaderTest(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )
            subtask = {
                "files": [
                    "src/tandem_agents/config/config_loader.py",
                    "src/tandem_agents/config/config_loader_test.py",
                ],
                "acceptance_criteria": ["Tests cover the config loader regression."],
            }

            rejection = _reviewable_failed_diff_rejection(
                root,
                subtask,
                [
                    "src/tandem_agents/config/config_loader.py",
                    "src/tandem_agents/config/config_loader_test.py",
                ],
            )

        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection["reason"], "focused_tests_failed")
        self.assertIn("FAILED", rejection["message"])

    def test_cancel_active_worker_engine_session_deletes_marked_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            layout = ensure_layout(run_dir)
            marker = run_dir / "active_worker_engine_sessions.json"
            marker.write_text(
                json.dumps(
                    {
                        "worker-1": {
                            "session_id": "session-1",
                            "run_id": "run-1",
                            "log_path": str(run_dir / "logs" / "worker-1.log"),
                        }
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}),
                run_id="aca-run-1",
                run_dir=run_dir,
                layout=layout,
                task={"task_id": "TAN-173"},
                repo={"path": "/repo"},
            )

            with mock.patch(
                "src.tandem_agents.core.phases.worker_dispatch.delete_tandem_session"
            ) as delete_session:
                _cancel_active_worker_engine_session(ctx, "worker-1", "worker_no_progress")
                for _ in range(50):
                    if delete_session.call_count and "worker.engine_cancelled" in layout["events"].read_text(
                        encoding="utf-8"
                    ):
                        break
                    time.sleep(0.01)

            delete_session.assert_called_once_with(ctx.cfg, "session-1")
            self.assertFalse(marker.exists())
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["type"] for event in events],
                ["worker.engine_cancel_requested", "worker.engine_cancelled"],
            )

    def test_failed_result_with_source_and_required_test_diff_is_reviewable(self) -> None:
        subtask = {
            "files": [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            "target_files": [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            "acceptance_criteria": ["Tests cover the config loader regression."],
        }
        result = {
            "returncode": 1,
            "partial_diff_artifact": "/runs/run-1/artifacts/worker.patch",
            "changed_files": [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
        }

        self.assertTrue(_failed_result_has_reviewable_source_and_test_diff(result, subtask))
        self.assertFalse(
            _failed_result_has_reviewable_source_and_test_diff(
                {**result, "changed_files": ["src/tandem_agents/config/config_loader_test.py"]},
                subtask,
            )
        )
        self.assertFalse(
            _failed_result_has_reviewable_source_and_test_diff(
                {**result, "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST"},
                subtask,
            )
        )

    def test_destructive_diff_guard_counts_real_diff_lines(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/src/file.py b/src/file.py",
                "--- a/src/file.py",
                "+++ b/src/file.py",
                "+new = True",
                *[f"-old_{index} = True" for index in range(25)],
            ]
        )

        self.assertEqual(_diff_add_delete_counts(diff), (1, 25))
        self.assertTrue(_diff_is_destructive_rewrite(diff, max_deletions=25))
        self.assertFalse(_diff_is_destructive_rewrite(diff, max_deletions=26))

    def test_rejected_failed_diff_records_worker_result_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            repo_path = root / "repo"
            repo_path.mkdir()
            worktree = run_dir / "worktrees" / "worker-1--slice-1"
            package = worktree / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            for init_path in [
                worktree / "src" / "__init__.py",
                worktree / "src" / "tandem_agents" / "__init__.py",
                package / "__init__.py",
            ]:
                init_path.write_text("", encoding="utf-8")
            (package / "config_loader.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "config_loader_test.py").write_text("import unittest\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            (package / "config_loader.py").write_text("VALUE = 2\n", encoding="utf-8")
            (package / "config_loader_test.py").write_text(
                "import unittest\n"
                "from src.tandem_agents.config.config_loader import VALUE\n",
                encoding="utf-8",
            )
            layout = ensure_layout(run_dir)
            subtask = {
                "id": "slice-1",
                "title": "Add exact scheduler config regression",
                "_worker_worktree_name": "worker-1--slice-1",
                "write_required": True,
                "files": [
                    "src/tandem_agents/config/config_loader.py",
                    "src/tandem_agents/config/config_loader_test.py",
                ],
                "acceptance_criteria": ["Tests cover the config loader regression."],
            }
            cfg = SimpleNamespace(
                env={},
                swarm=SimpleNamespace(enabled=False, max_workers=1),
                coordination=SimpleNamespace(lease_ttl_seconds=30, heartbeat_interval_seconds=120),
                repository=SimpleNamespace(slug="frumu-ai/tandem-agents"),
                provider_for_role=lambda _role: ("openai", "gpt-5.5"),
            )
            ctx = SimpleNamespace(
                cfg=cfg,
                run_id="run-1",
                run_dir=run_dir,
                repo_path=repo_path,
                layout=layout,
                task={"task_id": "TAN-173"},
                repo={"path": str(repo_path), "slug": "frumu-ai/tandem-agents"},
                planned_subtasks=[dict(subtask)],
                pending_subtasks=[dict(subtask)],
                worker_results=[],
                blackboard={"subtasks": [dict(subtask)]},
                status=initial_status(
                    "run-1",
                    {"task_id": "TAN-173"},
                    {"path": str(repo_path)},
                    {},
                    {},
                    {},
                    run_dir,
                ),
                coordination=_FakeCoordination(),
                lease_id=None,
                claim_identity={"host_id": "host-1"},
            )

            def fake_execute_pool(*_args, **kwargs):
                kwargs["on_start"]("worker-1", subtask)
                kwargs["on_result"](
                    {
                        "worker_id": "worker-1",
                        "subtask_id": "slice-1",
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_artifact": str(run_dir / "artifacts" / "worker-1.patch"),
                        "changed_files": [
                            "src/tandem_agents/config/config_loader.py",
                            "src/tandem_agents/config/config_loader_test.py",
                        ],
                        "blocker_kind": "engine_prompt_timeout",
                    }
                )
                return []

            with (
                mock.patch(
                    "src.tandem_agents.core.execution.runner_core._execute_local_worker_pool",
                    side_effect=fake_execute_pool,
                ),
                mock.patch("src.tandem_agents.core.phases.worker_dispatch._post_dispatch_validation"),
            ):
                dispatch_workers(ctx)

            self.assertEqual(len(ctx.worker_results), 1)
            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertEqual(ctx.worker_results[0]["blocker_kind"], "engine_prompt_timeout")
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("worker.verifiable_failed_diff_rejected", [event["type"] for event in events])
            self.assertNotIn("worker.verifiable_failed_diff_synced", [event["type"] for event in events])
            self.assertEqual(ctx.status["metrics"]["failed_workers"], 1)

    def test_serial_dispatch_reports_one_spawned_worker_with_queued_slices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            repo_path = root / "repo"
            repo_path.mkdir()
            layout = ensure_layout(run_dir)
            subtasks = [
                {"id": f"subtask-{index}", "title": f"Slice {index}", "write_required": True}
                for index in range(1, 4)
            ]
            cfg = SimpleNamespace(
                env={},
                swarm=SimpleNamespace(enabled=False, max_workers=1),
                coordination=SimpleNamespace(lease_ttl_seconds=30, heartbeat_interval_seconds=120),
                repository=SimpleNamespace(slug="frumu-ai/tandem-agents"),
                provider_for_role=lambda _role: ("openai", "gpt-5.5"),
            )
            ctx = SimpleNamespace(
                cfg=cfg,
                run_id="run-1",
                run_dir=run_dir,
                repo_path=repo_path,
                layout=layout,
                task={"task_id": "TAN-173"},
                repo={"path": str(repo_path), "slug": "frumu-ai/tandem-agents"},
                planned_subtasks=list(subtasks),
                pending_subtasks=list(subtasks),
                worker_results=[],
                blackboard={"subtasks": [dict(item) for item in subtasks]},
                status=initial_status(
                    "run-1",
                    {"task_id": "TAN-173"},
                    {"path": str(repo_path)},
                    {},
                    {},
                    {},
                    run_dir,
                ),
                coordination=_FakeCoordination(),
                lease_id=None,
                claim_identity={"host_id": "host-1"},
            )

            with (
                mock.patch(
                    "src.tandem_agents.core.execution.runner_core._execute_local_worker_pool",
                    return_value=[],
                ) as execute_pool,
                mock.patch("src.tandem_agents.core.phases.worker_dispatch._post_dispatch_validation"),
            ):
                dispatch_workers(ctx)

            execute_args = execute_pool.call_args.args
            self.assertEqual(execute_args[6], 1)
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            spawned = next(event for event in events if event["type"] == "swarm.spawned")
            self.assertEqual(spawned["payload"]["max_parallel"], 1)
            self.assertEqual(spawned["payload"]["spawned_workers"], 1)
            self.assertEqual(spawned["payload"]["queued_workers"], 3)
            self.assertEqual(spawned["payload"]["scheduled_workers"], 3)


if __name__ == "__main__":
    unittest.main()
