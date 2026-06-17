from __future__ import annotations

from contextlib import contextmanager
import json
import tempfile
import threading
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from textwrap import dedent

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
from src.tandem_agents.core.engine.prompts import build_manager_prompt
from src.tandem_agents.core.execution.run_lifecycle import block_run
from src.tandem_agents.core.execution.runner_core import (
    _completed_subtask_ids_for_retry,
    _deferred_subtasks_for_retry,
    _all_subtasks_verified_existing,
    _annotate_pr_candidate_current_layout,
    _auto_approve_loop,
    _collect_worker_changed_files,
    _crash_blocker_for_exception,
    _execute_local_worker_pool,
    _final_lease_release_decision,
    _has_unresolved_write_required_worker_failure,
    _integration_blocker_message,
    _integration_can_retry,
    _integration_failure_can_defer_to_review,
    _integration_event_type,
    _integration_prompt_timeout_seconds,
    _integration_semantic_blocker_can_defer_to_review,
    _linear_mcp_authorization_url,
    _linear_comment_task_summary,
    _permission_requests_from_payload,
    _partial_diff_artifacts_for_retry,
    _merge_partial_diff_artifacts_for_retry,
    _preserve_and_reset_blocked_worktree,
    _prepare_subtasks_with_discovery,
    _pr_candidate_edit_goal,
    _pr_candidate_target_files,
    _pr_candidate_unexpected_changed_files,
    _normalize_manager_subtasks,
    _task_mentions_external_pr_candidates,
    _touch_coordination,
    _verification_can_retry,
    _worker_failure_blocker,
    _worker_failure_can_retry,
    _worker_failure_retry_feedback,
    _worker_incomplete_diff_extra_retries,
    _discard_partial_diff_repair_artifacts,
    _record_worker_result,
    _record_coding_run_contract,
    _record_review_policy,
    _run_integration_prompt,
    _sticky_expected_repo_files,
    _validation_expected_repo_files,
)
from src.tandem_agents.core.engine.process_utils import run_command
from src.tandem_agents.core.engine.prompts import build_worker_prompt
from src.tandem_agents.runtime.runstate import append_event


class RunnerCoreDiscoveryTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "agent.yaml").write_text(
            dedent(
                """
                agent:
                  name: ACA
                tandem:
                  base_url: http://127.0.0.1:39733
                task_source:
                  type: manual
                  prompt: Permission test
                repository:
                  slug: frumu-ai/example
                provider:
                  id: openai
                  model: gpt-4.1-mini
                output:
                  root: runs
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def test_crash_blocker_classifies_linear_mcp_oauth_refresh_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.type = "linear"

            blocker = _crash_blocker_for_exception(
                cfg,
                RuntimeError(
                    "Server error '500 Internal Server Error' for url 'http://127.0.0.1:39731/tool/execute'; "
                    "mcp oauth token refresh failed with HTTP 400: invalid_grant Client ID mismatch"
                ),
            )

            self.assertEqual(blocker["kind"], "linear_mcp_auth_required")
            self.assertIn("Reconnect the Linear MCP server", blocker["message"])
            self.assertIn("invalid_grant", blocker["phase_detail"])

    def test_crash_blocker_classifies_linear_mcp_pending_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.type = "linear"

            with patch(
                "src.tandem_agents.core.execution.runner_core._linear_mcp_authorization_url",
                return_value="https://linear.example.test/authorize",
            ):
                blocker = _crash_blocker_for_exception(
                    cfg,
                    RuntimeError("Linear MCP server 'linear' is awaiting authorization."),
                )

            self.assertEqual(blocker["kind"], "linear_mcp_auth_required")
            self.assertIn("https://linear.example.test/authorize", blocker["message"])
            self.assertEqual(blocker["authorization_url"], "https://linear.example.test/authorize")

    def test_block_run_merges_structured_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            layout = {
                "status": run_dir / "status.json",
                "events": run_dir / "events.jsonl",
                "summary": run_dir / "summary.md",
            }

            block_run(
                run_id="run-1",
                run_dir=run_dir,
                layout=layout,
                cfg=cfg,
                task={"title": "Auth task"},
                repo={"path": str(root)},
                engine={},
                phase="intake",
                kind="linear_mcp_auth_required",
                message="Linear auth required",
                event_payload={"authorization_url": "https://linear.example.test/authorize"},
            )

            event = json.loads(layout["events"].read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["type"], "run.blocked")
            self.assertEqual(
                event["payload"]["authorization_url"],
                "https://linear.example.test/authorize",
            )

    def test_crash_blocker_handles_missing_linear_mcp_auth_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.type = "linear"

            with patch(
                "src.tandem_agents.core.execution.runner_core._linear_mcp_authorization_url",
                return_value="",
            ):
                blocker = _crash_blocker_for_exception(
                    cfg,
                    RuntimeError("Linear MCP server 'linear' is awaiting authorization."),
                )

            self.assertEqual(blocker["kind"], "linear_mcp_auth_required")
            self.assertNotIn("Authorization URL:", blocker["message"])

    def test_linear_mcp_authorization_url_reads_engine_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core._engine_request_json",
                return_value={
                    "linear": {
                        "last_auth_challenge": {
                            "authorization_url": "https://linear.example.test/authorize"
                        }
                    }
                },
            ):
                self.assertEqual(
                    _linear_mcp_authorization_url(cfg),
                    "https://linear.example.test/authorize",
                )

    def test_crash_blocker_keeps_non_linear_oauth_text_internal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))

            blocker = _crash_blocker_for_exception(
                cfg,
                RuntimeError("mcp oauth token refresh failed with HTTP 400: invalid_grant"),
            )

            self.assertEqual(blocker["kind"], "internal_error")
            self.assertIn("Unhandled exception", blocker["message"])

    def test_permission_requests_from_payload_accepts_engine_requests_shape(self) -> None:
        payload = {
            "requests": [
                {"id": "req-1", "status": "pending", "permission": "bash"},
                {"id": "req-2", "status": "allow", "permission": "bash"},
            ],
            "rules": [],
        }

        self.assertEqual(
            _permission_requests_from_payload(payload),
            [
                {"id": "req-1", "status": "pending", "permission": "bash"},
                {"id": "req-2", "status": "allow", "permission": "bash"},
            ],
        )

    def test_touch_coordination_mirrors_renewed_lease_into_status_file(self) -> None:
        class FakeCoordination:
            def __init__(self) -> None:
                self.updated_run: dict[str, object] | None = None

            def heartbeat_lease(self, lease_id: str, *, lease_ttl_seconds: int) -> dict[str, object]:
                self.heartbeat_args = {
                    "lease_id": lease_id,
                    "lease_ttl_seconds": lease_ttl_seconds,
                }
                return {
                    "lease_id": lease_id,
                    "task_key": "task-1",
                    "worker_id": "worker-1",
                    "host_id": "host-1",
                    "status": "active",
                    "heartbeat_at_ms": 111,
                    "expires_at_ms": 222,
                }

            def update_run(self, run_id: str, **kwargs: object) -> None:
                self.updated_run = {"run_id": run_id, **kwargs}

        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            ctx = SimpleNamespace(
                status={
                    "coordination": {
                        "lease_id": "lease-1",
                        "lease_expires_at_ms": 1,
                    }
                },
                layout={"status": status_path},
                consecutive_heartbeat_misses=0,
                coordination_lost=False,
            )
            coordination = FakeCoordination()

            heartbeat_ok = _touch_coordination(
                coordination,
                run_id="run-1",
                lease_id="lease-1",
                lease_ttl_seconds=300,
                status="running",
                phase="worker_execution",
                ctx=ctx,
            )

            self.assertTrue(heartbeat_ok)
            self.assertEqual(ctx.status["coordination"]["lease_expires_at_ms"], 222)
            self.assertEqual(ctx.status["coordination"]["lease_heartbeat_at_ms"], 111)
            self.assertEqual(ctx.status["coordination"]["lease_status"], "active")
            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["coordination"]["lease_expires_at_ms"], 222)
            self.assertEqual(coordination.updated_run["phase"], "worker_execution")

    def test_touch_coordination_blocks_status_after_stale_lease_misses(self) -> None:
        class FakeCoordination:
            def __init__(self) -> None:
                self.updated_runs: list[dict[str, object]] = []

            def heartbeat_lease(self, _lease_id: str, *, lease_ttl_seconds: int) -> None:
                self.lease_ttl_seconds = lease_ttl_seconds
                return None

            def get_lease(self, lease_id: str) -> dict[str, object]:
                return {
                    "lease_id": lease_id,
                    "task_key": "task-1",
                    "worker_id": "worker-1",
                    "host_id": "host-1",
                    "status": "stale",
                    "heartbeat_at_ms": 111,
                    "expires_at_ms": 222,
                }

            def update_run(self, run_id: str, **kwargs: object) -> None:
                self.updated_runs.append({"run_id": run_id, **kwargs})

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            status_path = run_dir / "status.json"
            events_path = run_dir / "events.jsonl"
            ctx = SimpleNamespace(
                run_id="run-1",
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "coordination": {
                        "lease_id": "lease-1",
                        "lease_status": "active",
                    },
                },
                layout={"status": status_path, "events": events_path},
                consecutive_heartbeat_misses=0,
                coordination_lost=False,
            )
            coordination = FakeCoordination()

            for _ in range(3):
                heartbeat_ok = _touch_coordination(
                    coordination,
                    run_id="run-1",
                    lease_id="lease-1",
                    lease_ttl_seconds=300,
                    status="running",
                    phase="worker_execution",
                    ctx=ctx,
                )

            self.assertFalse(heartbeat_ok)
            self.assertTrue(ctx.coordination_lost)
            self.assertEqual(ctx.status["run"]["status"], "blocked")
            self.assertEqual(ctx.status["blocker"]["kind"], "coordination_lost")
            self.assertEqual(ctx.status["coordination"]["lease_status"], "stale")
            self.assertEqual(ctx.status["coordination"]["consecutive_heartbeat_misses"], 3)
            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["run"]["status"], "blocked")
            self.assertEqual(persisted["coordination"]["lease_status"], "stale")
            self.assertEqual(coordination.updated_runs[-1]["status"], "blocked")
            self.assertTrue(coordination.updated_runs[-1]["completed"])
            event = json.loads(events_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["type"], "coordination_lost")
            self.assertEqual(event["payload"]["lease_status"], "stale")

    def test_touch_coordination_ignores_late_heartbeat_after_completed_lease(self) -> None:
        class FakeCoordination:
            def __init__(self) -> None:
                self.updated_runs: list[dict[str, object]] = []

            def heartbeat_lease(self, _lease_id: str, *, lease_ttl_seconds: int) -> None:
                self.lease_ttl_seconds = lease_ttl_seconds
                return None

            def get_lease(self, lease_id: str) -> dict[str, object]:
                return {
                    "lease_id": lease_id,
                    "task_key": "task-1",
                    "worker_id": "worker-1",
                    "host_id": "host-1",
                    "status": "completed",
                    "heartbeat_at_ms": 111,
                    "expires_at_ms": 222,
                }

            def update_run(self, run_id: str, **kwargs: object) -> None:
                self.updated_runs.append({"run_id": run_id, **kwargs})

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            status_path = run_dir / "status.json"
            events_path = run_dir / "events.jsonl"
            ctx = SimpleNamespace(
                run_id="run-1",
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "coordination": {
                        "lease_id": "lease-1",
                        "lease_status": "active",
                    },
                },
                layout={"status": status_path, "events": events_path},
                consecutive_heartbeat_misses=2,
                coordination_lost=False,
            )
            coordination = FakeCoordination()

            for _ in range(3):
                heartbeat_ok = _touch_coordination(
                    coordination,
                    run_id="run-1",
                    lease_id="lease-1",
                    lease_ttl_seconds=300,
                    status="running",
                    phase="worker_execution",
                    ctx=ctx,
                )

            self.assertTrue(heartbeat_ok)
            self.assertFalse(ctx.coordination_lost)
            self.assertEqual(ctx.consecutive_heartbeat_misses, 0)
            self.assertEqual(ctx.status["run"]["status"], "running")
            self.assertEqual(ctx.status["coordination"]["lease_status"], "completed")
            self.assertEqual(coordination.updated_runs, [])
            self.assertFalse(events_path.exists())

    def test_execute_local_worker_pool_reports_no_progress_timeout(self) -> None:
        def stalled_runner(*_args):
            time.sleep(0.05)
            return {
                "status": "completed",
                "returncode": 0,
                "output_excerpt": "finished after timeout",
                "changed_files": ["src/app.py"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = time.monotonic()
            results = _execute_local_worker_pool(
                self._config(root),
                "run-1",
                root,
                root / "runs" / "run-1",
                {"task_id": "TAN-1"},
                [{"id": "subtask-1", "title": "Do work", "write_required": True}],
                1,
                worker_runner=stalled_runner,
                worker_timeout_seconds=0.01,
            )
            elapsed = time.monotonic() - started

        self.assertEqual(len(results), 1)
        self.assertLess(elapsed, 0.04)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["blocker_kind"], "worker_no_progress")
        self.assertTrue(results[0]["worker_abandoned_after_timeout"])
        self.assertIn("no terminal result", results[0]["failure_reason"])
        self.assertIn("did not wait for the stuck worker thread", results[0]["recovery_action"])

    def test_worker_pool_uses_terminal_event_before_timeout_result(self) -> None:
        def event_then_stall(_cfg, run_id, _repo_path, run_dir, task, subtask, worker_id, _index):
            append_event(
                run_dir / "events.jsonl",
                "worker.completed",
                run_id,
                {
                    "worker_id": worker_id,
                    "subtask_id": subtask["id"],
                    "execution_id": subtask.get("_worker_execution_id"),
                    "returncode": 0,
                    "changed_files": ["src/app.py"],
                    "synced_files": ["src/app.py"],
                },
                task_id=task.get("task_id"),
                role="worker",
                repo={"path": str(_repo_path)},
            )
            time.sleep(0.05)
            return {
                "status": "completed",
                "returncode": 0,
                "changed_files": ["src/app.py"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            results = _execute_local_worker_pool(
                self._config(root),
                "run-1",
                root,
                run_dir,
                {"task_id": "TAN-1"},
                [{"id": "subtask-1", "title": "Do work", "write_required": True}],
                1,
                worker_runner=event_then_stall,
                worker_timeout_seconds=0.01,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(results[0]["returncode"], 0)
        self.assertEqual(results[0]["changed_files"], ["src/app.py"])
        self.assertNotEqual(results[0].get("blocker_kind"), "worker_no_progress")
        self.assertIn("terminal event", results[0].get("output_excerpt", ""))

    def test_serial_worker_pool_continues_after_abandoned_completed_result(self) -> None:
        def slow_runner(_cfg, _run_id, _repo_path, _run_dir, _task, subtask, worker_id, _index):
            if worker_id == "worker-1":
                time.sleep(0.2)
            return {
                "worker_id": worker_id,
                "subtask_id": subtask["id"],
                "status": "completed",
                "returncode": 0,
                "changed_files": [f"src/{worker_id}.py"],
            }

        def abort_first(index, subtask, worker_id):
            if index != 1:
                return None
            return {
                "worker_id": worker_id,
                "subtask_id": subtask["id"],
                "status": "completed",
                "returncode": 0,
                "changed_files": ["src/worker-1.py"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = time.monotonic()
            results = _execute_local_worker_pool(
                self._config(root),
                "run-1",
                root,
                root / "runs" / "run-1",
                {"task_id": "TAN-1"},
                [
                    {"id": "subtask-1", "title": "One", "write_required": True},
                    {"id": "subtask-2", "title": "Two", "write_required": True},
                ],
                1,
                worker_runner=slow_runner,
                abort_result=abort_first,
                worker_timeout_seconds=1,
            )
            elapsed = time.monotonic() - started

        self.assertEqual([result["subtask_id"] for result in results], ["subtask-1", "subtask-2"])
        self.assertLess(elapsed, 0.15)

    def test_abandoned_no_progress_worker_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                _worker_failure_can_retry(
                    self._config(Path(tmp)),
                    {
                        "kind": "worker_no_progress",
                        "worker": {"worker_abandoned_after_timeout": True},
                    },
                    attempt=0,
                    base_max_loops=2,
                )
            )

    def test_execute_local_worker_pool_keeps_result_when_callback_fails(self) -> None:
        def worker_runner(*_args):
            return {
                "worker_id": "worker-1",
                "subtask_id": "subtask-1",
                "status": "failed",
                "returncode": 1,
                "blocker_kind": "engine_dispatch_failed",
            }

        def broken_callback(_result):
            raise RuntimeError("status write failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = _execute_local_worker_pool(
                self._config(root),
                "run-1",
                root,
                root / "runs" / "run-1",
                {"task_id": "TAN-1"},
                [{"id": "subtask-1", "title": "Do work", "write_required": True}],
                1,
                worker_runner=worker_runner,
                on_result=broken_callback,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["blocker_kind"], "engine_dispatch_failed")

    def test_execute_local_worker_pool_calls_start_callback_only_for_started_workers(self) -> None:
        started: list[tuple[str, str]] = []

        def worker_runner(_cfg, _run_id, _repo_path, _run_dir, _task, subtask, worker_id, index):
            return {
                "worker_id": worker_id,
                "subtask_index": index,
                "subtask_id": subtask["id"],
                "title": subtask["title"],
                "status": "failed",
                "returncode": 1,
                "blocker_kind": "worker_corrupt_diff",
                "write_required": True,
            }

        def on_start(worker_id: str, subtask: dict[str, object]) -> None:
            started.append((worker_id, str(subtask.get("id"))))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = _execute_local_worker_pool(
                self._config(root),
                "run-1",
                root,
                root / "runs" / "run-1",
                {"task_id": "TAN-1"},
                [
                    {"id": "subtask-1", "title": "First", "write_required": True},
                    {"id": "subtask-2", "title": "Second", "write_required": True},
                ],
                1,
                worker_runner=worker_runner,
                on_start=on_start,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(started, [("worker-1", "subtask-1")])

    def test_execute_local_worker_pool_assigns_unique_execution_worktrees(self) -> None:
        seen_worktrees: list[str] = []

        def worker_runner(_cfg, _run_id, _repo_path, _run_dir, _task, subtask, worker_id, index):
            seen_worktrees.append(str(subtask.get("_worker_worktree_name") or ""))
            return {
                "worker_id": worker_id,
                "subtask_index": index,
                "subtask_id": subtask["id"],
                "title": subtask["title"],
                "status": "failed",
                "returncode": 1,
                "blocker_kind": "worker_incomplete_diff",
                "write_required": True,
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subtask = {"id": "subtask-1", "title": "Do work", "write_required": True}
            for _ in range(2):
                _execute_local_worker_pool(
                    self._config(root),
                    "run-1",
                    root,
                    root / "runs" / "run-1",
                    {"task_id": "TAN-1"},
                    [subtask],
                    1,
                    worker_runner=worker_runner,
                )

        self.assertEqual(len(seen_worktrees), 2)
        self.assertNotEqual(seen_worktrees[0], seen_worktrees[1])
        self.assertTrue(all(name.startswith("worker-1--subtask-1--exec-") for name in seen_worktrees))

    def test_manager_subtask_deliverable_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Verify Bug Monitor gates"},
                [
                    {
                        "id": "SIG-01-A",
                        "title": "Map gate flow",
                        "goal": "Confirm existing Bug Monitor gate flow.",
                        "deliverable": "A short note identifying gate APIs and the verification command.",
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            ["A short note identifying gate APIs and the verification command."],
        )
        self.assertEqual(
            subtasks[0]["deliverables"],
            ["A short note identifying gate APIs and the verification command."],
        )

    def test_manager_subtask_required_work_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Verify Bug Monitor gates"},
                [
                    {
                        "id": "sig01-e2e-quality-gate-fixture",
                        "title": "Add focused end-to-end Bug Monitor quality-gate fixture coverage",
                        "goal": "Exercise a mixed Bug Monitor fixture.",
                        "required_work": [
                            "Assert minor retries do not create draft work.",
                            "Assert blocked signals include quality-gate reasons.",
                        ],
                        "verification": ["Run the focused fixture test."],
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            [
                "Assert minor retries do not create draft work.",
                "Assert blocked signals include quality-gate reasons.",
            ],
        )
        self.assertEqual(subtasks[0]["verification_commands"], ["Run the focused fixture test."])

    def test_manager_subtask_expected_verification_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Verify Bug Monitor gates"},
                [
                    {
                        "id": "sig01-e2e-quality-gate-fixture",
                        "title": "Add/refine focused fixture coverage",
                        "goal": "Exercise Bug Monitor signal quality gates.",
                        "instructions": [
                            "Add or refine a focused fixture that covers quality-gate outcomes.",
                        ],
                        "expected_verification": [
                            "Focused Bug Monitor tests pass and cover accepted, retried, and blocked signals.",
                        ],
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            ["Focused Bug Monitor tests pass and cover accepted, retried, and blocked signals."],
        )

    def test_manager_subtask_scope_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Add prompt-injection exfiltration evals"},
                [
                    {
                        "title": "Add KB-MCP bulk export scenarios",
                        "goal": "Cover prompt-injected memory export attempts.",
                        "scope": "Add YAML eval scenarios and bounded-exposure assertions for no bulk export.",
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            ["Add YAML eval scenarios and bounded-exposure assertions for no bulk export."],
        )

    def test_manager_subtask_preserves_deterministic_repair_scope(self) -> None:
        repair_files = [
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {
                    "title": "TAN-170",
                    "target_files": [
                        *repair_files,
                        "src/tandem_agents/runtime/operator_dashboard.py",
                        "src/tandem_agents/runtime/operator_dashboard_test.py",
                        "src/tandem_agents/runtime/operator_view_test.py",
                    ],
                },
                [
                    {
                        "id": "repair-testless-partial-diff",
                        "title": "Repair testless partial diff",
                        "goal": "Repair the rejected repository partial diff with required test coverage.",
                        "files": repair_files,
                        "target_files": repair_files,
                        "acceptance_criteria": ["Read and edit the required test file first."],
                        "discarded_partial_diff_patch": "/runs/run-1/artifacts/worker-1.patch",
                        "deterministic_testless_repair": True,
                        "deterministic_partial_diff_repair": True,
                        "repair_changed_files": ["src/tandem_agents/core/repository/repository.py"],
                        "repair_requires_test_followup": [
                            "src/tandem_agents/core/repository/repository_test.py",
                        ],
                        "scope_note": "Only repair the rejected repository diff.",
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(subtasks[0]["files"], repair_files)
        self.assertEqual(subtasks[0]["target_files"], repair_files)
        self.assertTrue(subtasks[0]["deterministic_testless_repair"])
        self.assertTrue(subtasks[0]["deterministic_partial_diff_repair"])
        self.assertEqual(
            subtasks[0]["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        self.assertIn("Only repair the rejected repository diff.", subtasks[0]["scope_note"])

    def test_manager_subtask_keeps_worker_targets_narrow_with_task_targets(self) -> None:
        task_target_files = [
            "src/tandem_agents/core/phases/task_intake.py",
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
            "src/tandem_agents/core/phases/finalize.py",
            "src/tandem_agents/core/phases/pr_body.py",
        ]
        worker_files = [
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "TAN-170", "target_files": task_target_files},
                [
                    {
                        "id": "subtask-1",
                        "title": "Add repository primitives",
                        "goal": "Implement repository worktree primitives.",
                        "files": worker_files,
                        "acceptance_criteria": ["Repository code and tests cover isolated worktrees."],
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(subtasks[0]["files"], worker_files)
        self.assertEqual(subtasks[0]["target_files"], worker_files)

    def test_manager_subtask_filters_gitignored_target_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            run_command(["git", "init"], cwd=repo_path)
            (repo_path / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")
            subtasks = _normalize_manager_subtasks(
                {"title": "Define meta-harness eval crate"},
                [
                    {
                        "title": "Define docs and crate",
                        "goal": "Define tracked crate contracts without private docs deliverables.",
                        "files": [
                            "docs/internal/meta-harness/KANBAN.md",
                            "crates/tandem-meta-harness-eval/src/lib.rs",
                        ],
                        "acceptance_criteria": ["Tracked crate contract is defined."],
                    }
                ],
                str(repo_path),
            )

        self.assertEqual(subtasks[0]["files"], ["crates/tandem-meta-harness-eval/src/lib.rs"])
        self.assertEqual(subtasks[0]["target_files"], ["crates/tandem-meta-harness-eval/src/lib.rs"])
        self.assertEqual(subtasks[0]["ignored_target_files"], ["docs/internal/meta-harness/KANBAN.md"])

    def test_manager_subtask_drops_root_manifest_only_target_after_ignored_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            run_command(["git", "init"], cwd=repo_path)
            (repo_path / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")
            subtasks = _normalize_manager_subtasks(
                {"title": "Define meta-harness eval crate"},
                [
                    {
                        "title": "Define docs and manifest metadata",
                        "goal": "Define private docs and root manifest metadata.",
                        "files": [
                            "docs/internal/meta-harness/KANBAN.md",
                            "docs/internal/meta-harness/eval-crate.md",
                            "Cargo.toml",
                        ],
                        "acceptance_criteria": ["Tracked crate contract is defined."],
                    }
                ],
                str(repo_path),
            )

        self.assertEqual(subtasks[0]["files"], [])
        self.assertEqual(subtasks[0]["target_files"], [])
        self.assertEqual(
            subtasks[0]["ignored_target_files"],
            ["docs/internal/meta-harness/KANBAN.md", "docs/internal/meta-harness/eval-crate.md"],
        )
        self.assertIn("Do not satisfy this task by placing a prose specification", subtasks[0]["scope_note"])

    def test_permission_requests_from_payload_accepts_sdk_permissions_shape(self) -> None:
        payload = {
            "permissions": [
                {"request_id": "req-1", "status": "pending", "permission": "bash"},
            ],
        }

        self.assertEqual(
            _permission_requests_from_payload(payload),
            [{"request_id": "req-1", "status": "pending", "permission": "bash"}],
        )

    def test_auto_approve_loop_replies_to_pending_engine_permissions(self) -> None:
        stop_event = threading.Event()
        replied: list[tuple[str, str]] = []

        def fake_sleep(_seconds: float) -> None:
            stop_event.set()

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core.sdk_agent_teams_list_approvals",
                return_value={"approvals": []},
            ), patch(
                "src.tandem_agents.core.execution.runner_core.list_engine_permissions",
                return_value={
                    "requests": [
                        {
                            "id": "perm-1",
                            "status": "pending",
                            "permission": "apply_patch",
                        }
                    ]
                },
            ), patch(
                "src.tandem_agents.core.execution.runner_core.reply_engine_permission",
                side_effect=lambda _cfg, request_id, reply: replied.append((request_id, reply)) or {"ok": True},
            ), patch("src.tandem_agents.core.execution.runner_core.time.sleep", side_effect=fake_sleep):
                _auto_approve_loop(cfg, stop_event)

        self.assertEqual(replied, [("perm-1", "allow")])

    def test_final_lease_release_uses_nested_blocked_result(self) -> None:
        ctx = SimpleNamespace(status={"run": {"status": "running"}}, layout={})

        release_status, release_reason = _final_lease_release_decision(
            ctx,
            layout={},
            crashed_exc=None,
            result={
                "status": {
                    "run": {"status": "blocked"},
                    "blocker": {
                        "active": True,
                        "kind": "verification_failed",
                        "detail": "smoke test failed",
                    },
                }
            },
        )

        self.assertEqual(release_status, "blocked")
        self.assertEqual(release_reason, "smoke test failed")

    def test_final_lease_release_reads_persisted_blocked_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "run": {"status": "blocked"},
                        "phase": {"detail": "review did not approve"},
                        "blocker": {"active": True, "kind": "verification_failed"},
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(status={"run": {"status": "running"}}, layout={"status": status_path})

            release_status, release_reason = _final_lease_release_decision(
                ctx,
                layout={"status": status_path},
                crashed_exc=None,
                result=None,
            )

        self.assertEqual(release_status, "blocked")
        self.assertEqual(release_reason, "review did not approve")

    def test_final_lease_release_fails_closed_on_unknown_status(self) -> None:
        ctx = SimpleNamespace(status={"run": {"status": "running"}}, layout={})

        release_status, release_reason = _final_lease_release_decision(
            ctx,
            layout={},
            crashed_exc=None,
            result=None,
        )

        self.assertEqual(release_status, "blocked")
        self.assertEqual(release_reason, "run finished without terminal status")

    def test_empty_manager_plan_still_injects_discovered_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "index.html").write_text("<html><body>Todo app</body></html>\n", encoding="utf-8")
            (repo_path / "styles.css").write_text(".todo-item { color: #000; }\n", encoding="utf-8")
            task = {
                "title": "cleanup",
                "description": "Add due dates + overdue highlighting + filters to the TODO app",
                "acceptance_criteria": [
                    "Users can set an optional due date when creating a todo.",
                    "Filter controls (All, Active, Completed, Overdue) work correctly.",
                ],
            }

            discovered_files, subtasks = _prepare_subtasks_with_discovery(task, {"subtasks": []}, repo_path, 1)

            self.assertIn("index.html", discovered_files)
            self.assertIn("styles.css", discovered_files)
            self.assertTrue(subtasks)
            self.assertTrue(subtasks[0]["files"])

    def test_single_worker_bug_monitor_subtask_narrows_overbroad_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            paths = [
                "crates/tandem-server/src/http/tests/bug_monitor.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part01.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part02.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part03.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part04.rs",
                "crates/tandem-server/src/bug_monitor/log_parser.rs",
                "crates/tandem-server/src/bug_monitor/service.rs",
            ]
            for rel_path in paths:
                target = repo_path / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("bug monitor quality gate draft duplicate confidence retry\n", encoding="utf-8")

            task = {
                "title": "SIG-01 Verify Bug Monitor end-to-end against signal quality gates",
                "description": "Bug Monitor should prove quality gates block noisy signals.",
                "acceptance_criteria": [
                    "Minor retries, routine progress, low-confidence speculation, and duplicate failures do not create new draft work.",
                ],
            }
            manager_plan = {
                "subtasks": [
                    {
                        "title": "Add focused Bug Monitor quality-gate regression tests",
                        "goal": "Extend the existing Bug Monitor server/control-panel test path.",
                        "files": paths[:-1],
                    }
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, manager_plan, repo_path, 1)

            self.assertEqual(
                subtasks[0]["files"],
                [
                    "crates/tandem-server/src/http/tests/bug_monitor_parts/part03.rs",
                    "crates/tandem-server/src/http/tests/bug_monitor_parts/part04.rs",
                    "crates/tandem-server/src/bug_monitor/service.rs",
                ],
            )
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
            self.assertIn("ACA narrowed", subtasks[0]["scope_note"])

    def test_pr_candidate_task_does_not_use_discovered_files_as_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "crates").mkdir()
            (repo_path / "crates" / "optimizations.rs").write_text("fn existing() {}\n", encoding="utf-8")
            task = {
                "title": "Consolidate worthwhile small Bolt optimizations into one intentional PR",
                "description": "\n".join(
                    [
                        "Initial candidates to inspect/cherry-pick if still relevant:",
                        "* #1459 - 3+/3-, 3 files",
                        "* #1449 - 9+/3-, 2 files",
                        "",
                        "Acceptance:",
                        "* Apply only improvements that still make sense in the current file layout.",
                    ]
                ),
                "acceptance_criteria": [
                    "#1459 - 3+/3-, 3 files",
                    "Apply only improvements that still make sense in the current file layout.",
                ],
                "source": {"type": "linear", "item": "TAN-111"},
            }

            discovered_files, subtasks = _prepare_subtasks_with_discovery(task, {"subtasks": []}, repo_path, 1)

            self.assertTrue(_task_mentions_external_pr_candidates(task))
            self.assertIn("crates/optimizations.rs", discovered_files)
            self.assertTrue(subtasks)
            self.assertEqual(subtasks[0]["files"], [])
            self.assertEqual(subtasks[0]["target_files"], [])

    def test_manager_subtasks_are_capped_by_worker_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            raw_subtasks = [
                {"id": f"subtask-{index}", "title": f"Subtask {index}", "goal": f"Goal {index}"}
                for index in range(1, 6)
            ]

            _, subtasks = _prepare_subtasks_with_discovery(
                {"title": "cleanup", "description": "cleanup"},
                {"subtasks": raw_subtasks},
                repo_path,
                3,
            )

            self.assertEqual(
                [subtask["id"] for subtask in subtasks],
                ["subtask-1", "subtask-2", "subtask-3"],
            )

    def test_manager_subtasks_merge_for_single_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            manager_plan = {
                "subtasks": [
                    {
                        "id": "crate",
                        "title": "Define crate boundary",
                        "goal": "Create the eval crate boundary.",
                        "files": ["Cargo.toml", "crates/tandem-eval/src/lib.rs"],
                        "acceptance_criteria": ["Eval crate boundaries are defined."],
                        "verification_commands": ["cargo check -p tandem-eval"],
                    },
                    {
                        "id": "trace",
                        "title": "Define trace contracts",
                        "goal": "Add trace-store contracts.",
                        "files": [
                            "crates/tandem-eval/src/lib.rs",
                            "crates/tandem-eval/src/trace.rs",
                            "crates/tandem-eval/tests/trace_contract.rs",
                        ],
                        "acceptance_criteria": ["Trace store and replayable trace model are specified."],
                        "verification_commands": ["cargo test -p tandem-eval --test trace_contract"],
                    },
                    {
                        "id": "scoring",
                        "title": "Define scoring contracts",
                        "goal": "Add workflow version scoring contracts.",
                        "files": [
                            "crates/tandem-eval/src/lib.rs",
                            "crates/tandem-eval/src/scoring.rs",
                            "crates/tandem-eval/tests/scoring_contract.rs",
                        ],
                        "acceptance_criteria": ["Scored workflow/version model is specified."],
                    },
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(
                {"title": "MH-01 Define meta-harness eval crate"},
                manager_plan,
                repo_path,
                1,
            )

            self.assertEqual(len(subtasks), 1)
            self.assertEqual(subtasks[0]["id"], "subtask-1")
            self.assertEqual(
                subtasks[0]["files"],
                [
                    "Cargo.toml",
                    "crates/tandem-eval/src/lib.rs",
                    "crates/tandem-eval/src/trace.rs",
                    "crates/tandem-eval/tests/trace_contract.rs",
                    "crates/tandem-eval/src/scoring.rs",
                    "crates/tandem-eval/tests/scoring_contract.rs",
                ],
            )
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
            self.assertEqual(
                subtasks[0]["acceptance_criteria"],
                [
                    "Eval crate boundaries are defined.",
                    "Trace store and replayable trace model are specified.",
                    "Scored workflow/version model is specified.",
                ],
            )
            self.assertIn("ACA merged multiple manager subtasks", subtasks[0]["scope_note"])
            self.assertEqual([item["id"] for item in subtasks[0]["merged_subtasks"]], ["crate", "trace", "scoring"])

    def test_manager_subtasks_without_merge_are_still_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            manager_plan = {
                "subtasks": [
                    {
                        "id": "registry",
                        "title": "Add registry tests",
                        "goal": "Cover registry normalization.",
                        "files": ["crates/tandem-tools/src/lib.rs"],
                        "acceptance_criteria": ["Registry cases pass."],
                    },
                    {
                        "id": "sandbox",
                        "title": "Add path sandbox tests",
                        "goal": "Cover workspace path sandboxing.",
                        "files": ["crates/tandem-tools/src/builtin_tools.rs"],
                        "acceptance_criteria": ["Path sandbox cases pass."],
                    },
                    {
                        "id": "approval",
                        "title": "Add approval tests",
                        "goal": "Cover approval classifier policy.",
                        "files": ["crates/tandem-tools/src/approval_classifier.rs"],
                        "acceptance_criteria": ["Approval cases pass."],
                    },
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(
                {"title": "TAN-216 Add tandem-tools tests"},
                manager_plan,
                repo_path,
                1,
                merge_manager_subtasks=False,
            )

            self.assertEqual([subtask["id"] for subtask in subtasks], ["registry"])
            self.assertEqual(
                [subtask["files"] for subtask in subtasks],
                [
                    ["crates/tandem-tools/src/lib.rs"],
                ],
            )
            self.assertTrue(all("merged_subtasks" not in subtask for subtask in subtasks))

    def test_inferred_repo_targets_do_not_overwrite_manager_subtask_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            for rel_path, text in {
                "crates/tandem-tools/src/lib.rs": "resolve_registered_tool todo_write default_api bash\n",
                "crates/tandem-tools/src/builtin_tools.rs": "resolve_tool_path workspace sandbox symlink\n",
                "crates/tandem-tools/src/approval_classifier.rs": "approval_classifier classify mcp stripe\n",
            }.items():
                path = repo_path / rel_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            task = {
                "title": "Add unit test suite for tandem-tools: registry resolution, path sandbox, approval classifier",
                "description": "Add tests for registry resolution, path sandbox, and approval classifier.",
                "acceptance_criteria": ["cargo test -p tandem-tools passes"],
            }
            manager_plan = {
                "subtasks": [
                    {
                        "id": "registry",
                        "title": "Add registry-resolution unit tests",
                        "goal": "Cover resolve_registered_tool only.",
                        "files": ["crates/tandem-tools/src/lib.rs"],
                        "acceptance_criteria": ["Registry cases pass."],
                    }
                ]
            }

            discovered_files, subtasks = _prepare_subtasks_with_discovery(
                task,
                manager_plan,
                repo_path,
                4,
            )

            self.assertGreaterEqual(len(discovered_files), 2)
            self.assertEqual(subtasks[0]["files"], ["crates/tandem-tools/src/lib.rs"])
            self.assertEqual(subtasks[0]["target_files"], ["crates/tandem-tools/src/lib.rs"])

    def test_missing_manager_source_targets_are_dropped_when_existing_target_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            path = repo_path / "crates/tandem-tools/src/lib.rs"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("pub fn resolve_registered_tool() {}\n", encoding="utf-8")
            manager_plan = {
                "subtasks": [
                    {
                        "id": "registry",
                        "title": "Registry tests",
                        "goal": "Add registry resolution tests.",
                        "files": [
                            "crates/tandem-tools/src/lib.rs",
                            "crates/tandem-tools/src/registry.rs",
                            "crates/tandem-tools/src/registry_tests.rs",
                        ],
                        "acceptance_criteria": ["Registry cases pass."],
                    }
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(
                {"title": "Add tandem-tools registry tests"},
                manager_plan,
                repo_path,
                4,
            )

            self.assertEqual(subtasks[0]["files"], ["crates/tandem-tools/src/lib.rs"])
            self.assertEqual(subtasks[0]["target_files"], ["crates/tandem-tools/src/lib.rs"])
            self.assertIn("dropped non-existing manager file targets", subtasks[0]["scope_note"])
            self.assertIn("crates/tandem-tools/src/registry.rs", subtasks[0]["scope_note"])

    def test_expected_repo_files_are_sticky_across_retries(self) -> None:
        blackboard = {
            "repo_validation": {
                "expected_files": [
                    "crates/tandem-meta-harness-eval/src/lib.rs",
                    "crates/tandem-meta-harness-eval/src/scoring.rs",
                ]
            }
        }

        expected = _sticky_expected_repo_files(
            blackboard,
            [
                "crates/tandem-meta-harness-eval/src/lib.rs",
                "crates/tandem-meta-harness-eval/src/trace.rs",
            ],
        )

        self.assertEqual(
            expected,
            [
                "crates/tandem-meta-harness-eval/src/lib.rs",
                "crates/tandem-meta-harness-eval/src/scoring.rs",
                "crates/tandem-meta-harness-eval/src/trace.rs",
            ],
        )
        self.assertEqual(blackboard["expected_repo_files"], expected)

    def test_validation_expected_repo_files_drops_missing_untouched_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "crates/tandem-tools/src").mkdir(parents=True)
            (repo_path / "crates/tandem-tools/src/lib.rs").write_text("// tests\n", encoding="utf-8")

            expected = _validation_expected_repo_files(
                repo_path,
                [
                    "crates/tandem-tools/src/lib.rs",
                    ".github/workflows/rust.yml",
                ],
                ["crates/tandem-tools/src/lib.rs"],
            )

        self.assertEqual(expected, ["crates/tandem-tools/src/lib.rs"])

    def test_validation_expected_repo_files_keeps_changed_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)

            expected = _validation_expected_repo_files(
                repo_path,
                [".github/workflows/rust.yml"],
                [".github/workflows/rust.yml"],
            )

        self.assertEqual(expected, [".github/workflows/rust.yml"])

    def test_worker_prompt_includes_pr_candidate_context_artifact(self) -> None:
        task = {
            "title": "Consolidate worthwhile small Bolt optimizations into one intentional PR",
            "description": "Inspect #1459 before editing.",
        }
        subtask = {
            "id": "subtask-1",
            "title": "Inspect PRs",
            "goal": "Inspect candidates and apply safe changes.",
            "files": [],
            "target_files": [],
            "pr_candidate_context_artifact": "artifacts/pr_candidate_context.json",
            "pr_candidate_context": [{"number": 1459, "title": "Small cleanup", "state": "open"}],
        }

        prompt = build_worker_prompt("run-1", "worker-1", subtask, task, "/tmp/worktree")

        self.assertIn("ACA already fetched GitHub PR candidate context", prompt)
        self.assertIn("artifacts/pr_candidate_context.json", prompt)
        self.assertIn('"number": 1459', prompt)
        self.assertIn("This is an edit task, not a report-only task", prompt)
        self.assertIn("Do not stop after producing an applicability matrix", prompt)

    def test_pr_candidate_target_files_are_derived_from_context_without_noise_docs(self) -> None:
        contexts = [
            {
                "number": 1459,
                "changed_files": [
                    ".jules/bolt.md",
                    "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                    "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                ],
            },
            {
                "number": 1446,
                "files": [
                    {"filename": "src/components/logs/LogsDrawer.tsx"},
                    {"filename": "/src/lib/utils.ts"},
                ],
            },
            {"number": 1, "error": "not found", "changed_files": ["ignored.ts"]},
        ]

        self.assertEqual(
            _pr_candidate_target_files(contexts),
            [
                "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                "src/components/logs/LogsDrawer.tsx",
                "src/lib/utils.ts",
            ],
        )

    def test_pr_candidate_target_files_skip_stale_current_layout_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            current = repo_path / "packages" / "tandem-control-panel" / "src" / "pages" / "DashboardPage.tsx"
            current.parent.mkdir(parents=True)
            current.write_text("export function DashboardPage() {}\n", encoding="utf-8")
            contexts = [
                {
                    "number": 1459,
                    "files": [
                        {
                            "filename": "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                            "status": "modified",
                        },
                        {"filename": "src/lib/utils.ts", "status": "modified"},
                    ],
                },
            ]

            annotated = _annotate_pr_candidate_current_layout(contexts, repo_path)

            self.assertEqual(
                _pr_candidate_target_files(annotated),
                ["packages/tandem-control-panel/src/pages/DashboardPage.tsx"],
            )
            self.assertEqual(annotated[0]["stale_files"], ["src/lib/utils.ts"])
            self.assertEqual(
                _pr_candidate_unexpected_changed_files(
                    [{"pr_candidate_context": annotated}],
                    [
                        "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                        "src/lib/utils.ts",
                    ],
                ),
                ["src/lib/utils.ts"],
            )

    def test_pr_candidate_edit_goal_replaces_matrix_only_goal(self) -> None:
        goal = _pr_candidate_edit_goal("Produce a concise applicability matrix for each PR.")

        self.assertIn("Apply the still-relevant code changes", goal)
        self.assertIn("An applicability matrix alone is not sufficient", goal)

    def test_worker_failure_blocker_preserves_engine_empty_response_details(self) -> None:
        blocker = _worker_failure_blocker(
            [
                {
                    "worker_id": "worker-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "ENGINE_EMPTY_RESPONSE",
                    "blocker_kind": "engine_empty_response",
                    "engine": {
                        "session_id": "session-1",
                        "run_id": "run-engine-1",
                        "retry_count": 1,
                        "fallback_mode": "prompt_sync",
                    },
                }
            ]
        )

        self.assertEqual(blocker["kind"], "engine_empty_response")
        self.assertIn("session_id=session-1", blocker["detail"])
        self.assertIn("fallback=prompt_sync", blocker["detail"])

    def test_github_project_contract_target_files_override_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "unrelated.rs").write_text("fn unrelated() {}\n", encoding="utf-8")
            (repo_path / "crates").mkdir()
            task = {
                "title": "Add tenant helpers",
                "description": "\n".join(
                    [
                        "Add reusable tenant denial helpers",
                        "",
                        "## Files Likely Touched",
                        "- `crates/tandem-server/src/http/tests/mod.rs`",
                        "- `crates/tandem-server/src/app/state/tests/mod.rs`",
                    ]
                ),
                "source": {"type": "github_project", "issue_number": 1},
            }
            manager_plan = {
                "subtasks": [
                    {
                        "title": "wrong slice",
                        "files": ["src/unrelated.rs"],
                    }
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, manager_plan, repo_path, 1)

            self.assertEqual(
                subtasks[0]["files"],
                [
                    "crates/tandem-server/src/http/tests/mod.rs",
                    "crates/tandem-server/src/app/state/tests/mod.rs",
                ],
            )

    def test_github_project_fallback_subtask_keeps_contract_target_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "unrelated.rs").write_text("fn unrelated() {}\n", encoding="utf-8")
            task = {
                "title": "Filter sessions by tenant",
                "description": "\n".join(
                    [
                        "Filter session CRUD routes by tenant.",
                        "",
                        "## Files Likely Touched",
                        "- `crates/tandem-server/src/http/sessions.rs`",
                        "- `crates/tandem-core/src/storage_parts/`",
                    ]
                ),
                "source": {"type": "github_project", "issue_number": 1428},
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, {"subtasks": []}, repo_path, 1)

            self.assertEqual(
                subtasks[0]["files"],
                [
                    "crates/tandem-server/src/http/sessions.rs",
                    "crates/tandem-core/src/storage_parts/",
                ],
            )
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])

    def test_explicit_crate_path_and_symbols_override_fuzzy_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            files = {
                "crates/tandem-tools/src/lib.rs": (
                    "#[cfg(test)]\nmod tests {\n"
                    "fn registry_resolution() { resolve_registered_tool(); }\n}\n"
                ),
                "crates/tandem-tools/src/builtin_tools.rs": (
                    "fn resolve_tool_path() {}\nfn is_within_workspace_root() {}\n"
                ),
                "crates/tandem-tools/src/approval_classifier.rs": (
                    "pub fn classify() {}\nfn standing_allow_is_unsafe() {}\n"
                ),
                "crates/tandem-server/src/http/tests/approval_gate_matrix.rs": (
                    "approval gate matrix governance tests\n"
                ),
            }
            for rel_path, contents in files.items():
                target = repo_path / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
            task = {
                "title": "Add unit test suite for tandem-tools: registry resolution, path sandbox, approval classifier",
                "description": "\n".join(
                    [
                        "Add focused coverage in `crates/tandem-tools`.",
                        "Cover `resolve_registered_tool`, `resolve_tool_path`, `is_within_workspace_root`,",
                        "and `approval_classifier::classify` / `standing_allow_is_unsafe`.",
                    ]
                ),
                "acceptance_criteria": [
                    "Tests cover registry resolution aliases.",
                    "Tests cover path sandbox rejection cases.",
                    "Tests cover approval classifier allow/deny behavior.",
                ],
            }
            manager_plan = {
                "subtasks": [
                    {
                        "title": "wrong fuzzy target",
                        "files": ["crates/tandem-server/src/http/tests/approval_gate_matrix.rs"],
                    }
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, manager_plan, repo_path, 1)

            self.assertIn("crates/tandem-tools/src/lib.rs", subtasks[0]["files"])
            self.assertIn("crates/tandem-tools/src/builtin_tools.rs", subtasks[0]["files"])
            self.assertIn("crates/tandem-tools/src/approval_classifier.rs", subtasks[0]["files"])
            self.assertNotIn("crates/tandem-server/src/http/tests/approval_gate_matrix.rs", subtasks[0]["files"])
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])

    def test_verified_existing_short_circuit_requires_all_subtasks_satisfied(self) -> None:
        subtasks = [
            {"id": "subtask-1", "files": ["index.html", "styles.css"]},
            {"id": "subtask-2", "files": ["package.json"]},
        ]
        worker_results = [
            {"subtask_id": "subtask-1", "status": "skipped_existing"},
            {"subtask_id": "subtask-2", "status": "tolerated_failure"},
        ]

        self.assertTrue(_all_subtasks_verified_existing(subtasks, worker_results, {"ok": True}))
        self.assertFalse(_all_subtasks_verified_existing(subtasks, worker_results[:1], {"ok": True}))

    def test_verified_existing_short_circuit_rejects_github_project_tasks(self) -> None:
        subtasks = [{"id": "subtask-1", "files": ["index.html"]}]
        worker_results = [{"subtask_id": "subtask-1", "status": "skipped_existing"}]

        self.assertFalse(
            _all_subtasks_verified_existing(
                subtasks,
                worker_results,
                {"ok": True},
                {"source": {"type": "github_project", "project_item_id": 123}},
            )
        )
        self.assertFalse(
            _all_subtasks_verified_existing(
                subtasks,
                worker_results,
                {"ok": True},
                {
                    "source": {
                        "type": "github_project",
                        "project_item_id": 123,
                        "issue_url": "https://github.com/frumu-ai/tandem/issues/1",
                    }
                },
            )
        )

    def test_verified_existing_short_circuit_rejects_linear_code_edit_tasks(self) -> None:
        subtasks = [{"id": "subtask-1", "files": ["index.html"]}]
        worker_results = [{"subtask_id": "subtask-1", "status": "skipped_existing"}]

        self.assertFalse(
            _all_subtasks_verified_existing(
                subtasks,
                worker_results,
                {"ok": True},
                {"execution_kind": "code_edit", "source": {"type": "linear", "issue_id": "TAN-68"}},
            )
        )

    def test_write_required_worker_failure_is_unresolved_without_existing_proof(self) -> None:
        self.assertTrue(
            _has_unresolved_write_required_worker_failure(
                [{"subtask_id": "subtask-1", "status": "failed", "write_required": True}]
            )
        )
        self.assertFalse(
            _has_unresolved_write_required_worker_failure(
                [{"subtask_id": "subtask-1", "status": "failed", "write_required": False}]
            )
        )
        self.assertFalse(
            _has_unresolved_write_required_worker_failure(
                [
                    {
                        "subtask_id": "subtask-1",
                        "status": "failed",
                        "write_required": True,
                        "verified_existing": True,
                    }
                ]
            )
        )

    def test_worker_pool_preserves_subtask_write_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def worker_runner(*_args):
                return {
                    "status": "failed",
                    "returncode": 1,
                    "write_required": False,
                    "output_excerpt": "Worker reported success but produced no filesystem changes.",
                }

            results = _execute_local_worker_pool(
                resolve_config(root),
                "run-test",
                root,
                root,
                {"title": "Needs edits"},
                [
                    {
                        "id": "subtask-1",
                        "title": "Write the change",
                        "write_required": True,
                    }
                ],
                1,
                worker_runner=worker_runner,
            )

            self.assertEqual(results[0]["status"], "failed")
            self.assertTrue(results[0]["write_required"])
            self.assertTrue(_has_unresolved_write_required_worker_failure(results))

    def test_linear_comment_summary_records_pr_decisions(self) -> None:
        task = {
            "title": "Inventory Bolt/Jules PRs and record close/merge decision per PR",
            "raw_issue_body": "\n".join(
                [
                    "## Initial Inventory",
                    "",
                    "Green-ish / potentially cherry-pickable:",
                    "",
                    "* #1459 - 3+/3-, 3 files, green",
                    "* #1357 - 40+/34-, 2 files, green",
                    "",
                    "Large or suspicious despite green:",
                    "",
                    "* #1454 - 1507+/2857-, 5 files, green but too large for casual merge",
                    "",
                    "Failing and likely close/supersede unless valuable:",
                    "",
                    "* #1457, #1456, #1455",
                    "",
                    "## Acceptance",
                    "",
                    "* Decision notes are posted in this Linear issue or linked follow-up comments.",
                ]
            ),
            "acceptance_criteria": [
                "Decision notes are posted in this Linear issue or linked follow-up comments.",
            ],
        }

        summary = _linear_comment_task_summary(task)

        self.assertIn("#1459: cherry-pick", summary)
        self.assertIn("#1357: cherry-pick", summary)
        self.assertIn("#1454: needs-manual-review", summary)
        self.assertIn("#1457: close", summary)
        self.assertIn("#1456: close", summary)
        self.assertIn("No repository changes, commit, push, or PR were expected", summary)

    def test_worker_results_are_deduplicated_by_subtask(self) -> None:
        worker_results: list[dict[str, object]] = []
        blackboard = {"workers": []}
        first = {
            "worker_id": "worker-1",
            "subtask_id": "subtask-1",
            "title": "cleanup - slice 1",
            "status": "failed",
        }
        second = {
            "worker_id": "worker-1",
            "subtask_id": "subtask-1",
            "title": "cleanup - slice 1",
            "status": "completed",
        }

        _record_worker_result(blackboard, worker_results, first)
        _record_worker_result(blackboard, worker_results, second)

        self.assertEqual(len(worker_results), 1)
        self.assertEqual(len(blackboard["workers"]), 1)
        self.assertEqual(worker_results[0]["status"], "completed")
        self.assertEqual(blackboard["workers"][0]["status"], "completed")

    def test_collect_worker_changed_files_dedupes_and_rejects_parent_paths(self) -> None:
        files = _collect_worker_changed_files(
            [
                {"changed_files": ["./src/app.ts", "src/app.ts", "../outside.txt", "__aca_temp_probe.txt"]},
                {"changed_files": ["packages/panel/src/App.tsx", "safe/../unsafe.ts"]},
            ]
        )

        self.assertEqual(files, ["src/app.ts", "packages/panel/src/App.tsx"])

    def test_retryable_worker_failure_builds_repair_feedback(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-1",
                    "subtask_id": "subtask-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "worker_incomplete_diff",
                    "changed_files": ["crates/tandem-tools/tests/registry_resolution.rs"],
                    "stdout": "Remaining blockers: path sandbox and approval classifier tests are missing.",
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 0)

        self.assertIsNotNone(feedback)
        self.assertIn("retryable blocker `worker_incomplete_diff`", feedback or "")
        self.assertIn("crates/tandem-tools/tests/registry_resolution.rs", feedback or "")
        self.assertIn("unmet acceptance criteria", feedback or "")

    def test_partial_diff_artifact_keeps_worker_output_excerpt(self) -> None:
        artifacts = _partial_diff_artifacts_for_retry(
            [
                {
                    "worker_id": "worker-1",
                    "subtask_id": "subtask-1",
                    "partial_diff_artifact": "/runs/run-1/artifacts/worker-1.patch",
                    "changed_files": ["crates/eval/src/scoring.rs", "__aca_temp_probe.txt"],
                    "stdout": "Remaining implementation blockers: missing passes() method.",
                }
            ]
        )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(
            artifacts[0]["worker_output_excerpt"],
            "Remaining implementation blockers: missing passes() method.",
        )
        self.assertEqual(artifacts[0]["changed_files"], ["crates/eval/src/scoring.rs"])

    def test_partial_diff_retry_artifacts_accumulate_across_attempts(self) -> None:
        existing = [
            {
                "subtask_id": "subtask-1",
                "worker_id": "worker-1",
                "patch_path": "/runs/run-1/artifacts/source-only.patch",
                "changed_files": ["src/tandem_agents/config/config_loader.py"],
            }
        ]
        new_artifacts = [
            {
                "subtask_id": "subtask-1",
                "worker_id": "worker-1",
                "patch_path": "/runs/run-1/artifacts/test-only.patch",
                "changed_files": ["src/tandem_agents/config/config_loader_test.py"],
            },
            {
                "subtask_id": "subtask-1",
                "worker_id": "worker-1",
                "patch_path": "/runs/run-1/artifacts/source-only.patch",
                "changed_files": ["src/tandem_agents/config/config_loader.py"],
            },
        ]

        artifacts = _merge_partial_diff_artifacts_for_retry(existing, new_artifacts)

        self.assertEqual(
            [artifact["patch_path"] for artifact in artifacts],
            [
                "/runs/run-1/artifacts/source-only.patch",
                "/runs/run-1/artifacts/test-only.patch",
            ],
        )
        self.assertEqual(
            [artifact["changed_files"] for artifact in artifacts],
            [
                ["src/tandem_agents/config/config_loader.py"],
                ["src/tandem_agents/config/config_loader_test.py"],
            ],
        )

    def test_worker_no_progress_builds_base_retry_feedback(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-2",
                    "subtask_id": "subtask-2",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "Worker produced no terminal result within 300s.",
                    "blocker_kind": "worker_no_progress",
                    "stdout": "Worker produced no terminal result within 300s.",
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 0)

        self.assertIsNotNone(feedback)
        self.assertIn("retryable blocker `worker_no_progress`", feedback or "")
        self.assertTrue(
            _worker_failure_can_retry(
                SimpleNamespace(env={}),
                blocker,
                attempt=0,
                base_max_loops=2,
            )
        )
        self.assertTrue(
            _worker_failure_can_retry(
                SimpleNamespace(env={}),
                blocker,
                attempt=2,
                base_max_loops=2,
            )
        )
        self.assertFalse(
            _worker_failure_can_retry(
                SimpleNamespace(env={}),
                blocker,
                attempt=3,
                base_max_loops=2,
            )
        )

    def test_worker_no_change_guard_failure_is_not_retryable(self) -> None:
        cfg = SimpleNamespace(env={})
        for failure_reason in ("WORKER_NO_CHANGE", "WORKER_REPAIR_NO_CHANGE"):
            blocker = {
                "kind": "worker_no_progress",
                "worker": {
                    "failure_reason": failure_reason,
                },
            }

            self.assertFalse(
                _worker_failure_can_retry(
                    cfg,
                    blocker,
                    attempt=0,
                    base_max_loops=2,
                )
            )

    def test_deferred_subtasks_for_retry_keeps_unstarted_serial_tail(self) -> None:
        pending = [
            {"id": "subtask-1", "title": "One"},
            {"id": "subtask-2", "title": "Two"},
            {"id": "subtask-3", "title": "Three"},
        ]
        results = [{"subtask_id": "subtask-1", "returncode": 1}]

        deferred = _deferred_subtasks_for_retry(pending, results)

        self.assertEqual([item["id"] for item in deferred], ["subtask-2", "subtask-3"])
        self.assertIsNot(deferred[0], pending[1])

    def test_unproductive_diff_does_not_get_extra_partial_diff_retries(self) -> None:
        cfg = SimpleNamespace(env={})
        blocker = {
            "kind": "worker_unproductive_diff",
            "worker": {
                "partial_diff_artifact": "/runs/run-1/artifacts/worker-1.patch",
            },
        }

        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=0, base_max_loops=2))
        self.assertFalse(_worker_failure_can_retry(cfg, blocker, attempt=1, base_max_loops=2))

    def test_carry_forward_patch_apply_failure_is_retryable_with_discard_feedback(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-1",
                    "subtask_id": "subtask-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "CARRY_FORWARD_PATCH_APPLY_FAILED",
                    "blocker_kind": "carry_forward_patch_apply_failed",
                    "stdout": "ACA could not apply the preserved partial worker diff before retry.",
                    "recovery_action": "Discard this preserved patch for the next repair attempt.",
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 0)

        self.assertEqual(blocker["kind"], "carry_forward_patch_apply_failed")
        self.assertIn("could not apply preserved partial diff", blocker["phase_detail"])
        self.assertIsNotNone(feedback)
        self.assertIn("carry_forward_patch_apply_failed", feedback or "")
        self.assertIn("Discard this preserved patch", feedback or "")
        self.assertTrue(_worker_failure_can_retry(SimpleNamespace(env={}), blocker, attempt=0, base_max_loops=2))

    def test_incomplete_diff_gets_two_extra_worker_repair_loops_by_default(self) -> None:
        cfg = SimpleNamespace(env={})
        blocker = {"kind": "worker_incomplete_diff"}

        self.assertEqual(_worker_incomplete_diff_extra_retries(cfg), 2)
        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=0, base_max_loops=2))
        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=1, base_max_loops=2))
        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=2, base_max_loops=2))
        self.assertFalse(_worker_failure_can_retry(cfg, blocker, attempt=3, base_max_loops=2))

    def test_engine_timeout_with_partial_diff_gets_extra_repair_budget(self) -> None:
        cfg = SimpleNamespace(env={})
        blocker = {
            "kind": "engine_prompt_timeout",
            "worker": {
                "partial_diff_artifact": "/runs/run-1/artifacts/worker-1.partial-worker-diff.patch",
            },
        }

        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=0, base_max_loops=2))
        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=1, base_max_loops=2))
        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=2, base_max_loops=2))
        self.assertFalse(_worker_failure_can_retry(cfg, blocker, attempt=3, base_max_loops=2))
        self.assertFalse(
            _worker_failure_can_retry(
                cfg,
                {"kind": "engine_prompt_timeout", "worker": {}},
                attempt=1,
                base_max_loops=2,
            )
        )

    def test_engine_timeout_retry_feedback_compacts_preserved_patch_boilerplate(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-1",
                    "subtask_id": "subtask-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                    "blocker_kind": "engine_prompt_timeout",
                    "changed_files": ["src/tandem_agents/api/worktree_isolation.py"],
                    "partial_diff_artifact": "/runs/run-1/artifacts/worker-1.partial-worker-diff.patch",
                    "stdout": (
                        "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt did not finish within 300s.\n"
                        "ACA preserved this partial worker diff because the Tandem engine stalled before a terminal response.\n"
                        "The partial diff is not treated as a completed worker result; retry or block with this evidence.\n"
                    ),
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 1)

        self.assertIsNotNone(feedback)
        self.assertIn("retryable blocker `engine_prompt_timeout`", feedback or "")
        self.assertIn("ENGINE_PROMPT_TIMEOUT", feedback or "")
        self.assertIn("Preserved partial patch", feedback or "")
        self.assertNotIn("not treated as a completed worker result", feedback or "")

    def test_worker_off_track_builds_retry_feedback(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-1",
                    "subtask_id": "subtask-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                    "blocker_kind": "worker_off_track",
                    "changed_files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                    "partial_diff_artifact": "/runs/run-1/artifacts/worker-1.patch",
                    "stdout": "Required regression test file was not touched.",
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 0)

        self.assertIsNotNone(feedback)
        self.assertEqual(blocker["kind"], "worker_off_track")
        self.assertIn("retryable blocker `worker_off_track`", feedback or "")
        self.assertTrue(_worker_failure_can_retry(SimpleNamespace(env={}), blocker, attempt=0, base_max_loops=2))

    def test_worker_runaway_diff_builds_retry_feedback(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-2",
                    "subtask_id": "subtask-2",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "WORKER_RUNAWAY_DIFF",
                    "blocker_kind": "worker_runaway_diff",
                    "changed_files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                    "partial_diff_artifact": "/runs/run-1/artifacts/worker-2.patch",
                    "stdout": "Diff exceeded runaway guard.",
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 0)

        self.assertIsNotNone(feedback)
        self.assertEqual(blocker["kind"], "worker_runaway_diff")
        self.assertIn("retryable blocker `worker_runaway_diff`", feedback or "")
        self.assertTrue(_worker_failure_can_retry(SimpleNamespace(env={}), blocker, attempt=0, base_max_loops=2))

    def test_worker_unproductive_diff_builds_retry_feedback(self) -> None:
        ctx = SimpleNamespace(
            worker_results=[
                {
                    "worker_id": "worker-3",
                    "subtask_id": "subtask-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "WORKER_UNPRODUCTIVE_DIFF",
                    "blocker_kind": "worker_unproductive_diff",
                    "changed_files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                    "partial_diff_artifact": "/runs/run-1/artifacts/worker-3.patch",
                    "stdout": "Worker produced a comment-only placeholder diff.",
                }
            ]
        )
        blocker = _worker_failure_blocker(ctx.worker_results)

        feedback = _worker_failure_retry_feedback(ctx, blocker, 0)

        self.assertIsNotNone(feedback)
        self.assertEqual(blocker["kind"], "worker_unproductive_diff")
        self.assertIn("retryable blocker `worker_unproductive_diff`", feedback or "")
        self.assertTrue(_worker_failure_can_retry(SimpleNamespace(env={}), blocker, attempt=0, base_max_loops=2))

    def test_extra_worker_repair_loop_is_limited_to_incomplete_diffs(self) -> None:
        cfg = SimpleNamespace(env={})

        self.assertFalse(
            _worker_failure_can_retry(
                cfg,
                {"kind": "worker_no_diff"},
                attempt=1,
                base_max_loops=2,
            )
        )

    def test_corrupt_diff_can_use_bounded_extra_repair_budget(self) -> None:
        cfg = SimpleNamespace(env={})
        blocker = {"kind": "worker_corrupt_diff"}

        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=1, base_max_loops=2))
        self.assertTrue(_worker_failure_can_retry(cfg, blocker, attempt=2, base_max_loops=2))
        self.assertFalse(_worker_failure_can_retry(cfg, blocker, attempt=3, base_max_loops=2))

    def test_incomplete_diff_extra_retry_budget_can_be_disabled(self) -> None:
        cfg = SimpleNamespace(env={"ACA_WORKER_INCOMPLETE_DIFF_EXTRA_RETRIES": "0"})

        self.assertEqual(_worker_incomplete_diff_extra_retries(cfg), 0)
        self.assertFalse(
            _worker_failure_can_retry(
                cfg,
                {"kind": "worker_incomplete_diff"},
                attempt=1,
                base_max_loops=2,
            )
        )

    def test_verification_can_use_incomplete_diff_extra_repair_budget(self) -> None:
        cfg = SimpleNamespace(env={})
        ctx = SimpleNamespace(
            status={
                "repair": {
                    "extra_retry_source": "worker_incomplete_diff",
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                    "partial_diff_state": "preserved_not_accepted",
                }
            },
            blackboard={},
        )

        self.assertTrue(_verification_can_retry(cfg, ctx, attempt=1, base_max_loops=2))
        self.assertTrue(_verification_can_retry(cfg, ctx, attempt=2, base_max_loops=2))
        self.assertFalse(_verification_can_retry(cfg, ctx, attempt=3, base_max_loops=2))

    def test_integration_can_use_incomplete_diff_extra_repair_budget(self) -> None:
        cfg = SimpleNamespace(env={})
        ctx = SimpleNamespace(
            status={
                "repair": {
                    "extra_retry_source": "worker_incomplete_diff",
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                    "partial_diff_state": "preserved_not_accepted",
                }
            },
            blackboard={},
        )

        self.assertTrue(_integration_can_retry(cfg, ctx, attempt=1, base_max_loops=2))
        self.assertTrue(_integration_can_retry(cfg, ctx, attempt=2, base_max_loops=2))
        self.assertTrue(_integration_can_retry(cfg, ctx, attempt=3, base_max_loops=2))
        self.assertFalse(_integration_can_retry(cfg, ctx, attempt=4, base_max_loops=2))

    def test_integration_prompt_timeout_uses_watchdog_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={"ACA_INTEGRATION_PROMPT_TIMEOUT_SECONDS": "0.1"}),
                run_id="run-1",
                task={"title": "Task", "description": "Task description"},
                worker_results=[],
                repo_path=root,
                layout={"logs": logs},
            )

            @contextmanager
            def heartbeat(*_args, **_kwargs):
                yield

            def slow_stream(*_args, **_kwargs):
                time.sleep(0.5)
                return {"returncode": 0, "stdout": '{"approved":true}'}

            with patch(
                "src.tandem_agents.core.execution.runner_core.engine_session_provider_model",
                return_value={"provider": "openai-codex", "model": "gpt-5.5"},
            ), patch(
                "src.tandem_agents.core.execution.runner_core._role_provider_override_config",
            ), patch(
                "src.tandem_agents.core.execution.runner_core.engine_env",
                return_value={},
            ), patch(
                "src.tandem_agents.core.execution.runner_core._coordination_heartbeat",
                side_effect=heartbeat,
            ), patch(
                "src.tandem_agents.core.execution.runner_core.stream_tandem_prompt",
                side_effect=slow_stream,
            ):
                result = _run_integration_prompt(ctx)

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
            self.assertTrue(_integration_failure_can_defer_to_review(result))
            self.assertIn("ACA integration prompt exceeded 0.1s", result["stdout"])
            self.assertIn("ACA integration prompt exceeded 0.1s", (logs / "manager-integration.log").read_text())

    def test_integration_prompt_timeout_ignores_invalid_env(self) -> None:
        cfg = SimpleNamespace(env={"ACA_INTEGRATION_PROMPT_TIMEOUT_SECONDS": "not-a-number"})

        self.assertEqual(_integration_prompt_timeout_seconds(cfg), 300.0)

    def test_verification_extra_repair_budget_requires_incomplete_diff_state(self) -> None:
        cfg = SimpleNamespace(env={})
        ctx = SimpleNamespace(status={"repair": {}}, blackboard={})

        self.assertFalse(_verification_can_retry(cfg, ctx, attempt=1, base_max_loops=2))
        self.assertFalse(_integration_can_retry(cfg, ctx, attempt=1, base_max_loops=2))

    def test_discard_partial_diff_repair_artifacts_marks_stale_timeout_patch(self) -> None:
        ctx = SimpleNamespace(
            status={
                "repair": {
                    "extra_retry_source": "worker_incomplete_diff",
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                    "partial_diff_state": "preserved_not_accepted",
                }
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                    "partial_diff_state": "preserved_not_accepted",
                }
            },
        )

        _discard_partial_diff_repair_artifacts(ctx, reason="integration rejected helper-only patch")

        for source in (ctx.status, ctx.blackboard):
            repair = source["repair"]
            self.assertNotIn("partial_diff_artifacts", repair)
            self.assertEqual(repair["partial_diff_state"], "discarded_after_integration_rejection")
            self.assertEqual(repair["partial_diff_discard_reason"], "integration rejected helper-only patch")
            self.assertEqual(
                repair["discarded_partial_diff_artifacts"][0]["patch_path"],
                "/runs/run-1/artifacts/worker.patch",
            )
        self.assertTrue(_verification_can_retry(SimpleNamespace(env={}), ctx, attempt=2, base_max_loops=2))

    def test_retryable_worker_failure_collects_partial_diff_artifact(self) -> None:
        artifacts = _partial_diff_artifacts_for_retry(
            [
                {
                    "worker_id": "worker-1",
                    "subtask_id": "subtask-1",
                    "partial_diff_artifact": "/runs/run-1/artifacts/worker-1.partial-worker-diff.patch",
                    "changed_files": ["./crates/eval/src/scoring.rs"],
                },
                {
                    "worker_id": "worker-2",
                    "subtask_id": "subtask-2",
                    "artifacts": {"partial_diff": "/runs/run-1/artifacts/worker-2.partial-worker-diff.patch"},
                },
                {"worker_id": "worker-3", "subtask_id": "subtask-3"},
            ]
        )

        self.assertEqual(
            artifacts,
            [
                {
                    "subtask_id": "subtask-1",
                    "worker_id": "worker-1",
                    "patch_path": "/runs/run-1/artifacts/worker-1.partial-worker-diff.patch",
                    "changed_files": ["crates/eval/src/scoring.rs"],
                },
                {
                    "subtask_id": "subtask-2",
                    "worker_id": "worker-2",
                    "patch_path": "/runs/run-1/artifacts/worker-2.partial-worker-diff.patch",
                },
            ],
        )

    def test_retryable_worker_failure_collects_completed_subtasks(self) -> None:
        completed = _completed_subtask_ids_for_retry(
            [
                {"worker_id": "worker-1", "subtask_id": "subtask-1", "status": "completed"},
                {"worker_id": "worker-2", "subtask_id": "subtask-2", "status": "failed"},
                {"worker_id": "worker-3", "subtask_id": "subtask-3", "verified_existing": True},
                {"worker_id": "worker-4", "subtask_id": "subtask-1", "status": "completed"},
            ]
        )

        self.assertEqual(completed, ["subtask-1", "subtask-3"])

    def test_integration_blocker_detects_semantic_failure_with_zero_exit(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Duplicate findLast import remains.",'
                '"blockers":["missing PR"]}'
            ),
        }

        message = _integration_blocker_message(result)

        self.assertIsNotNone(message)
        self.assertIn("Integration review did not approve", message or "")
        self.assertIn("Duplicate findLast import remains", message or "")
        self.assertIn("missing PR", message or "")

    def test_integration_event_type_marks_zero_exit_semantic_blocker_failed(self) -> None:
        blocked = {
            "returncode": 0,
            "stdout": '{"status":"blocked","approved":false,"summary":"No usable diff."}',
        }
        ok = {"returncode": 0, "stdout": '{"status":"approved","approved":true}'}
        failed = {"returncode": 1, "stdout": "integration command failed"}

        self.assertEqual(_integration_event_type(blocked), "manager.failed")
        self.assertEqual(_integration_event_type(ok), "manager.completed")
        self.assertEqual(_integration_event_type(failed), "manager.failed")

    def test_integration_engine_watchdog_failure_defers_to_review(self) -> None:
        result = {
            "returncode": 1,
            "stdout": "ENGINE_TOOL_LOOP_STALLED: engine stalled after tool activity.",
        }

        self.assertTrue(_integration_failure_can_defer_to_review(result))

    def test_integration_plain_failure_does_not_defer_to_review(self) -> None:
        result = {
            "returncode": 1,
            "stdout": "integration command failed",
        }

        self.assertFalse(_integration_failure_can_defer_to_review(result))

    def test_integration_sandbox_inspection_blocker_defers_to_review(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Could not inspect repository status/diff because sandbox blocked git.",'
                '"tests":["Not run: git/status commands were blocked by bubblewrap_not_available."]}'
            ),
        }
        blocker = _integration_blocker_message(result) or ""

        self.assertTrue(_integration_semantic_blocker_can_defer_to_review(result, blocker))

    def test_integration_placeholder_output_does_not_defer(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Worker output only added crates/tandem-tools/tests/unit_suite_placeholder.rs '
                'containing a placeholder comment. The requested tandem-tools unit suite was not implemented.",'
                '"risks":["Acceptance criteria unmet: no table-driven tests were added."],'
                '"tests":["Not run; worker reported verification was not run."]}'
            ),
        }
        blocker = _integration_blocker_message(result) or ""

        self.assertFalse(_integration_semantic_blocker_can_defer_to_review(result, blocker))

    def test_integration_concrete_code_finding_does_not_defer(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Duplicate findLast import remains.",'
                '"required_fixes":["Remove the duplicate import."]}'
            ),
        }
        blocker = _integration_blocker_message(result) or ""

        self.assertFalse(_integration_semantic_blocker_can_defer_to_review(result, blocker))

    def test_preserve_and_reset_blocked_worktree_cleans_synced_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            artifacts = root / "artifacts"
            artifacts.mkdir()
            repo.mkdir()
            run_command(["git", "init", "--initial-branch=main", str(repo)])
            (repo / "README.md").write_text("before\n", encoding="utf-8")
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem.invalid", "add", "README.md"])
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem.invalid", "commit", "-m", "init"])
            (repo / "README.md").write_text("after\n", encoding="utf-8")
            (repo / "scratch.txt").write_text("temp\n", encoding="utf-8")
            ctx = SimpleNamespace(
                repo_path=repo,
                cfg=SimpleNamespace(env={}),
                layout={"artifacts": artifacts},
                blackboard={},
            )

            _preserve_and_reset_blocked_worktree(ctx, reason="test")

            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "before\n")
            self.assertFalse((repo / "scratch.txt").exists())
            patch_text = (artifacts / "blocked-working-diff.patch").read_text(encoding="utf-8")
            self.assertIn("-before", patch_text)
            self.assertIn("+after", patch_text)
            self.assertIn("blocked_worktree_cleanup", ctx.blackboard)

    def test_preserve_and_reset_worktree_supports_retry_artifact_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            artifacts = root / "artifacts"
            artifacts.mkdir()
            repo.mkdir()
            run_command(["git", "init", "--initial-branch=main", str(repo)])
            (repo / "README.md").write_text("before\n", encoding="utf-8")
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem.invalid", "add", "README.md"])
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem.invalid", "commit", "-m", "init"])
            (repo / "README.md").write_text("rejected\n", encoding="utf-8")
            ctx = SimpleNamespace(
                repo_path=repo,
                cfg=SimpleNamespace(env={}),
                layout={"artifacts": artifacts},
                blackboard={},
            )

            patch_path = _preserve_and_reset_blocked_worktree(
                ctx,
                reason="verification_retry_review_repair_needed",
                artifact_name="retry-2-rejected-working-diff.patch",
            )

            self.assertEqual(patch_path, artifacts / "retry-2-rejected-working-diff.patch")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "before\n")
            patch_text = patch_path.read_text(encoding="utf-8")
            self.assertIn("-before", patch_text)
            self.assertIn("+rejected", patch_text)
            cleanup = ctx.blackboard["blocked_worktree_cleanup"][0]
            self.assertEqual(cleanup["reason"], "verification_retry_review_repair_needed")
            self.assertEqual(cleanup["patch_path"], str(patch_path))

    def test_manager_prompt_includes_previous_feedback_for_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: Repair flow
                    repository:
                      slug: frumu-ai/example
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            task = {"title": "Repair flow", "description": "Fix verification failures"}
            repo = {"path": "/tmp/repo"}
            prompt = build_manager_prompt(
                "run-1",
                task,
                repo,
                cfg,
                repo_context="src/app.py",
                previous_feedback="Reviewer Feedback:\nplease fix the tests",
            )

            self.assertIn("Reviewer Feedback:", prompt)
            self.assertIn("please fix the tests", prompt)

    def test_runner_records_coding_run_contract_in_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blackboard: dict[str, object] = {}
            contract = build_coding_run_contract(
                run_id="run-3",
                task={"title": "Fix README", "source": {"type": "github_project"}},
                repo_path=root,
                branch_name="aca/example/fix-readme-run-3",
                expected_repo_files=["README.md"],
            )

            _record_coding_run_contract(blackboard, contract)
            _record_coding_run_contract(blackboard, contract)

            self.assertIn("coding_run_contract", blackboard)
            self.assertEqual(blackboard["coding_run_contract"]["handoff_mode"], "code_edit")
            self.assertIn("Coding run contract: diff review and minimal verification are required before handoff.", blackboard["notes"])
            self.assertEqual(
                blackboard["notes"].count(
                    "Coding run contract: diff review and minimal verification are required before handoff."
                ),
                1,
            )

    def test_runner_records_review_policy_in_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: Review policy
                    repository:
                      slug: frumu-ai/example
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    review:
                      policy: human_review
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            blackboard: dict[str, object] = {}

            _record_review_policy(blackboard, cfg)

            self.assertIn("review_policy", blackboard)
            self.assertTrue(blackboard["review_policy"]["human_review_required"])
            self.assertIn("human review gate required before merge.", blackboard["notes"][0].lower())

    def test_local_worker_pool_returns_completed_results_in_completion_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: Parallel work
                    repository:
                      slug: frumu-ai/example
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            repo_path = root / "repo"
            run_dir = root / "runs" / "run-1"
            repo_path.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            task = {"title": "Parallel work", "description": "exercise the worker pool"}
            pending_subtasks = [
                {"id": "subtask-1", "title": "slow", "goal": "slow", "write_required": True},
                {"id": "subtask-2", "title": "fast", "goal": "fast", "write_required": True},
            ]
            call_order: list[str] = []

            def fake_worker_runner(
                _cfg,
                _run_id,
                _repo_path,
                _run_dir,
                _task,
                subtask,
                worker_id,
                index,
            ):
                call_order.append(worker_id)
                if worker_id == "worker-1":
                    time.sleep(0.15)
                return {
                    "worker_id": worker_id,
                    "subtask_index": index,
                    "subtask_id": subtask["id"],
                    "title": subtask["title"],
                    "status": "completed",
                    "returncode": 0,
                    "worktree": str(repo_path),
                    "log_path": "",
                    "output_excerpt": worker_id,
                    "write_required": True,
                    "verified_existing": False,
                }

            results = _execute_local_worker_pool(
                cfg,
                "run-1",
                repo_path,
                run_dir,
                task,
                pending_subtasks,
                2,
                worker_runner=fake_worker_runner,
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["worker_id"], "worker-2")
            self.assertCountEqual(call_order, ["worker-1", "worker-2"])

    def test_parallel_worker_pool_honors_abort_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "runs" / "run-1"
            repo_path.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            pending_subtasks = [
                {"id": "subtask-1", "title": "first", "goal": "first", "write_required": True},
                {"id": "subtask-2", "title": "second", "goal": "second", "write_required": True},
            ]
            abort_calls: list[str] = []

            def fake_worker_runner(
                _cfg,
                _run_id,
                _repo_path,
                _run_dir,
                _task,
                subtask,
                worker_id,
                index,
            ):
                time.sleep(0.2)
                return {
                    "worker_id": worker_id,
                    "subtask_index": index,
                    "subtask_id": subtask["id"],
                    "title": subtask["title"],
                    "status": "completed",
                    "returncode": 0,
                    "worktree": str(repo_path),
                    "log_path": "",
                    "output_excerpt": worker_id,
                    "write_required": True,
                    "verified_existing": False,
                }

            def abort_result(index: int, subtask: dict[str, object], worker_id: str) -> dict[str, object]:
                abort_calls.append(worker_id)
                return {
                    "worker_id": worker_id,
                    "subtask_index": index,
                    "subtask_id": subtask["id"],
                    "title": subtask["title"],
                    "status": "failed",
                    "returncode": 1,
                    "worktree": "",
                    "log_path": "",
                    "output_excerpt": "Worker aborted by heartbeat guard.",
                    "blocker_kind": "worker_unproductive",
                    "write_required": True,
                    "verified_existing": False,
                }

            started = time.monotonic()
            results = _execute_local_worker_pool(
                resolve_config(root),
                "run-1",
                repo_path,
                run_dir,
                {"title": "Parallel work"},
                pending_subtasks,
                2,
                worker_runner=fake_worker_runner,
                abort_result=abort_result,
                worker_timeout_seconds=5,
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.15)
            self.assertEqual(len(results), 2)
            self.assertCountEqual([result["worker_id"] for result in results], ["worker-1", "worker-2"])
            self.assertTrue(all(result["blocker_kind"] == "worker_unproductive" for result in results))
            self.assertCountEqual(abort_calls, ["worker-1", "worker-2"])

    def test_serial_worker_pool_stops_after_write_required_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "runs" / "run-1"
            repo_path.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            pending_subtasks = [
                {"id": "subtask-1", "title": "first", "goal": "first", "write_required": True},
                {"id": "subtask-2", "title": "second", "goal": "second", "write_required": True},
            ]
            call_order: list[str] = []

            def fake_worker_runner(
                _cfg,
                _run_id,
                _repo_path,
                _run_dir,
                _task,
                subtask,
                worker_id,
                index,
            ):
                call_order.append(worker_id)
                return {
                    "worker_id": worker_id,
                    "subtask_index": index,
                    "subtask_id": subtask["id"],
                    "title": subtask["title"],
                    "status": "failed",
                    "returncode": 1,
                    "worktree": str(repo_path),
                    "log_path": "",
                    "output_excerpt": "timed out with partial diff",
                    "write_required": True,
                    "verified_existing": False,
                    "blocker_kind": "worker_incomplete_diff",
                }

            results = _execute_local_worker_pool(
                resolve_config(root),
                "run-1",
                repo_path,
                run_dir,
                {"title": "Serial work"},
                pending_subtasks,
                1,
                worker_runner=fake_worker_runner,
            )

            self.assertEqual([result["worker_id"] for result in results], ["worker-1"])
            self.assertEqual(call_order, ["worker-1"])


if __name__ == "__main__":
    unittest.main()
