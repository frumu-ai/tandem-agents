from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.phases.worker_dispatch import (
    _active_worker_engine_silence_summary,
    _baseline_file_state,
    _cancel_active_worker_engine_session,
    _changed_files_satisfy_required_test_files,
    _changed_files_satisfy_primary_source_target,
    _changed_python_test_modules,
    _changed_python_tests_result,
    _changed_files_scoped_to_subtask,
    _diff_add_delete_counts,
    _filter_diff_text_to_files,
    _diff_has_substantive_required_test_addition,
    _diff_required_tests_missing_added_public_symbols,
    _diff_is_destructive_rewrite,
    _filter_result_partial_diff_artifact,
    _fresh_changed_files_since_baseline,
    _latest_worker_retry_write_required,
    _messages_have_assistant_or_tool_activity,
    _one_sided_guard_elapsed_seconds,
    _validation_changed_files_with_carried_baseline,
    _effective_worker_repair_no_change_abort_seconds,
    _effective_worker_test_only_diff_abort_seconds,
    _effective_worker_testless_diff_abort_seconds,
    _effective_worker_no_change_abort_seconds,
    _failed_result_has_reviewable_production_diff,
    _failed_result_has_reviewable_source_and_test_diff,
    _attach_carried_partial_diff_to_repair_no_change_result,
    _reviewable_failed_diff_rejection,
    _subtask_has_required_test_only_diff,
    _subtask_has_verifiable_source_and_test_diff,
    _subtask_requires_paired_source_test_diff,
    _subtask_required_test_files,
    _subtask_is_no_change_guard_candidate,
    _subtask_is_repair_no_change_guard_candidate,
    _tool_loop_summary_from_messages,
    _worktree_has_subtask_changes,
    _worker_no_change_abort_seconds,
    _worker_paired_source_test_no_change_abort_seconds,
    _worker_progress_snapshot_sleep_seconds,
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

    def test_effective_no_change_timeout_waits_for_async_no_text_classification(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "90",
                    "ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS": "180",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                }
            )
        )

        timeout = _effective_worker_no_change_abort_seconds(
            ctx,
            {"id": "subtask-1", "files": ["src/app.py"], "write_required": True},
            "worker-1",
        )

        self.assertEqual(timeout, 240.0)

    def test_effective_no_change_timeout_waits_for_prompt_sync_first_budget(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS": "300",
                    "ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS": "480",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                }
            )
        )

        timeout = _effective_worker_no_change_abort_seconds(
            ctx,
            {
                "id": "fallback-throughput-repair",
                "files": ["src/app.py"],
                "write_required": True,
            },
            "worker-1",
        )

        self.assertEqual(timeout, 360.0)

    def test_one_sided_guard_elapsed_tracks_age_from_first_seen(self) -> None:
        state: dict[str, dict[str, float]] = {}

        self.assertEqual(
            _one_sided_guard_elapsed_seconds(state, "worker-1", "testless", True, 100.0),
            0.0,
        )
        self.assertEqual(
            _one_sided_guard_elapsed_seconds(state, "worker-1", "testless", True, 112.5),
            12.5,
        )
        self.assertEqual(state, {"worker-1": {"testless": 100.0}})

    def test_one_sided_guard_elapsed_resets_when_shape_is_no_longer_active(self) -> None:
        state = {"worker-1": {"testless": 100.0, "test_only": 105.0}}

        self.assertEqual(
            _one_sided_guard_elapsed_seconds(state, "worker-1", "testless", False, 130.0),
            0.0,
        )
        self.assertEqual(state, {"worker-1": {"test_only": 105.0}})
        self.assertEqual(
            _one_sided_guard_elapsed_seconds(state, "worker-1", "test_only", False, 131.0),
            0.0,
        )
        self.assertEqual(state, {})

    def test_paired_source_test_no_change_timeout_defaults_and_ignores_invalid_env(self) -> None:
        self.assertEqual(
            _worker_paired_source_test_no_change_abort_seconds(SimpleNamespace(cfg=SimpleNamespace(env={}))),
            150.0,
        )
        self.assertEqual(
            _worker_paired_source_test_no_change_abort_seconds(
                SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS": "45"}))
            ),
            45.0,
        )
        self.assertEqual(
            _worker_paired_source_test_no_change_abort_seconds(
                SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS": "bad"}))
            ),
            150.0,
        )

    def test_effective_no_change_timeout_caps_paired_source_test_subtasks(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS": "300",
                    "ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS": "480",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                    "ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS": "75",
                }
            )
        )

        timeout = _effective_worker_no_change_abort_seconds(
            ctx,
            {
                "id": "subtask-1",
                "files": ["src/app.py", "src/app_test.py"],
                "target_files": ["src/app.py", "src/app_test.py"],
                "write_required": True,
                "acceptance_criteria": ["Add regression test coverage for repository isolation."],
            },
            "worker-1",
        )

        self.assertEqual(timeout, 450.0)

    def test_effective_no_change_timeout_can_disable_paired_source_test_cap(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS": "300",
                    "ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS": "480",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                    "ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS": "0",
                }
            )
        )

        timeout = _effective_worker_no_change_abort_seconds(
            ctx,
            {
                "id": "subtask-1",
                "files": ["src/app.py", "src/app_test.py"],
                "target_files": ["src/app.py", "src/app_test.py"],
                "write_required": True,
                "acceptance_criteria": ["Add regression test coverage for repository isolation."],
            },
            "worker-1",
        )

        self.assertEqual(timeout, 240.0)

    def test_effective_repair_no_change_timeout_caps_paired_source_test_repairs(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS": "180",
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS": "300",
                    "ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS": "480",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                    "ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS": "75",
                }
            )
        )

        timeout = _effective_worker_repair_no_change_abort_seconds(
            ctx,
            {
                "id": "fallback-repository-isolation-part-1",
                "files": ["src/app.py", "src/app_test.py"],
                "target_files": ["src/app.py", "src/app_test.py"],
                "deterministic_testless_repair": True,
                "write_required": True,
                "acceptance_criteria": ["Add regression test coverage for repository isolation."],
            },
            "worker-1",
        )

        self.assertEqual(timeout, 450.0)

    def test_effective_no_change_timeout_keeps_larger_operator_override(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "600",
                    "ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS": "180",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                }
            )
        )

        timeout = _effective_worker_no_change_abort_seconds(
            ctx,
            {"id": "subtask-1", "files": ["src/app.py"], "write_required": True},
            "worker-1",
        )

        self.assertEqual(timeout, 600.0)

    def test_effective_testless_diff_timeout_uses_content_guard_budget(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS": "120",
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS": "180",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                }
            )
        )

        timeout = _effective_worker_testless_diff_abort_seconds(
            ctx,
            {
                "id": "subtask-1",
                "files": ["src/app.py", "src/app_test.py"],
                "write_required": True,
            },
            "worker-1",
        )

        self.assertEqual(timeout, 120.0)

    def test_effective_test_only_diff_timeout_uses_test_only_guard_budget(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS": "120",
                    "ACA_WORKER_TEST_ONLY_DIFF_ABORT_SECONDS": "180",
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS": "180",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                }
            )
        )

        timeout = _effective_worker_test_only_diff_abort_seconds(
            ctx,
            {
                "id": "subtask-1",
                "files": ["src/app.py", "src/app_test.py"],
                "target_files": ["src/app.py", "src/app_test.py"],
                "repair_requires_production_followup": ["src/app.py"],
                "acceptance_criteria": [
                    "Make the first new repair edit in the required production file.",
                ],
                "write_required": True,
            },
            "worker-1",
        )

        self.assertEqual(timeout, 45.0)

    def test_paired_repair_one_sided_timeout_uses_operator_override(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS": "120",
                    "ACA_WORKER_TEST_ONLY_DIFF_ABORT_SECONDS": "180",
                    "ACA_WORKER_PAIRED_REPAIR_ONE_SIDED_ABORT_SECONDS": "30",
                }
            )
        )
        subtask = {
            "id": "subtask-1",
            "files": ["src/app.py", "src/app_test.py"],
            "repair_requires_production_followup": ["src/app.py"],
            "repair_requires_test_followup": ["src/app_test.py"],
            "write_required": True,
        }

        self.assertTrue(_subtask_requires_paired_source_test_diff(subtask))
        self.assertEqual(_effective_worker_testless_diff_abort_seconds(ctx, subtask, "worker-1"), 30.0)
        self.assertEqual(_effective_worker_test_only_diff_abort_seconds(ctx, subtask, "worker-1"), 30.0)

    def test_progress_snapshot_sleep_caps_slow_heartbeat_for_paired_tasks(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                coordination=SimpleNamespace(heartbeat_interval_seconds=120),
                env={},
            )
        )
        paired_subtask = {
            "id": "subtask-1",
            "files": ["src/app.py", "src/app_test.py"],
            "repair_requires_production_followup": ["src/app.py"],
            "repair_requires_test_followup": ["src/app_test.py"],
            "write_required": True,
        }
        source_only_subtask = {
            "id": "subtask-2",
            "files": ["src/other.py"],
            "write_required": True,
        }

        self.assertEqual(_worker_progress_snapshot_sleep_seconds(ctx, {}), 60.0)
        self.assertEqual(
            _worker_progress_snapshot_sleep_seconds(ctx, {"worker-1": source_only_subtask}),
            60.0,
        )
        self.assertEqual(
            _worker_progress_snapshot_sleep_seconds(ctx, {"worker-1": paired_subtask}),
            15.0,
        )

    def test_progress_snapshot_sleep_tracks_short_paired_guard_override(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                coordination=SimpleNamespace(heartbeat_interval_seconds=120),
                env={"ACA_WORKER_PAIRED_REPAIR_ONE_SIDED_ABORT_SECONDS": "30"},
            )
        )
        paired_subtask = {
            "id": "subtask-1",
            "files": ["src/app.py", "src/app_test.py"],
            "repair_requires_production_followup": ["src/app.py"],
            "repair_requires_test_followup": ["src/app_test.py"],
            "write_required": True,
        }

        self.assertEqual(
            _worker_progress_snapshot_sleep_seconds(ctx, {"worker-1": paired_subtask}),
            10.0,
        )

    def test_progress_snapshot_sleep_can_keep_slow_heartbeat_when_paired_guard_disabled(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                coordination=SimpleNamespace(heartbeat_interval_seconds=120),
                env={"ACA_WORKER_PAIRED_REPAIR_ONE_SIDED_ABORT_SECONDS": "0"},
            )
        )
        paired_subtask = {
            "id": "subtask-1",
            "files": ["src/app.py", "src/app_test.py"],
            "repair_requires_production_followup": ["src/app.py"],
            "repair_requires_test_followup": ["src/app_test.py"],
            "write_required": True,
        }

        self.assertEqual(
            _worker_progress_snapshot_sleep_seconds(ctx, {"worker-1": paired_subtask}),
            60.0,
        )

    def test_effective_testless_diff_timeout_can_still_be_disabled(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS": "0",
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                }
            )
        )

        timeout = _effective_worker_testless_diff_abort_seconds(
            ctx,
            {"id": "subtask-1", "files": ["src/app.py"], "write_required": True},
            "worker-1",
        )

        self.assertEqual(timeout, 0.0)

    def test_effective_repair_no_change_timeout_waits_for_worker_budget(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS": "180",
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                    "ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS": "180",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "60",
                }
            )
        )

        timeout = _effective_worker_repair_no_change_abort_seconds(
            ctx,
            {
                "id": "subtask-1",
                "files": ["src/app.py", "src/app_test.py"],
                "write_required": True,
                "deterministic_partial_diff_repair": True,
            },
            "worker-1",
        )

        self.assertEqual(timeout, 240.0)

    def test_effective_repair_no_change_timeout_can_still_be_disabled(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS": "0",
                    "ACA_WORKER_NO_CHANGE_ABORT_SECONDS": "240",
                }
            )
        )

        timeout = _effective_worker_repair_no_change_abort_seconds(
            ctx,
            {
                "id": "subtask-1",
                "files": ["src/app.py"],
                "write_required": True,
                "deterministic_partial_diff_repair": True,
            },
            "worker-1",
        )

        self.assertEqual(timeout, 0.0)

    def test_repair_no_change_result_keeps_carried_verifiable_partial_context(self) -> None:
        result = {
            "failure_reason": "WORKER_REPAIR_NO_CHANGE",
            "blocker_kind": "worker_no_progress",
            "output_excerpt": "Repair worker made no filesystem changes.",
        }
        subtask = {
            "carry_forward_patch": "/runs/run-1/artifacts/worker-1.patch",
            "repair_source_failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
            "repair_changed_files": [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            "repair_worker_output_excerpt": (
                "Focused failure: TypeError: worker_worktree_name() takes from 1 to 2 positional arguments "
                "but 4 were given"
            ),
            "verification_commands": [
                "python3 -m unittest src.tandem_agents.core.repository.repository_test"
            ],
        }

        _attach_carried_partial_diff_to_repair_no_change_result(result, subtask, None)

        self.assertEqual(result["partial_diff_artifact"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertEqual(result["artifacts"]["partial_diff"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertEqual(result["preserved_failure_reason"], "WORKER_VERIFIABLE_DIFF_TEST_FAILED")
        self.assertEqual(
            result["changed_files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertIn("worker_worktree_name", result["verification_output_excerpt"])
        self.assertEqual(
            result["verification_command"],
            ["python3", "-m", "unittest", "src.tandem_agents.core.repository.repository_test"],
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

    def test_tool_loop_summary_detects_failed_patch_and_noop_edit_churn(self) -> None:
        messages = [
            {
                "parts": [
                    {
                        "type": "tool",
                        "tool": "apply_patch",
                        "args": {
                            "patchText": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch"
                        },
                        "result": "error: No valid patches in input (allow with \"--allow-empty\")",
                    },
                    {
                        "type": "tool",
                        "tool": "apply_patch",
                        "args": {
                            "patchText": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch"
                        },
                        "result": "error: No valid patches in input (allow with \"--allow-empty\")",
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "args": {"path": "src/app.py", "old": "alpha", "new": "bravo"},
                        "result": "ok",
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "args": {"path": "src/app.py", "old": "bravo", "new": "alpha"},
                        "result": "ok",
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "args": {"path": "src/app.py", "old": "alpha", "new": "alpha"},
                        "result": "ok",
                    },
                    {"type": "tool", "tool": "read", "args": {"path": "src/app.py"}, "result": "alpha"},
                    {"type": "tool", "tool": "read", "args": {"path": "src/app.py"}, "result": "alpha"},
                    {"type": "tool", "tool": "read", "args": {"path": "src/app.py"}, "result": "alpha"},
                ]
            }
        ]

        summary = _tool_loop_summary_from_messages(messages)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["invalid_patch_count"], 2)
        self.assertEqual(summary["edit_count"], 3)
        self.assertEqual(summary["noop_edit_count"], 1)
        self.assertEqual(summary["paths"], ["src/app.py"])
        self.assertIn("failed patch and no-op edit", summary["reason"])

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

    def test_engine_silence_summary_detects_user_only_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "active_worker_engine_sessions.json").write_text(
                json.dumps({"worker-1": {"session_id": "session-1", "run_id": "run-1"}}),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(run_dir=run_dir, cfg=SimpleNamespace(env={}))
            messages = [
                {
                    "info": {"role": "user"},
                    "parts": [{"type": "text", "text": "worker prompt"}],
                }
            ]

            with mock.patch(
                "src.tandem_agents.core.phases.worker_dispatch._session_messages_with_timeout",
                return_value=messages,
            ):
                summary = _active_worker_engine_silence_summary(ctx, "worker-1")

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["session_id"], "session-1")
            self.assertEqual(summary["run_id"], "run-1")
            self.assertEqual(summary["message_count"], 1)

    def test_engine_silence_summary_ignores_assistant_or_tool_activity(self) -> None:
        self.assertTrue(
            _messages_have_assistant_or_tool_activity(
                [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "working"}]}]
            )
        )
        self.assertTrue(
            _messages_have_assistant_or_tool_activity(
                [{"info": {"role": "user"}, "parts": [{"type": "tool", "tool": "read"}]}]
            )
        )
        self.assertFalse(
            _messages_have_assistant_or_tool_activity(
                [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "prompt"}]}]
            )
        )
        self.assertFalse(
            _messages_have_assistant_or_tool_activity(
                [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "prompt"}, {"type": "tool"}]}]
            )
        )

    def test_latest_retry_write_required_uses_retry_started_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            events = [
                {
                    "type": "worker.started",
                    "payload": {
                        "worker_id": "worker-2",
                        "execution_id": "exec-1",
                    },
                },
                {
                    "type": "worker.retry_started",
                    "payload": {
                        "worker_id": "worker-2",
                        "execution_id": "exec-1",
                        "write_required": False,
                    },
                },
            ]
            events_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            ctx = SimpleNamespace(layout={"events": str(events_path)})

            self.assertFalse(
                _latest_worker_retry_write_required(
                    ctx,
                    "worker-2",
                    {"_worker_execution_id": "exec-1", "write_required": True},
                )
            )
            self.assertIsNone(
                _latest_worker_retry_write_required(
                    ctx,
                    "worker-2",
                    {"_worker_execution_id": "other-exec", "write_required": True},
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

    def test_required_tests_must_exercise_added_public_production_symbol(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/src/tandem_agents/core/repository/repository.py b/src/tandem_agents/core/repository/repository.py",
                "--- a/src/tandem_agents/core/repository/repository.py",
                "+++ b/src/tandem_agents/core/repository/repository.py",
                "+class RepositoryWorktreeAllocation:",
                "+def allocate_issue_worktree(repo_binding, run_id, worker_id):",
                "+    return RepositoryWorktreeAllocation()",
                "diff --git a/src/tandem_agents/core/repository/repository_test.py b/src/tandem_agents/core/repository/repository_test.py",
                "--- a/src/tandem_agents/core/repository/repository_test.py",
                "+++ b/src/tandem_agents/core/repository/repository_test.py",
                "+    def test_task_run_branch_name_includes_issue_key(self):",
                '+        branch = task_run_branch_name({"issue_key": "LACA-12"}, "run-1")',
                '+        self.assertIn("LACA-12", branch)',
            ]
        )

        self.assertEqual(
            _diff_required_tests_missing_added_public_symbols(
                diff_text,
                ["src/tandem_agents/core/repository/repository_test.py"],
            ),
            ["RepositoryWorktreeAllocation", "allocate_issue_worktree"],
        )

    def test_required_tests_pass_when_they_call_added_public_symbol(self) -> None:
        diff_text = "\n".join(
            [
                "diff --git a/src/tandem_agents/core/repository/repository.py b/src/tandem_agents/core/repository/repository.py",
                "--- a/src/tandem_agents/core/repository/repository.py",
                "+++ b/src/tandem_agents/core/repository/repository.py",
                "+def allocate_issue_worktree(repo_binding, run_id, worker_id):",
                "+    return repo_binding",
                "diff --git a/src/tandem_agents/core/repository/repository_test.py b/src/tandem_agents/core/repository/repository_test.py",
                "--- a/src/tandem_agents/core/repository/repository_test.py",
                "+++ b/src/tandem_agents/core/repository/repository_test.py",
                "+    def test_allocate_issue_worktree_returns_allocation(self):",
                '+        allocation = allocate_issue_worktree({"name": "repo"}, "run-1", "worker-1")',
                '+        self.assertEqual(allocation["name"], "repo")',
            ]
        )

        self.assertEqual(
            _diff_required_tests_missing_added_public_symbols(
                diff_text,
                ["src/tandem_agents/core/repository/repository_test.py"],
            ),
            [],
        )

    def test_reviewable_failed_diff_rejection_reports_misaligned_public_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src" / "tandem_agents" / "core" / "repository"
            package.mkdir(parents=True)
            for init_path in [
                root / "src" / "__init__.py",
                root / "src" / "tandem_agents" / "__init__.py",
                root / "src" / "tandem_agents" / "core" / "__init__.py",
                package / "__init__.py",
            ]:
                init_path.write_text("", encoding="utf-8")
            (package / "repository.py").write_text("def task_run_branch_name(task, run_id):\n    return run_id\n", encoding="utf-8")
            (package / "repository_test.py").write_text("import unittest\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            (package / "repository.py").write_text(
                "class IsolatedRunCheckout:\n"
                "    pass\n\n"
                "def task_run_branch_name(task, run_id):\n"
                "    return run_id\n",
                encoding="utf-8",
            )
            (package / "repository_test.py").write_text(
                "import unittest\n\n"
                "from src.tandem_agents.core.repository.repository import task_run_branch_name\n\n"
                "class RepositoryTest(unittest.TestCase):\n"
                "    def test_task_run_branch_name_keeps_run_id(self):\n"
                "        self.assertEqual(task_run_branch_name({}, 'run-1'), 'run-1')\n",
                encoding="utf-8",
            )
            subtask = {
                "files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "acceptance_criteria": ["Tests cover the repository isolation regression."],
            }

            rejection = _reviewable_failed_diff_rejection(
                root,
                subtask,
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
            )

        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection["reason"], "misaligned_test_diff")
        self.assertEqual(rejection["missing_symbols"], ["IsolatedRunCheckout"])
        self.assertIn("IsolatedRunCheckout", rejection["message"])

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

    def test_reviewable_failed_diff_rejection_counts_staged_carried_test_diff(self) -> None:
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
                "from src.tandem_agents.config.config_loader import VALUE\n\n"
                "class ConfigLoaderTest(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(VALUE, 2)\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "add",
                    "src/tandem_agents/config/config_loader.py",
                    "src/tandem_agents/config/config_loader_test.py",
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (package / "config_loader.py").write_text("VALUE = 2  # repaired\n", encoding="utf-8")
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

        self.assertIsNone(rejection)

    def test_changed_python_tests_result_scrubs_runtime_repo_env(self) -> None:
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
                "import os\n"
                "import unittest\n\n"
                "class ConfigLoaderEnvTest(unittest.TestCase):\n"
                "    def test_runtime_repo_env_is_scrubbed(self):\n"
                "        self.assertNotIn('ACA_REPO_PATH', os.environ)\n"
                "        self.assertNotIn('ACA_WORKTREE_ROOT', os.environ)\n"
                "        self.assertNotIn('TANDEM_CONTROL_PANEL_CONFIG_FILE', os.environ)\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "ACA_REPO_PATH": "/workspace/repos/tandem-agents",
                    "ACA_WORKTREE_ROOT": "/workspace/repos",
                    "TANDEM_CONTROL_PANEL_CONFIG_FILE": "/workspace/tandem-data/control-panel-config.json",
                },
                clear=False,
            ):
                result = _changed_python_tests_result(
                    root,
                    ["src/tandem_agents/config/config_loader_test.py"],
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["ok"], result["output"])

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
            attempts = run_dir / "active_worker_attempts.json"
            attempts.write_text(
                json.dumps({"worker-1": "exec-1", "worker-2": "exec-2"}),
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
            self.assertEqual(
                json.loads(attempts.read_text(encoding="utf-8")),
                {"worker-2": "exec-2"},
            )
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["type"] for event in events],
                ["worker.engine_cancel_requested", "worker.engine_cancelled"],
            )

    def test_cancel_active_worker_engine_session_keeps_marker_when_delete_fails(self) -> None:
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
                "src.tandem_agents.core.phases.worker_dispatch.delete_tandem_session",
                side_effect=RuntimeError("engine delete timed out"),
            ) as delete_session:
                _cancel_active_worker_engine_session(ctx, "worker-1", "worker_no_progress")
                for _ in range(50):
                    if delete_session.call_count and "worker.engine_cancel_failed" in layout["events"].read_text(
                        encoding="utf-8"
                    ):
                        break
                    time.sleep(0.01)

            delete_session.assert_called_once_with(ctx.cfg, "session-1")
            active = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(active["worker-1"]["session_id"], "session-1")
            self.assertIn("engine delete timed out", active["worker-1"]["cleanup_error"])
            self.assertIn("cleanup_failed_at_ms", active["worker-1"])

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
        self.assertFalse(
            _failed_result_has_reviewable_source_and_test_diff(
                {**result, "failure_reason": "WORKER_VERIFIABLE_DIFF_UNTERMINATED"},
                subtask,
            )
        )
        self.assertTrue(
            _failed_result_has_reviewable_source_and_test_diff(
                {
                    **result,
                    "failure_reason": "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
                    "verification_returncode": 0,
                    "verification_timed_out": False,
                },
                subtask,
            )
        )
        self.assertFalse(
            _failed_result_has_reviewable_source_and_test_diff(
                {
                    **result,
                    "failure_reason": "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
                    "verification_returncode": 0,
                    "verification_timed_out": True,
                },
                subtask,
            )
        )

    def test_failed_result_with_scoped_production_contract_diff_is_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            package = worktree / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            config_types = package / "config_types.py"
            config_types.write_text(
                "DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50\n\n"
                "class SchedulerConfig:\n"
                "    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            config_types.write_text(
                "DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50\n"
                "DEFAULT_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS = 4\n"
                "DEFAULT_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS = 0\n"
                "DEFAULT_SCHEDULER_RATE_LIMIT_BACKPRESSURE = True\n"
                "DEFAULT_SCHEDULER_CI_BACKPRESSURE = True\n"
                "DEFAULT_SCHEDULER_MERGE_QUEUE_BACKPRESSURE = True\n\n"
                "class SchedulerConfig:\n"
                "    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT\n"
                "    max_concurrent_worker_runs: int = DEFAULT_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS\n"
                "    max_daily_model_spend_cents: int = DEFAULT_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS\n"
                "    rate_limit_backpressure: bool = DEFAULT_SCHEDULER_RATE_LIMIT_BACKPRESSURE\n"
                "    ci_backpressure: bool = DEFAULT_SCHEDULER_CI_BACKPRESSURE\n"
                "    merge_queue_backpressure: bool = DEFAULT_SCHEDULER_MERGE_QUEUE_BACKPRESSURE\n",
                encoding="utf-8",
            )
            subtask = {
                "files": ["src/tandem_agents/config/config_types.py"],
                "target_files": ["src/tandem_agents/config/config_types.py"],
                "acceptance_criteria": [
                    "Add max_concurrent_worker_runs, max_daily_model_spend_cents, rate_limit_backpressure, ci_backpressure, and merge_queue_backpressure.",
                    "Add those exact scheduler fields to ResolvedConfig.as_dict() under the scheduler payload if the scheduler payload enumerates fields explicitly.",
                    "Do not add max_parallel_workers or other aliases.",
                ],
            }
            result = {
                "returncode": 1,
                "partial_diff_artifact": "/runs/run-1/artifacts/worker.patch",
                "changed_files": ["src/tandem_agents/config/config_types.py"],
            }

            self.assertTrue(_failed_result_has_reviewable_production_diff(result, subtask, worktree))

    def test_failed_result_matches_uppercase_env_contract_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            package = worktree / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            config_loader = package / "config_loader.py"
            config_loader.write_text(
                "def resolve_scheduler_config(env):\n"
                "    return SchedulerConfig(queue_depth_limit=50)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            config_loader.write_text(
                "def resolve_scheduler_config(env):\n"
                "    scheduler = {}\n"
                "    return SchedulerConfig(\n"
                "        queue_depth_limit=50,\n"
                "        max_concurrent_worker_runs=_config_int(\n"
                "            scheduler, env, 'max_concurrent_worker_runs', 'ACA_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS', 4\n"
                "        ),\n"
                "        max_daily_model_spend_cents=_config_int(\n"
                "            scheduler, env, 'max_daily_model_spend_cents', 'ACA_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS', 0\n"
                "        ),\n"
                "        rate_limit_backpressure=_config_bool(\n"
                "            scheduler, env, 'rate_limit_backpressure', 'ACA_SCHEDULER_RATE_LIMIT_BACKPRESSURE', True\n"
                "        ),\n"
                "        ci_backpressure=_config_bool(\n"
                "            scheduler, env, 'ci_backpressure', 'ACA_SCHEDULER_CI_BACKPRESSURE', True\n"
                "        ),\n"
                "        merge_queue_backpressure=_config_bool(\n"
                "            scheduler, env, 'merge_queue_backpressure', 'ACA_SCHEDULER_MERGE_QUEUE_BACKPRESSURE', True\n"
                "        ),\n"
                "    )\n",
                encoding="utf-8",
            )
            subtask = {
                "files": ["src/tandem_agents/config/config_loader.py"],
                "target_files": ["src/tandem_agents/config/config_loader.py"],
                "acceptance_criteria": [
                    "Load ACA_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS, ACA_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS, "
                    "ACA_SCHEDULER_RATE_LIMIT_BACKPRESSURE, ACA_SCHEDULER_CI_BACKPRESSURE, and "
                    "ACA_SCHEDULER_MERGE_QUEUE_BACKPRESSURE env vars.",
                ],
            }
            result = {
                "returncode": 1,
                "partial_diff_artifact": "/runs/run-1/artifacts/worker.patch",
                "changed_files": ["src/tandem_agents/config/config_loader.py"],
            }

            self.assertTrue(_failed_result_has_reviewable_production_diff(result, subtask, worktree))

    def test_failed_result_with_docstring_only_production_diff_is_not_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            package = worktree / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            config_types = package / "config_types.py"
            config_types.write_text(
                "class SchedulerConfig:\n"
                "    queue_depth_limit: int = 50\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            config_types.write_text(
                "class SchedulerConfig:\n"
                "    \"\"\"Scheduling and throughput controls for ACA runs.\"\"\"\n"
                "    queue_depth_limit: int = 50\n",
                encoding="utf-8",
            )
            subtask = {
                "files": ["src/tandem_agents/config/config_types.py"],
                "target_files": ["src/tandem_agents/config/config_types.py"],
                "acceptance_criteria": [
                    "Add max_concurrent_worker_runs, max_daily_model_spend_cents, rate_limit_backpressure, ci_backpressure, and merge_queue_backpressure.",
                ],
            }
            result = {
                "returncode": 1,
                "partial_diff_artifact": "/runs/run-1/artifacts/worker.patch",
                "changed_files": ["src/tandem_agents/config/config_types.py"],
            }

            self.assertFalse(_failed_result_has_reviewable_production_diff(result, subtask, worktree))

    def test_changed_files_scoped_to_subtask_ignores_carried_forward_partials(self) -> None:
        subtask = {
            "files": ["src/tandem_agents/config/config_loader.py"],
            "target_files": ["src/tandem_agents/config/config_loader.py"],
        }

        self.assertEqual(
            _changed_files_scoped_to_subtask(
                [
                    "src/tandem_agents/config/config_types.py",
                    "src/tandem_agents/config/config_loader.py",
                ],
                subtask,
            ),
            ["src/tandem_agents/config/config_loader.py"],
        )

    def test_filter_diff_text_to_files_ignores_carried_forward_sections(self) -> None:
        diff_text = (
            "diff --git a/src/tandem_agents/config/config_types.py b/src/tandem_agents/config/config_types.py\n"
            "--- a/src/tandem_agents/config/config_types.py\n"
            "+++ b/src/tandem_agents/config/config_types.py\n"
            "@@ -1 +1 @@\n"
            "-queue_depth_limit = 50\n"
            "+max_concurrent_worker_runs = 4\n"
            "diff --git a/src/tandem_agents/config/config_loader.py b/src/tandem_agents/config/config_loader.py\n"
            "--- a/src/tandem_agents/config/config_loader.py\n"
            "+++ b/src/tandem_agents/config/config_loader.py\n"
            "@@ -1 +1 @@\n"
            "-return SchedulerConfig(queue_depth_limit=50)\n"
            "+return SchedulerConfig(max_concurrent_worker_runs=4)\n"
        )

        filtered = _filter_diff_text_to_files(
            diff_text,
            ["src/tandem_agents/config/config_loader.py"],
        )

        self.assertIn("config_loader.py", filtered)
        self.assertNotIn("config_types.py", filtered)
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            package = worktree / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            (package / "config_loader.py").write_text(
                "return SchedulerConfig(queue_depth_limit=50)\n",
                encoding="utf-8",
            )
            patch_path = worktree / "filtered.patch"
            patch_path.write_text(filtered, encoding="utf-8")
            result = subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_worktree_has_subtask_changes_ignores_inherited_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            package = worktree / "src" / "tandem_agents" / "config"
            package.mkdir(parents=True)
            (package / "config_types.py").write_text("queue_depth_limit = 50\n", encoding="utf-8")
            (package / "config_loader.py").write_text("def load():\n    return 50\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            (package / "config_types.py").write_text("max_concurrent_worker_runs = 4\n", encoding="utf-8")
            subtask = {
                "files": ["src/tandem_agents/config/config_loader.py"],
                "target_files": ["src/tandem_agents/config/config_loader.py"],
            }

            self.assertFalse(_worktree_has_subtask_changes(worktree, subtask))

            (package / "config_loader.py").write_text("def load():\n    return 4\n", encoding="utf-8")

            self.assertTrue(_worktree_has_subtask_changes(worktree, subtask))

    def test_worktree_has_subtask_changes_ignores_start_baseline_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            package = worktree / "src" / "tandem_agents" / "core" / "repository"
            package.mkdir(parents=True)
            source_rel = "src/tandem_agents/core/repository/repository.py"
            test_rel = "src/tandem_agents/core/repository/repository_test.py"
            (worktree / source_rel).write_text("def existing():\n    return 1\n", encoding="utf-8")
            (worktree / test_rel).write_text("def test_existing():\n    assert existing\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)

            (worktree / source_rel).write_text("def worker_one():\n    return 1\n", encoding="utf-8")
            (worktree / test_rel).write_text("def test_worker_one():\n    assert worker_one\n", encoding="utf-8")
            baseline = {
                "baseline_changed_files": [source_rel, test_rel],
                "baseline_file_states": {
                    source_rel: _baseline_file_state(worktree, source_rel),
                    test_rel: _baseline_file_state(worktree, test_rel),
                },
            }
            subtask = {
                "files": [source_rel, test_rel],
                "target_files": [source_rel, test_rel],
            }

            self.assertFalse(_worktree_has_subtask_changes(worktree, subtask, baseline))
            self.assertEqual(
                _fresh_changed_files_since_baseline(worktree, [source_rel, test_rel], baseline),
                [],
            )

            (worktree / source_rel).write_text("def worker_two():\n    return 2\n", encoding="utf-8")

            self.assertTrue(_worktree_has_subtask_changes(worktree, subtask, baseline))
            self.assertEqual(
                _fresh_changed_files_since_baseline(worktree, [source_rel, test_rel], baseline),
                [source_rel],
            )

    def test_result_partial_diff_artifact_is_filtered_to_fresh_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "worker-2.partial-worker-diff.patch"
            artifact.write_text(
                "# Partial worker diff preserved after nonterminal engine result\n\n"
                "diff --git a/src/tandem_agents/core/phases/task_intake.py b/src/tandem_agents/core/phases/task_intake.py\n"
                "index 1111111..2222222 100644\n"
                "--- a/src/tandem_agents/core/phases/task_intake.py\n"
                "+++ b/src/tandem_agents/core/phases/task_intake.py\n"
                "@@ -1 +1 @@\n"
                "-old intake\n"
                "+new intake\n"
                "diff --git a/src/tandem_agents/core/repository/repository.py b/src/tandem_agents/core/repository/repository.py\n"
                "index 3333333..4444444 100644\n"
                "--- a/src/tandem_agents/core/repository/repository.py\n"
                "+++ b/src/tandem_agents/core/repository/repository.py\n"
                "@@ -1 +1 @@\n"
                "-old repo\n"
                "+new repo\n"
                "diff --git a/src/tandem_agents/core/repository/repository_test.py b/src/tandem_agents/core/repository/repository_test.py\n"
                "index 5555555..6666666 100644\n"
                "--- a/src/tandem_agents/core/repository/repository_test.py\n"
                "+++ b/src/tandem_agents/core/repository/repository_test.py\n"
                "@@ -1 +1 @@\n"
                "-old test\n"
                "+new test\n",
                encoding="utf-8",
            )
            result = {
                "partial_diff_artifact": str(artifact),
                "artifacts": {"partial_diff": str(artifact)},
            }

            filtered = _filter_result_partial_diff_artifact(
                result,
                ["src/tandem_agents/core/phases/task_intake.py"],
            )

            self.assertTrue(filtered)
            self.assertNotEqual(filtered, str(artifact))
            self.assertEqual(result["partial_diff_artifact"], filtered)
            self.assertEqual(result["artifacts"]["partial_diff"], filtered)
            self.assertEqual(result["artifacts"]["original_partial_diff"], str(artifact))
            filtered_text = Path(filtered).read_text(encoding="utf-8")
            self.assertIn("src/tandem_agents/core/phases/task_intake.py", filtered_text)
            self.assertNotIn("src/tandem_agents/core/repository/repository.py", filtered_text)
            self.assertNotIn("src/tandem_agents/core/repository/repository_test.py", filtered_text)

    def test_verifiable_diff_requires_primary_source_target(self) -> None:
        subtask = {
            "title": "Wire intake to isolated worktrees",
            "files": [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            "target_files": [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            "acceptance_criteria": [
                "Claim-time intake stores branch/worktree metadata.",
                "Tests or focused assertions verify claim-time base revision pinning.",
            ],
        }

        self.assertFalse(
            _changed_files_satisfy_primary_source_target(
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                subtask,
            )
        )
        self.assertFalse(
            _subtask_has_verifiable_source_and_test_diff(
                subtask,
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
            )
        )
        self.assertTrue(
            _subtask_has_verifiable_source_and_test_diff(
                subtask,
                [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
            )
        )

    def test_test_only_diff_guard_allows_pure_test_slice(self) -> None:
        subtask = {
            "files": ["src/tandem_agents/config/config_loader_test.py"],
            "target_files": ["src/tandem_agents/config/config_loader_test.py"],
            "acceptance_criteria": [
                "Add focused config loader test coverage for exact scheduler fields.",
            ],
        }

        self.assertFalse(
            _subtask_has_required_test_only_diff(
                subtask,
                ["src/tandem_agents/config/config_loader_test.py"],
            )
        )

    def test_test_only_diff_guard_allows_explicit_test_only_repair_scope(self) -> None:
        subtask = {
            "files": [
                "src/tandem_agents/config/config_loader_test.py",
                "src/tandem_agents/config/config_loader.py",
            ],
            "target_files": [
                "src/tandem_agents/config/config_loader_test.py",
                "src/tandem_agents/config/config_loader.py",
            ],
            "scope_note": (
                "Mechanical slice 3 of 3 for throughput config controls. "
                "Edit only config_loader_test.py. This is a test-only slice after "
                "the config fields and loader wiring slices; do not edit production files here."
            ),
            "acceptance_criteria": [
                "Add focused config loader test coverage for exact scheduler fields.",
            ],
        }

        self.assertFalse(
            _subtask_has_required_test_only_diff(
                subtask,
                ["src/tandem_agents/config/config_loader_test.py"],
            )
        )

    def test_test_only_diff_guard_rejects_required_production_followup(self) -> None:
        subtask = {
            "files": [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            "target_files": [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            "repair_requires_production_followup": ["src/tandem_agents/config/config_loader.py"],
            "acceptance_criteria": [
                "Tests cover the config loader regression.",
                "Make the first new repair edit in the required production file.",
            ],
        }

        self.assertTrue(
            _subtask_has_required_test_only_diff(
                subtask,
                ["src/tandem_agents/config/config_loader_test.py"],
            )
        )

    def test_carried_source_patch_satisfies_repair_production_followup(self) -> None:
        source_path = "src/tandem_agents/core/repository/repository.py"
        test_path = "src/tandem_agents/core/repository/repository_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "repair_requires_production_followup": [source_path],
            "acceptance_criteria": [
                "Tests cover the repository isolation regression.",
                "Make the first new repair edit in the required production file.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [test_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_has_required_test_only_diff(subtask, [test_path]))
        self.assertFalse(_subtask_has_required_test_only_diff(subtask, effective_files))
        self.assertTrue(_changed_files_satisfy_primary_source_target(effective_files, subtask))
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))

    def test_paired_source_test_repair_requires_fresh_required_test_delta(self) -> None:
        source_path = "src/tandem_agents/core/repository/repository.py"
        test_path = "src/tandem_agents/core/repository/repository_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "repair_requires_paired_source_test_diff": True,
            "repair_requires_production_followup": [source_path],
            "repair_requires_test_followup": [test_path],
            "acceptance_criteria": ["Add regression test coverage for repository isolation."],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [source_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path])
        self.assertFalse(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))

    def test_composed_carried_repair_counts_carried_pair_for_fresh_source_delta(self) -> None:
        source_path = "src/tandem_agents/aca_harness/calculator.py"
        test_path = "src/tandem_agents/aca_harness/calculator_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patches": ["/tmp/worker-1.partial-worker-diff.patch"],
            "repair_requires_paired_source_test_diff": True,
            "repair_requires_paired_source_test": True,
            "repair_requires_production_followup": [source_path],
            "repair_requires_test_followup": [test_path],
            "repair_mode": "complementary_guarded_partial_diff",
            "acceptance_criteria": [
                "Fix the focused verification failure in the preserved source/test patch.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [source_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))
        self.assertTrue(
            _changed_files_satisfy_required_test_files(
                effective_files,
                _subtask_required_test_files(subtask),
            )
        )

    def test_weak_source_test_repair_counts_carried_pair_for_fresh_source_delta(self) -> None:
        source_path = "src/tandem_agents/aca_harness/calculator.py"
        test_path = "src/tandem_agents/aca_harness/calculator_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "deterministic_partial_diff_repair": True,
            "repair_requires_paired_source_test_diff": True,
            "repair_requires_paired_source_test": True,
            "repair_requires_production_followup": [source_path],
            "repair_requires_test_followup": [test_path],
            "repair_verification_first": True,
            "repair_mode": "weak_source_test_diff",
            "acceptance_criteria": [
                "Fix the focused verification failure in the preserved source/test patch.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [source_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))
        self.assertTrue(
            _changed_files_satisfy_required_test_files(
                effective_files,
                _subtask_required_test_files(subtask),
            )
        )

    def test_weak_source_test_repair_counts_singular_carried_pair_without_verification_first(self) -> None:
        source_path = "src/tandem_agents/aca_harness/calculator.py"
        test_path = "src/tandem_agents/aca_harness/calculator_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "deterministic_partial_diff_repair": True,
            "repair_changed_files": [source_path, test_path],
            "repair_requires_paired_source_test_diff": True,
            "repair_requires_paired_source_test": True,
            "repair_requires_production_followup": [source_path],
            "repair_requires_test_followup": [test_path],
            "repair_mode": "weak_source_test_diff",
            "acceptance_criteria": [
                "The preserved weak source+test patch is applied before this worker starts.",
                "Make the first new repair edit in the required test file.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [source_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))
        self.assertTrue(
            _changed_files_satisfy_required_test_files(
                effective_files,
                _subtask_required_test_files(subtask),
            )
        )

    def test_verification_first_paired_repair_counts_carried_pair_for_fresh_source_delta(self) -> None:
        source_path = "src/tandem_agents/aca_harness/calculator.py"
        test_path = "src/tandem_agents/aca_harness/calculator_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "deterministic_partial_diff_repair": True,
            "repair_requires_paired_source_test_diff": True,
            "repair_requires_paired_source_test": True,
            "repair_requires_production_followup": [source_path],
            "repair_requires_test_followup": [test_path],
            "repair_verification_first": True,
            "repair_mode": "complementary_guarded_partial_diff",
            "acceptance_criteria": [
                "Verify and minimally fix the combined source/test repair.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [source_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))

    def test_failed_verifiable_repair_counts_carried_required_test_file(self) -> None:
        source_path = "src/tandem_agents/aca_harness/calculator.py"
        test_path = "src/tandem_agents/aca_harness/calculator_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "deterministic_partial_diff_repair": True,
            "repair_verification_first": True,
            "repair_changed_files": [source_path, test_path],
            "acceptance_criteria": [
                "Run python3 -m unittest src.tandem_agents.aca_harness.calculator_test.",
                "Fix only the focused verification failure in the preserved source/test patch.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [source_path],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_requires_paired_source_test_diff(subtask))
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))
        self.assertTrue(
            _changed_files_satisfy_required_test_files(
                effective_files,
                _subtask_required_test_files(subtask),
            )
        )

    def test_verification_first_carried_diff_counts_without_fresh_delta(self) -> None:
        source_path = "src/tandem_agents/aca_harness/calculator.py"
        test_path = "src/tandem_agents/aca_harness/calculator_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "deterministic_partial_diff_repair": True,
            "repair_verification_first": True,
            "repair_changed_files": [source_path, test_path],
            "acceptance_criteria": [
                "Run python3 -m unittest src.tandem_agents.aca_harness.calculator_test.",
                "If verification passes, return without making another mandatory edit.",
            ],
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [],
            baseline,
        )

        self.assertEqual(effective_files, [source_path, test_path])
        self.assertTrue(_subtask_has_verifiable_source_and_test_diff(subtask, effective_files))

    def test_normal_source_test_subtask_uses_paired_guard(self) -> None:
        source_path = "src/tandem_agents/core/repository/repository.py"
        test_path = "src/tandem_agents/core/repository/repository_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "write_required": True,
            "acceptance_criteria": ["Add regression test coverage for repository isolation."],
        }

        self.assertTrue(_subtask_requires_paired_source_test_diff(subtask))

    def test_carried_test_only_patch_does_not_satisfy_production_followup(self) -> None:
        source_path = "src/tandem_agents/core/repository/repository.py"
        test_path = "src/tandem_agents/core/repository/repository_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
            "repair_requires_production_followup": [source_path],
            "acceptance_criteria": ["Add regression test coverage for repository isolation."],
        }
        baseline = {"baseline_changed_files": [test_path]}

        effective_files = _validation_changed_files_with_carried_baseline(
            subtask,
            [test_path],
            baseline,
        )

        self.assertEqual(effective_files, [test_path])
        self.assertTrue(_subtask_has_required_test_only_diff(subtask, effective_files))

    def test_carried_patch_requires_fresh_repair_change(self) -> None:
        source_path = "src/tandem_agents/core/repository/repository.py"
        test_path = "src/tandem_agents/core/repository/repository_test.py"
        subtask = {
            "files": [source_path, test_path],
            "target_files": [source_path, test_path],
            "carry_forward_patch": "/tmp/worker-1.partial-worker-diff.patch",
        }
        baseline = {"baseline_changed_files": [source_path, test_path]}

        self.assertEqual(
            _validation_changed_files_with_carried_baseline(subtask, [], baseline),
            [],
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
                        "failure_reason": "WORKER_RUNAWAY_DIFF",
                        "blocker_kind": "worker_runaway_diff",
                        "output_excerpt": "Worker diff exceeded ACA runaway guard before focused validation.",
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
            self.assertEqual(ctx.worker_results[0]["blocker_kind"], "worker_incomplete_diff")
            self.assertEqual(ctx.worker_results[0]["failure_reason"], "WORKER_VERIFIABLE_DIFF_WEAK_TEST")
            self.assertEqual(ctx.worker_results[0]["patch_reusable"], False)
            self.assertIn("runaway/destructive guard", ctx.worker_results[0]["output_excerpt"])
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("worker.verifiable_failed_diff_rejected", [event["type"] for event in events])
            self.assertNotIn("worker.verifiable_failed_diff_synced", [event["type"] for event in events])
            self.assertEqual(ctx.status["metrics"]["failed_workers"], 1)

    def test_unterminated_verifiable_diff_is_not_synced_as_completed_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            repo_path = root / "repo"
            worktree = run_dir / "worktrees" / "worker-1--slice-1"
            for base in (repo_path, worktree):
                package = base / "src" / "tandem_agents" / "config"
                package.mkdir(parents=True)
                for init_path in [
                    base / "src" / "__init__.py",
                    base / "src" / "tandem_agents" / "__init__.py",
                    package / "__init__.py",
                ]:
                    init_path.write_text("", encoding="utf-8")
                (package / "config_loader.py").write_text("VALUE = 1\n", encoding="utf-8")
                (package / "config_loader_test.py").write_text(
                    "import unittest\n\n"
                    "class ConfigLoaderTest(unittest.TestCase):\n"
                    "    def test_value(self):\n"
                    "        self.assertEqual(1, 1)\n",
                    encoding="utf-8",
                )
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            (worktree / "src" / "tandem_agents" / "config" / "config_loader.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (worktree / "src" / "tandem_agents" / "config" / "config_loader_test.py").write_text(
                "import unittest\n"
                "from src.tandem_agents.config.config_loader import VALUE\n\n"
                "class ConfigLoaderTest(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(VALUE, 2)\n",
                encoding="utf-8",
            )
            layout = ensure_layout(run_dir)
            subtask = {
                "id": "slice-1",
                "title": "Add config loader regression",
                "_worker_worktree_name": "worker-1--slice-1",
                "write_required": True,
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
                        "failure_reason": "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
                        "blocker_kind": "worker_incomplete_diff",
                        "output_excerpt": "Focused tests passed but the worker never returned a terminal result.",
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
            self.assertEqual(
                ctx.worker_results[0]["failure_reason"],
                "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
            )
            self.assertEqual(ctx.worker_results[0]["blocker_kind"], "worker_incomplete_diff")
            self.assertNotIn(
                "VALUE = 2",
                (repo_path / "src" / "tandem_agents" / "config" / "config_loader.py").read_text(
                    encoding="utf-8"
                ),
            )
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            event_types = [event["type"] for event in events]
            self.assertNotIn("worker.verifiable_failed_diff_synced", event_types)
            self.assertEqual(ctx.status["metrics"]["failed_workers"], 1)

    def test_scoped_production_failed_diff_syncs_as_completed_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            repo_path = root / "repo"
            worktree = run_dir / "worktrees" / "worker-1--slice-1"
            for base in (repo_path, worktree):
                package = base / "src" / "tandem_agents" / "config"
                package.mkdir(parents=True)
                (package / "config_types.py").write_text(
                    "DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50\n\n"
                    "class SchedulerConfig:\n"
                    "    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT\n",
                    encoding="utf-8",
                )
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            (worktree / "src" / "tandem_agents" / "config" / "config_types.py").write_text(
                "DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50\n"
                "DEFAULT_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS = 4\n"
                "DEFAULT_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS = 0\n"
                "DEFAULT_SCHEDULER_RATE_LIMIT_BACKPRESSURE = True\n"
                "DEFAULT_SCHEDULER_CI_BACKPRESSURE = True\n"
                "DEFAULT_SCHEDULER_MERGE_QUEUE_BACKPRESSURE = True\n\n"
                "class SchedulerConfig:\n"
                "    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT\n"
                "    max_concurrent_worker_runs: int = DEFAULT_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS\n"
                "    max_daily_model_spend_cents: int = DEFAULT_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS\n"
                "    rate_limit_backpressure: bool = DEFAULT_SCHEDULER_RATE_LIMIT_BACKPRESSURE\n"
                "    ci_backpressure: bool = DEFAULT_SCHEDULER_CI_BACKPRESSURE\n"
                "    merge_queue_backpressure: bool = DEFAULT_SCHEDULER_MERGE_QUEUE_BACKPRESSURE\n",
                encoding="utf-8",
            )
            layout = ensure_layout(run_dir)
            subtask = {
                "id": "slice-1",
                "title": "Add scheduler throughput config fields",
                "_worker_worktree_name": "worker-1--slice-1",
                "write_required": True,
                "files": ["src/tandem_agents/config/config_types.py"],
                "target_files": ["src/tandem_agents/config/config_types.py"],
                "acceptance_criteria": [
                    "Add max_concurrent_worker_runs, max_daily_model_spend_cents, rate_limit_backpressure, ci_backpressure, and merge_queue_backpressure.",
                    "Do not add max_parallel_workers or aliases.",
                ],
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
                        "changed_files": ["src/tandem_agents/config/config_types.py"],
                        "blocker_kind": "worker_incomplete_diff",
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
            self.assertEqual(ctx.worker_results[0]["status"], "completed")
            self.assertEqual(ctx.worker_results[0]["partial_diff_state"], "reviewable_terminalized")
            self.assertIn(
                "max_concurrent_worker_runs",
                (repo_path / "src" / "tandem_agents" / "config" / "config_types.py").read_text(encoding="utf-8"),
            )
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn(
                "worker.reviewable_production_failed_diff_synced",
                [event["type"] for event in events],
            )

    def test_scoped_production_failed_diff_with_terminalized_blockers_does_not_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run-1"
            repo_path = root / "repo"
            worktree = run_dir / "worktrees" / "worker-1--slice-1"
            for base in (repo_path, worktree):
                package = base / "src" / "tandem_agents" / "config"
                package.mkdir(parents=True)
                (package / "config_types.py").write_text(
                    "DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50\n\n"
                    "class SchedulerConfig:\n"
                    "    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT\n",
                    encoding="utf-8",
                )
            subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA Test"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
            (worktree / "src" / "tandem_agents" / "config" / "config_types.py").write_text(
                "DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50\n"
                "DEFAULT_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS = 4\n"
                "DEFAULT_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS = 0\n"
                "DEFAULT_SCHEDULER_RATE_LIMIT_BACKPRESSURE = True\n"
                "DEFAULT_SCHEDULER_CI_BACKPRESSURE = True\n"
                "DEFAULT_SCHEDULER_MERGE_QUEUE_BACKPRESSURE = True\n\n"
                "class SchedulerConfig:\n"
                "    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT\n"
                "    max_concurrent_worker_runs: int = DEFAULT_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS\n"
                "    max_daily_model_spend_cents: int = DEFAULT_SCHEDULER_MAX_DAILY_MODEL_SPEND_CENTS\n"
                "    rate_limit_backpressure: bool = DEFAULT_SCHEDULER_BACKPRESSURE_ENABLED\n"
                "    ci_backpressure: bool = DEFAULT_SCHEDULER_BACKPRESSURE_ENABLED\n"
                "    merge_queue_backpressure: bool = DEFAULT_SCHEDULER_BACKPRESSURE_ENABLED\n",
                encoding="utf-8",
            )
            layout = ensure_layout(run_dir)
            subtask = {
                "id": "slice-1",
                "title": "Add scheduler throughput config fields",
                "_worker_worktree_name": "worker-1--slice-1",
                "write_required": True,
                "files": ["src/tandem_agents/config/config_types.py"],
                "target_files": ["src/tandem_agents/config/config_types.py"],
                "acceptance_criteria": [
                    "Add max_concurrent_worker_runs, max_daily_model_spend_cents, rate_limit_backpressure, ci_backpressure, and merge_queue_backpressure.",
                ],
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
                        "changed_files": ["src/tandem_agents/config/config_types.py"],
                        "blocker_kind": "worker_incomplete_diff",
                        "stdout": (
                            "ENGINE_TOOL_LOOP_TERMINALIZED\n"
                            "Remaining implementation blockers:\n"
                            "- DEFAULT_SCHEDULER_BACKPRESSURE_ENABLED is undefined."
                        ),
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
            self.assertNotIn(
                "max_concurrent_worker_runs",
                (repo_path / "src" / "tandem_agents" / "config" / "config_types.py").read_text(encoding="utf-8"),
            )
            events = [
                json.loads(line)
                for line in layout["events"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertNotIn(
                "worker.reviewable_production_failed_diff_synced",
                [event["type"] for event in events],
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
