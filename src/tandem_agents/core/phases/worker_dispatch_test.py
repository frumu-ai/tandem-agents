from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.phases.worker_dispatch import (
    _failed_result_has_reviewable_source_and_test_diff,
    _subtask_is_no_change_guard_candidate,
    _subtask_is_repair_no_change_guard_candidate,
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
        self.assertEqual(_worker_no_change_abort_seconds(SimpleNamespace(cfg=SimpleNamespace(env={}))), 240.0)
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
            240.0,
        )

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
