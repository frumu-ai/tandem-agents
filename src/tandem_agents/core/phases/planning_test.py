from __future__ import annotations

import unittest
import tempfile
import json
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.phases.planning import (
    _align_python_test_targets_to_repo_conventions,
    _append_deferred_repair_subtasks,
    _apply_repo_context_required_files_to_task,
    _carry_forward_partial_diff_artifacts,
    _cancel_active_manager_engine_session,
    _constrain_extra_partial_diff_repair_subtasks,
    _completed_repair_worker_results,
    _deterministic_testless_partial_diff_repair_plan,
    _mark_manager_planning_started,
    _manager_plan_from_stdout,
    _manager_prompt_timeout_seconds,
    _merge_or_defer_sticky_expected_files,
    _namespace_repair_retry_subtask_ids,
    _prepare_subtasks,
    _remote_code_task_requires_worker_execution,
    _sanitize_partial_diff_artifact_paths_in_plan,
    _serial_subtask_limit,
    _split_dense_serial_subtasks,
    _write_required_after_prescreen,
    run_manager_prompt,
)


class PlanningPreScreenTest(unittest.TestCase):
    def test_cancel_active_manager_engine_session_deletes_marked_session(self) -> None:
        from src.tandem_agents.runtime.runstate import ensure_layout

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            layout = ensure_layout(run_dir)
            marker = run_dir / "active_worker_engine_sessions.json"
            marker.write_text(
                json.dumps(
                    {
                        "manager": {
                            "session_id": "session-1",
                            "run_id": "run-1",
                            "log_path": str(run_dir / "logs" / "manager.log"),
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

            with mock.patch("src.tandem_agents.core.phases.planning.delete_tandem_session") as delete_session:
                _cancel_active_manager_engine_session(ctx, "manager_prompt_timeout")
                for _ in range(50):
                    if delete_session.call_count and "manager.engine_cancelled" in layout["events"].read_text(
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
                ["manager.engine_cancel_requested", "manager.engine_cancelled"],
            )

    def test_append_deferred_repair_subtasks_resumes_serial_tail(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "deferred_subtasks": [
                        {"id": "subtask-2", "title": "Two", "files": ["src/two.py"]},
                        {"id": "subtask-3", "title": "Three", "files": ["src/three.py"]},
                    ]
                }
            },
            status={},
        )
        subtasks = [{"id": "subtask-1", "title": "Repair", "files": ["src/one.py"]}]

        _append_deferred_repair_subtasks(ctx, subtasks)

        self.assertEqual([subtask["id"] for subtask in subtasks], ["subtask-1", "subtask-2", "subtask-3"])
        self.assertIn("deferred this serial subtask", subtasks[1]["scope_note"])
        self.assertEqual(ctx.blackboard["repair"]["deferred_subtasks_appended"], 2)

    def test_repo_context_required_files_become_task_targets_when_absent(self) -> None:
        task = {
            "execution_kind": "code_edit",
            "source": {"type": "linear", "issue_id": "TAN-57"},
            "title": "Add regression coverage",
        }

        applied = _apply_repo_context_required_files_to_task(
            task,
            [
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
        )

        self.assertTrue(applied)
        self.assertEqual(
            task["target_files"],
            [
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
        )
        self.assertEqual(task["task_contract"]["target_files"], task["target_files"])

    def test_repo_context_required_files_do_not_override_explicit_targets(self) -> None:
        task = {
            "target_files": ["src/explicit.py"],
            "task_contract": {"target_files": ["src/explicit.py"]},
        }

        applied = _apply_repo_context_required_files_to_task(task, ["src/from-graph.py"])

        self.assertFalse(applied)
        self.assertEqual(task["target_files"], ["src/explicit.py"])

    def test_linear_code_edit_requires_worker_execution(self) -> None:
        self.assertTrue(
            _remote_code_task_requires_worker_execution(
                {
                    "execution_kind": "code_edit",
                    "source": {"type": "linear", "issue_id": "TAN-68"},
                }
            )
        )

    def test_linear_report_task_can_use_existing_satisfaction(self) -> None:
        self.assertFalse(
            _remote_code_task_requires_worker_execution(
                {
                    "execution_kind": "research_report",
                    "source": {"type": "linear", "issue_id": "TAN-68"},
                }
            )
        )

    def test_manual_code_edit_can_use_existing_satisfaction(self) -> None:
        self.assertFalse(
            _remote_code_task_requires_worker_execution(
                {
                    "execution_kind": "code_edit",
                    "source": {"type": "manual"},
                }
            )
        )

    def test_manager_prompt_timeout_defaults_and_ignores_invalid_env(self) -> None:
        self.assertEqual(_manager_prompt_timeout_seconds(SimpleNamespace(env={})), 90.0)
        self.assertEqual(
            _manager_prompt_timeout_seconds(
                SimpleNamespace(env={"ACA_MANAGER_PROMPT_TIMEOUT_SECONDS": "12.5"})
            ),
            12.5,
        )
        self.assertEqual(
            _manager_prompt_timeout_seconds(
                SimpleNamespace(env={"ACA_MANAGER_PROMPT_TIMEOUT_SECONDS": "not-a-number"})
            ),
            90.0,
        )

    def test_serial_subtask_limit_defaults_and_ignores_invalid_env(self) -> None:
        self.assertEqual(_serial_subtask_limit(SimpleNamespace(env={})), 4)
        self.assertEqual(_serial_subtask_limit(SimpleNamespace(env={"ACA_SERIAL_SUBTASK_LIMIT": "2"})), 2)
        self.assertEqual(
            _serial_subtask_limit(SimpleNamespace(env={"ACA_SERIAL_SUBTASK_LIMIT": "not-a-number"})),
            4,
        )

    def test_dense_subtasks_split_into_serial_slices_when_swarm_disabled(self) -> None:
        ctx = SimpleNamespace(cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=False, max_workers=3)))
        subtasks = [
            {
                "id": "subtask-1",
                "title": "Repository worktree/run metadata primitives",
                "goal": "Implement all repository lifecycle behavior.",
                "files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "acceptance_criteria": [
                    "Create a worktree.",
                    "Create a branch.",
                    "Pin the base revision.",
                    "Track touched files.",
                    "Track generated artifacts.",
                    "Detect overlaps.",
                    "Clean up stale leases.",
                ],
            }
        ]

        _split_dense_serial_subtasks(ctx, subtasks, max_acceptance_criteria=3)

        self.assertEqual([item["id"] for item in subtasks], ["subtask-1-part-1", "subtask-1-part-2", "subtask-1-part-3"])
        self.assertEqual([len(item["acceptance_criteria"]) for item in subtasks], [3, 3, 1])
        self.assertTrue(all(item["files"] == subtasks[0]["files"] for item in subtasks))
        self.assertIn("serial slices", subtasks[0]["scope_note"])

    def test_dense_subtasks_split_into_serial_slices_when_worker_limit_is_one(self) -> None:
        ctx = SimpleNamespace(cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=False, max_workers=1)))
        subtasks = [
            {
                "id": "subtask-1",
                "title": "Spend-safe slice",
                "acceptance_criteria": ["one", "two", "three", "four"],
                "files": ["src/a.py"],
                "target_files": ["src/a.py"],
            }
        ]

        _split_dense_serial_subtasks(ctx, subtasks, max_acceptance_criteria=2)

        self.assertEqual([item["id"] for item in subtasks], ["subtask-1-part-1", "subtask-1-part-2"])
        self.assertEqual([item["acceptance_criteria"] for item in subtasks], [["one", "two"], ["three", "four"]])
        self.assertTrue(all("Only one worker slice runs at a time" in item["scope_note"] for item in subtasks))

    def test_dense_subtasks_are_not_split_when_swarm_enabled(self) -> None:
        ctx = SimpleNamespace(cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=True)))
        subtasks = [
            {
                "id": "subtask-1",
                "title": "Parallel-safe slice",
                "acceptance_criteria": ["one", "two", "three", "four"],
                "files": ["src/a.py"],
                "target_files": ["src/a.py"],
            }
        ]

        _split_dense_serial_subtasks(ctx, subtasks, max_acceptance_criteria=2)

        self.assertEqual(len(subtasks), 1)
        self.assertEqual(subtasks[0]["id"], "subtask-1")
        self.assertEqual(subtasks[0]["acceptance_criteria"], ["one", "two", "three", "four"])

    def test_completed_repair_worker_results_survive_narrower_retry_plan(self) -> None:
        ctx = SimpleNamespace(
            repo_path=Path("/workspace/repos/example"),
            blackboard={
                "repair": {"completed_subtask_ids": ["subtask-1", "subtask-2"]},
                "workers": [
                    {
                        "worker_id": "worker-1",
                        "subtask_id": "subtask-1",
                        "status": "completed",
                        "returncode": 0,
                        "write_required": True,
                    },
                    {
                        "worker_id": "worker-2",
                        "subtask_id": "subtask-2",
                        "status": "completed",
                        "returncode": 0,
                        "write_required": True,
                    },
                ],
            },
        )

        carried = _completed_repair_worker_results(ctx, {"subtask-1"})

        self.assertEqual([result["subtask_id"] for result in carried], ["subtask-2"])
        self.assertEqual(carried[0]["status"], "skipped_existing")
        self.assertFalse(carried[0]["write_required"])
        self.assertTrue(carried[0]["verified_existing"])

    def test_partial_diff_artifact_matches_retry_subtask_by_changed_file_overlap(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-from-previous-plan",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/worker-2.patch",
                            "changed_files": [
                                "crates/eval/src/scoring.rs",
                                "crates/eval/tests/scored_version_model.rs",
                            ],
                            "worker_output_excerpt": (
                                "Remaining implementation blockers: "
                                "BoundedExposureScore::passes() is missing."
                            ),
                        }
                    ]
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["crates/eval/src/security_scenarios.rs"],
                "target_files": ["crates/eval/src/security_scenarios.rs"],
            },
            {
                "id": "subtask-2",
                "files": ["crates/eval/src/scoring.rs"],
                "target_files": ["crates/eval/src/scoring.rs"],
            },
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(subtasks[1]["carry_forward_patch"], "/runs/run-1/artifacts/worker-2.patch")
        self.assertIn("crates/eval/tests/scored_version_model.rs", subtasks[1]["files"])
        self.assertIn("crates/eval/tests/scored_version_model.rs", subtasks[1]["target_files"])
        self.assertEqual(
            subtasks[1]["repair_changed_files"],
            ["crates/eval/src/scoring.rs", "crates/eval/tests/scored_version_model.rs"],
        )
        self.assertIn("BoundedExposureScore::passes() is missing", subtasks[1]["scope_note"])
        self.assertIn(
            "Resolve the recovered partial-diff blocker",
            subtasks[1]["acceptance_criteria"][0],
        )
        self.assertIn("finish them before adding new scope", subtasks[1]["scope_note"])

    def test_engine_timeout_partial_diff_is_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": [
                                "src/tandem_agents/api/worktree_isolation.py",
                                "src/tandem_agents/api/worktree_isolation_test.py",
                            ],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt did not finish within 300s.\n\n"
                                "ACA preserved this partial worker diff because the Tandem engine stalled before a terminal response.\n"
                                "The partial diff is not treated as a completed worker result; retry or block with this evidence.\n"
                                "Changed files:\n"
                                "- src/tandem_agents/api/worktree_isolation.py\n"
                                "- src/tandem_agents/api/worktree_isolation_test.py"
                            ),
                        }
                    ]
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/api/worktree_isolation.py"],
                "target_files": ["src/tandem_agents/api/worktree_isolation.py"],
                "acceptance_criteria": [
                    "Implement the full intake runtime, manifest tracking, conflict detection, and PR metadata workflow.",
                ],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertTrue(subtasks[0]["write_required"])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/api/worktree_isolation.py",
                "src/tandem_agents/api/worktree_isolation_test.py",
            ],
        )
        self.assertNotIn("discarded_partial_diff_patch", subtasks[0])
        self.assertTrue(subtasks[0]["write_required"])
        self.assertIn(
            "Finish the preserved partial worker diff",
            subtasks[0]["goal"],
        )
        self.assertIn(
            "Resolve the recovered partial-diff blocker",
            subtasks[0]["acceptance_criteria"][0],
        )
        self.assertNotIn("full intake runtime", "\n".join(subtasks[0]["acceptance_criteria"]))
        self.assertIn("repair_deferred_acceptance_criteria", subtasks[0])
        self.assertNotIn("not treated as a completed worker result", subtasks[0]["scope_note"])

    def test_off_track_testless_partial_diff_is_not_carried_forward(self) -> None:
        parent_targets = [
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
            "src/tandem_agents/core/phases/task_intake.py",
        ]
        ctx = SimpleNamespace(
            task={"target_files": parent_targets},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: "
                                "after 218s it had changed only non-test files while required test files were "
                                "src/tandem_agents/core/repository/repository_test.py."
                            ),
                        }
                    ],
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/core/repository/repository.py"],
                "target_files": ["src/tandem_agents/core/repository/repository.py"],
                "acceptance_criteria": [
                    "Do not expand into unrelated manager scope beyond the preserved patch.",
                    "Add regression coverage for per-issue repository isolation.",
                ],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["discarded_partial_diff_patch"],
            "/runs/run-1/artifacts/worker-1.patch",
        )
        self.assertEqual(subtasks[0]["files"], parent_targets)
        self.assertEqual(subtasks[0]["target_files"], parent_targets)
        self.assertEqual(subtasks[0]["repair_parent_target_files"], parent_targets)
        self.assertIn(
            "the worker drifted off the required test-first path",
            subtasks[0]["repair_failure_summary"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Keep repair edits scoped to the parent task target files", criteria)
        self.assertNotIn("Do not expand into unrelated manager scope", criteria)
        self.assertIn("repository_test.py", criteria)
        self.assertIn("Start from the clean target files", subtasks[0]["scope_note"])
        self.assertIn("repository_test.py", subtasks[0]["scope_note"])

    def test_testless_partial_diff_gets_deterministic_repair_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/core/phases/task_intake.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: "
                                "after 185s it had changed only non-test files while required test files were "
                                "src/tandem_agents/core/repository/repository_test.py."
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/repository/repository_test.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/phases/task_intake.py",
            ],
        )
        self.assertEqual(subtask["target_files"], subtask["files"])
        self.assertEqual(
            subtask["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Read and edit the required test file", criteria)
        self.assertIn("Do not copy or replay the rejected partial patch", criteria)
        self.assertNotIn("/runs/run-1/artifacts/worker-1.patch", criteria)
        self.assertEqual(
            subtask["repair_parent_target_files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
                "src/tandem_agents/core/phases/task_intake.py",
            ],
        )

    def test_testless_deterministic_repair_defers_out_of_contract_required_tests(self) -> None:
        parent_targets = [
            "src/tandem_agents/core/phases/task_intake.py",
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
            "src/tandem_agents/core/phases/finalize.py",
            "src/tandem_agents/core/phases/pr_body.py",
        ]
        ctx = SimpleNamespace(
            task={"target_files": list(parent_targets)},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/phases/finalize.py",
                                "src/tandem_agents/core/phases/pr_body.py",
                                "src/tandem_agents/runtime/operator_dashboard.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: "
                                "after 185s it had changed only non-test files while required test files were "
                                "src/tandem_agents/core/repository/repository_test.py, "
                                "src/tandem_agents/runtime/operator_dashboard_test.py, "
                                "src/tandem_agents/runtime/operator_view_test.py."
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        expected_active_files = list(dict.fromkeys(["src/tandem_agents/core/repository/repository_test.py", *parent_targets]))
        self.assertEqual(subtask["files"], expected_active_files)
        self.assertEqual(subtask["target_files"], subtask["files"])
        self.assertEqual(subtask["repair_requires_test_followup"], ["src/tandem_agents/core/repository/repository_test.py"])
        active_text = "\n".join(subtask["files"])
        self.assertNotIn("operator_dashboard", active_text)
        self.assertNotIn("operator_view", active_text)
        self.assertEqual(
            subtask["repair_deferred_files"],
            [
                "src/tandem_agents/runtime/operator_dashboard_test.py",
                "src/tandem_agents/runtime/operator_view_test.py",
                "src/tandem_agents/runtime/operator_dashboard.py",
            ],
        )

    def test_test_only_timeout_partial_keeps_production_target_active(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["src/tandem_agents/api/main_test.py"],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt did not finish within 300s.\n"
                                "Changed files:\n"
                                "- src/tandem_agents/api/main_test.py"
                            ),
                        }
                    ]
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/api/main.py", "src/tandem_agents/api/main_test.py"],
                "target_files": ["src/tandem_agents/api/main.py", "src/tandem_agents/api/main_test.py"],
                "acceptance_criteria": [
                    "Run startup creates or validates a dedicated git worktree.",
                    "Tests cover distinct worktree paths.",
                ],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertEqual(
            subtasks[0]["files"],
            ["src/tandem_agents/api/main.py", "src/tandem_agents/api/main_test.py"],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_requires_production_followup"],
            ["src/tandem_agents/api/main.py"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("preserved diff is test-only", criteria)
        self.assertIn("Do not mark this repair complete with a test-only diff", criteria)
        self.assertIn("src/tandem_agents/api/main.py", criteria)

    def test_test_only_partial_diff_gets_deterministic_repair_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/runtime/operator_dashboard_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": (
                                "Worker changed only required test files for a regression subtask: "
                                "after 188s it had not made the required production change."
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtask["target_files"], subtask["files"])
        self.assertEqual(
            subtask["repair_requires_production_followup"],
            ["src/tandem_agents/core/repository/repository.py"],
        )
        self.assertTrue(subtask["deterministic_partial_diff_repair"])
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Make the first new repair edit in the required production file", criteria)
        self.assertIn("Do not mark this repair complete with a test-only diff", criteria)
        self.assertNotIn("operator_dashboard_test.py", "\n".join(subtask["files"]))

    def test_test_only_partial_diff_derives_sibling_production_target_without_parent_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            config_dir = repo_path / "src" / "tandem_agents" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config_loader.py").write_text("def load():\n    return None\n", encoding="utf-8")
            (config_dir / "config_loader_test.py").write_text("import unittest\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={},
                repo_path=repo_path,
                blackboard={
                    "repair": {
                        "partial_diff_artifacts": [
                            {
                                "subtask_id": "subtask-1",
                                "worker_id": "worker-1",
                                "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                                "changed_files": ["src/tandem_agents/config/config_loader_test.py"],
                                "worker_output_excerpt": (
                                    "Worker changed only required test files for a regression subtask: "
                                    "after 184s it had not made the required production change."
                                ),
                            }
                        ],
                    }
                },
            )

            plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
        )
        self.assertEqual(subtask["target_files"], subtask["files"])
        self.assertEqual(
            subtask["repair_requires_production_followup"],
            ["src/tandem_agents/config/config_loader.py"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Make the first new repair edit in the required production file", criteria)
        self.assertIn("Do not mark this repair complete with a test-only diff", criteria)

    def test_test_only_partial_diff_prefers_failed_subtask_targets_over_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            config_dir = repo_path / "src" / "tandem_agents" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config_loader.py").write_text("def load():\n    return None\n", encoding="utf-8")
            (config_dir / "config_loader_test.py").write_text("import unittest\n", encoding="utf-8")
            (repo_path / "scripts").mkdir()
            (repo_path / "scripts" / "bootstrap_config.js").write_text("module.exports = {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={},
                repo_path=repo_path,
                blackboard={
                    "repair": {
                        "partial_diff_artifacts": [
                            {
                                "subtask_id": "subtask-2",
                                "worker_id": "worker-2",
                                "patch_path": "/runs/run-1/artifacts/worker-2.patch",
                                "changed_files": ["src/tandem_agents/config/config_loader_test.py"],
                                "subtask_target_files": [
                                    "scripts/bootstrap_config.js",
                                    "src/tandem_agents/config/config_loader_test.py",
                                ],
                                "worker_output_excerpt": (
                                    "Worker changed only required test files for a regression subtask: "
                                    "after 182s it had not made the required production change."
                                ),
                            }
                        ],
                    }
                },
            )

            plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(
            subtask["files"],
            [
                "scripts/bootstrap_config.js",
                "src/tandem_agents/config/config_loader_test.py",
            ],
        )
        self.assertEqual(
            subtask["repair_requires_production_followup"],
            ["scripts/bootstrap_config.js"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("scripts/bootstrap_config.js", criteria)
        self.assertNotIn("config_loader.py", criteria)

    def test_complementary_source_and_test_partials_get_combined_verify_plan(self) -> None:
        ctx = SimpleNamespace(
            task={},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test.patch",
                            "changed_files": ["src/tandem_agents/config/config_loader_test.py"],
                            "worker_output_excerpt": (
                                "Worker changed only required test files for a regression subtask: "
                                "after 184s it had not made the required production change."
                            ),
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source.patch",
                            "changed_files": ["src/tandem_agents/config/config_loader.py"],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: after 185s "
                                "it had changed only non-test files while required test files were "
                                "src/tandem_agents/config/config_loader_test.py."
                            ),
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Verify complementary source and test partial diffs")
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
        )
        self.assertEqual(
            subtask["carry_forward_patches"],
            ["/runs/run-1/artifacts/source.patch", "/runs/run-1/artifacts/test.patch"],
        )
        self.assertTrue(subtask["repair_verification_first"])
        self.assertFalse(subtask["write_required"])
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("ACA applied the preserved production patch and test patch", criteria)
        self.assertIn("Run the narrowest deterministic verification", criteria)

    def test_sticky_production_target_becomes_active_for_test_only_timeout_partial(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 2,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["src/tandem_agents/api/main_test.py"],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt did not finish within 300s.\n"
                                "Changed files:\n"
                                "- src/tandem_agents/api/main_test.py"
                            ),
                        }
                    ],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/api/main_test.py"],
                "target_files": ["src/tandem_agents/api/main_test.py"],
                "acceptance_criteria": ["Finish the preserved timeout tests."],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)
        _merge_or_defer_sticky_expected_files(
            ctx,
            subtasks,
            ["src/tandem_agents/api/main_test.py"],
            [
                "src/tandem_agents/api/main_test.py",
                "src/tandem_agents/api/main.py",
            ],
        )

        self.assertEqual(
            subtasks[0]["files"],
            ["src/tandem_agents/api/main.py", "src/tandem_agents/api/main_test.py"],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_requires_production_followup"],
            ["src/tandem_agents/api/main.py"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("preserved diff is test-only", criteria)
        self.assertIn("Do not mark this repair complete with a test-only diff", criteria)
        self.assertIn("src/tandem_agents/api/main.py", criteria)

    def test_self_referential_partial_diff_is_not_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": [
                                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                            ],
                            "worker_output_excerpt": (
                                "The added coverage appears self-referential: it defines a test-only constant "
                                "and does not appear to exercise actual GitHub Projects readiness logic."
                            ),
                        }
                    ]
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                "target_files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                "acceptance_criteria": [
                    "Do not expand the edit set beyond `crates/tandem-server/src/http/coder_parts/part09.rs`.",
                    "Run the narrow relevant Rust test target.",
                ],
            },
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["discarded_partial_diff_patch"],
            "/runs/run-1/artifacts/worker-1.patch",
        )
        self.assertIn("Replace the rejected or incomplete partial-diff approach", subtasks[0]["acceptance_criteria"][0])
        self.assertIn("ACA rejected the preserved partial worker diff", subtasks[0]["scope_note"])
        self.assertIn("helper-only or self-referential", subtasks[0]["repair_failure_summary"])
        self.assertNotIn("/runs/run-1/artifacts/worker-1.patch", "\n".join(subtasks[0]["acceptance_criteria"]))

    def test_terminalized_message_formatting_partial_diff_is_not_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "crates/tandem-server/src/http/coder_parts/part05.rs",
                    "crates/tandem-server/src/http/coder_parts/part09.rs",
                    "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["crates/tandem-server/src/http/mcp.rs"],
                            "worker_output_excerpt": (
                                "What the diff appears to implement:\n"
                                "- Adds a GitHub Projects schema-drift readiness message constant.\n"
                                "- Adds helper functions to format a degraded/readiness error JSON response.\n"
                                "Verification:\n"
                                "- verification not run\n"
                                "Remaining implementation blockers:\n"
                                "- The regression appears limited to message formatting; the excerpt does not show "
                                "this readiness error being wired into the actual GitHub Projects read/readiness "
                                "schema-drift path.\n"
                                "- The JSON readiness helper itself is not covered by the added test in the excerpt.\n"
                                "The partial diff is not treated as a completed worker result; retry or block "
                                "with this evidence."
                            ),
                        }
                    ]
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                "target_files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
            },
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["discarded_partial_diff_patch"],
            "/runs/run-1/artifacts/worker-1.patch",
        )
        self.assertEqual(
            subtasks[0]["files"],
            [
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                "crates/tandem-server/src/http/coder_parts/part05.rs",
                "crates/tandem-server/src/http/coder_parts/part09.rs",
            ],
        )
        self.assertEqual(
            subtasks[0]["target_files"],
            [
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                "crates/tandem-server/src/http/coder_parts/part05.rs",
                "crates/tandem-server/src/http/coder_parts/part09.rs",
            ],
        )
        self.assertEqual(
            subtasks[0]["repair_parent_target_files"],
            [
                "crates/tandem-server/src/http/coder_parts/part05.rs",
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
        )
        self.assertIn("Replace the rejected or incomplete partial-diff approach", subtasks[0]["acceptance_criteria"][0])
        self.assertIn(
            "Keep repair edits scoped to the parent task target files",
            "\n".join(subtasks[0]["acceptance_criteria"]),
        )
        self.assertNotIn(
            "Do not expand the edit set beyond",
            "\n".join(subtasks[0]["acceptance_criteria"]),
        )
        self.assertIn("Failure summary:", subtasks[0]["scope_note"])
        self.assertIn("not wired into the production path", subtasks[0]["repair_failure_summary"])
        self.assertNotIn("Recovered partial-diff blocker/context", subtasks[0]["scope_note"])
        self.assertIn("repair_deferred_acceptance_criteria", subtasks[0])
        self.assertNotIn("/runs/run-1/artifacts/worker-1.patch", "\n".join(subtasks[0]["acceptance_criteria"]))

    def test_unproductive_partial_diff_is_not_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "crates/tandem-server/src/http/coder_parts/part09.rs",
                    "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                            "worker_output_excerpt": (
                                "Worker produced an unproductive partial diff: worker diff changes only "
                                "string wording in tests. ACA preserved the patch and abandoned this worker "
                                "instead of waiting for another engine timeout."
                            ),
                        }
                    ]
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                "target_files": ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
            },
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["discarded_partial_diff_patch"],
            "/runs/run-1/artifacts/worker-1.patch",
        )
        self.assertIn("ACA flagged the diff as unproductive", subtasks[0]["repair_failure_summary"])
        self.assertIn("did not add meaningful regression coverage", subtasks[0]["repair_failure_summary"])
        self.assertIn(
            "Start from the clean target files",
            subtasks[0]["scope_note"],
        )

    def test_runaway_partial_diff_is_not_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "crates/tandem-server/src/http/coder_parts/part09.rs",
                    "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": [
                                "crates/tandem-server/src/http/coder_parts/part09.rs",
                                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                            ],
                            "worker_output_excerpt": (
                                "Worker diff exceeded ACA runaway guard (54729819 bytes across "
                                "1560554 lines; max 1000000). ACA preserved a clipped summary and "
                                "abandoned this worker instead of writing a giant patch artifact."
                            ),
                        }
                    ]
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                "target_files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
            },
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["discarded_partial_diff_patch"],
            "/runs/run-1/artifacts/worker-1.patch",
        )
        self.assertIn("ACA flagged the diff as runaway-sized", subtasks[0]["repair_failure_summary"])
        self.assertIn(
            "Start from the clean target files",
            subtasks[0]["scope_note"],
        )

    def test_extra_partial_diff_repair_attempt_keeps_only_carried_subtask(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 3,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "scope_note": "existing",
                "files": [
                    "crates/eval/src/scoring.rs",
                    "crates/eval/src/trace.rs",
                    "crates/eval/tests/trace_model.rs",
                ],
                "target_files": [
                    "crates/eval/src/scoring.rs",
                    "crates/eval/src/trace.rs",
                    "crates/eval/tests/trace_model.rs",
                ],
                "repair_changed_files": ["crates/eval/src/scoring.rs"],
            },
            {"id": "subtask-2"},
            {"id": "subtask-3"},
        ]

        _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)

        self.assertEqual([subtask["id"] for subtask in subtasks], ["subtask-1"])
        self.assertEqual(subtasks[0]["files"], ["crates/eval/src/scoring.rs"])
        self.assertEqual(subtasks[0]["target_files"], ["crates/eval/src/scoring.rs"])
        self.assertEqual(
            subtasks[0]["repair_deferred_files"],
            ["crates/eval/src/trace.rs", "crates/eval/tests/trace_model.rs"],
        )
        self.assertIn("narrowed this extra repair attempt", subtasks[0]["scope_note"])
        self.assertIn("Active repair targets are:", subtasks[0]["scope_note"])

    def test_repair_retry_subtask_ids_do_not_collide_with_completed_prior_attempt(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 2,
                    "partial_diff_artifacts": [
                        {"subtask_id": "subtask-2", "patch_path": "/runs/run-1/artifacts/worker-2.patch"}
                    ],
                    "completed_subtask_ids": ["subtask-1"],
                }
            }
        )
        subtasks = [
            {"id": "subtask-1", "title": "Repair", "scope_note": "existing"},
            {"id": "subtask-2", "title": "Other"},
        ]

        _namespace_repair_retry_subtask_ids(ctx, subtasks)

        self.assertEqual(subtasks[0]["id"], "repair-attempt-2-subtask-1")
        self.assertEqual(subtasks[0]["repair_original_subtask_id"], "subtask-1")
        self.assertEqual(subtasks[1]["id"], "subtask-2")
        self.assertIn("completed-subtask carry-forward", subtasks[0]["scope_note"])

    def test_source_only_timeout_partial_keeps_declared_test_file_active(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 3,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "repair_changed_files": ["src/tandem_agents/core/repository/repository.py"],
                "repair_worker_output_excerpt": "ENGINE_PROMPT_TIMEOUT before terminal response.",
                "acceptance_criteria": [
                    "Repository tests cover successful branch/worktree creation without mutating the shared checkout.",
                ],
            },
            {"id": "subtask-2"},
        ]

        _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)

        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        self.assertIn("required test file", "\n".join(subtasks[0]["acceptance_criteria"]))

    def test_source_and_test_verifiable_partial_becomes_verification_first(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 3,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "write_required": True,
                "files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/core/phases/task_intake.py",
                ],
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/core/phases/task_intake.py",
                ],
                "repair_changed_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "repair_worker_output_excerpt": (
                    "Worker produced a source plus required-test partial diff but did not return "
                    "a terminal result after 250s. ACA preserved the patch and moved to a smaller "
                    "verification/fix retry instead of waiting for another engine timeout."
                ),
            },
            {"id": "subtask-2"},
        ]

        _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)

        self.assertEqual([subtask["id"] for subtask in subtasks], ["subtask-1"])
        self.assertFalse(subtasks[0]["write_required"])
        self.assertTrue(subtasks[0]["repair_verification_first"])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Run the narrow deterministic verification first", criteria)
        self.assertIn("without making another mandatory edit", criteria)

    def test_verification_first_prescreen_keeps_write_not_required(self) -> None:
        subtask = {"pre_satisfied": False, "repair_verification_first": True}

        self.assertFalse(_write_required_after_prescreen(subtask))

    def test_normal_unsatisfied_prescreen_requires_write(self) -> None:
        subtask = {"pre_satisfied": False}

        self.assertTrue(_write_required_after_prescreen(subtask))

    def test_extra_source_only_partial_keeps_required_test_file_active(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 4,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "repair_changed_files": ["src/tandem_agents/core/repository/repository.py"],
                "repair_worker_output_excerpt": (
                    "Worker drifted off the required regression/test coverage path: after 185s "
                    "it had changed only non-test files while required test files were "
                    "src/tandem_agents/core/repository/repository_test.py."
                ),
            },
            {"id": "subtask-2"},
        ]

        _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)

        self.assertEqual([subtask["id"] for subtask in subtasks], ["subtask-1"])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("read and edit the required test file", criteria)
        self.assertIn("repository_test.py", criteria)
        self.assertNotIn("repository_test.py", "\n".join(subtasks[0].get("repair_deferred_files", [])))

    def test_extra_partial_diff_repair_defers_sticky_expected_files(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 3,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "files": ["src/tandem_agents/api/worktrees.py"],
                "target_files": ["src/tandem_agents/api/worktrees.py"],
            }
        ]

        _merge_or_defer_sticky_expected_files(
            ctx,
            subtasks,
            ["src/tandem_agents/api/worktrees.py"],
            [
                "src/tandem_agents/api/worktrees.py",
                "src/tandem_agents/api/main.py",
                "src/tandem_agents/api/main_test.py",
            ],
        )

        self.assertEqual(subtasks[0]["files"], ["src/tandem_agents/api/worktrees.py"])
        self.assertEqual(subtasks[0]["target_files"], ["src/tandem_agents/api/worktrees.py"])
        self.assertEqual(
            subtasks[0]["repair_deferred_files"],
            ["src/tandem_agents/api/main.py", "src/tandem_agents/api/main_test.py"],
        )
        self.assertNotIn("scope_note", subtasks[0])

    def test_extra_test_only_partial_keeps_sticky_production_target_active(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 3,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "files": ["src/tandem_agents/api/main_test.py"],
                "target_files": ["src/tandem_agents/api/main_test.py"],
                "repair_changed_files": ["src/tandem_agents/api/main_test.py"],
                "acceptance_criteria": ["Finish the preserved timeout tests."],
            }
        ]

        _merge_or_defer_sticky_expected_files(
            ctx,
            subtasks,
            ["src/tandem_agents/api/main_test.py"],
            [
                "src/tandem_agents/api/main_test.py",
                "src/tandem_agents/api/main.py",
                "docs/follow-up.md",
            ],
        )

        self.assertEqual(
            subtasks[0]["files"],
            ["src/tandem_agents/api/main.py", "src/tandem_agents/api/main_test.py"],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_requires_production_followup"],
            ["src/tandem_agents/api/main.py"],
        )
        self.assertEqual(subtasks[0].get("repair_deferred_files"), ["docs/follow-up.md"])
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Do not mark this repair complete with a test-only diff", criteria)
        self.assertIn("src/tandem_agents/api/main.py", criteria)
        self.assertNotIn("docs/follow-up.md", criteria)
        self.assertIn("sticky production follow-up targets active", subtasks[0]["scope_note"])

    def test_carried_test_only_partial_defers_unpaired_sticky_files_on_base_retry(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 2,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        subtasks = [
            {
                "id": "subtask-1",
                "carry_forward_patch": "/runs/run-1/artifacts/worker.patch",
                "files": ["src/tandem_agents/core/repository/repository_test.py"],
                "target_files": ["src/tandem_agents/core/repository/repository_test.py"],
                "repair_changed_files": [
                    "src/tandem_agents/core/repository/repository_test.py",
                    "__aca_temp_probe.txt",
                ],
                "acceptance_criteria": ["Finish the preserved repository isolation tests."],
            }
        ]

        _merge_or_defer_sticky_expected_files(
            ctx,
            subtasks,
            ["src/tandem_agents/core/repository/repository_test.py"],
            [
                "src/tandem_agents/core/repository/repository_test.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/runtime/operator_dashboard.py",
            ],
        )

        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_requires_production_followup"],
            ["src/tandem_agents/core/repository/repository.py"],
        )
        self.assertEqual(
            subtasks[0]["repair_deferred_files"],
            [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/runtime/operator_dashboard.py",
            ],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("src/tandem_agents/core/repository/repository.py", criteria)
        self.assertNotIn("src/tandem_agents/core/phases/task_intake.py", criteria)
        self.assertNotIn("src/tandem_agents/runtime/operator_dashboard.py", criteria)

    def test_rejected_partial_defers_unrelated_sticky_expected_files(self) -> None:
        ctx = SimpleNamespace(
            blackboard={
                "repair": {
                    "attempt": 2,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [{"patch_path": "/runs/run-1/artifacts/worker.patch"}],
                }
            }
        )
        parent_targets = [
            "src/tandem_agents/core/phases/task_intake.py",
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
            "src/tandem_agents/core/phases/finalize.py",
            "src/tandem_agents/core/phases/pr_body.py",
        ]
        subtasks = [
            {
                "id": "subtask-1",
                "discarded_partial_diff_patch": "/runs/run-1/artifacts/worker.patch",
                "repair_parent_target_files": list(parent_targets),
                "files": [
                    "src/tandem_agents/runtime/operator_dashboard_test.py",
                    *parent_targets,
                ],
                "target_files": [
                    "src/tandem_agents/runtime/operator_dashboard_test.py",
                    *parent_targets,
                ],
                "acceptance_criteria": ["Replace the rejected partial diff."],
            }
        ]

        _merge_or_defer_sticky_expected_files(
            ctx,
            subtasks,
            list(parent_targets),
            [
                *parent_targets,
                "src/tandem_agents/runtime/operator_dashboard_test.py",
                "src/tandem_agents/runtime/operator_view_test.py",
                "src/tandem_agents/runtime/operator_dashboard.py",
            ],
        )

        self.assertEqual(subtasks[0]["files"], parent_targets)
        self.assertEqual(subtasks[0]["target_files"], parent_targets)
        self.assertEqual(
            subtasks[0]["repair_deferred_files"],
            [
                "src/tandem_agents/runtime/operator_dashboard_test.py",
                "src/tandem_agents/runtime/operator_view_test.py",
                "src/tandem_agents/runtime/operator_dashboard.py",
            ],
        )
        rendered = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertNotIn("operator_dashboard", rendered)
        self.assertIn("deferred sticky expected files", subtasks[0]["scope_note"])

    def test_normal_retry_keeps_sticky_expected_files_active(self) -> None:
        ctx = SimpleNamespace(blackboard={"repair": {"attempt": 2, "base_max_loops": 2}})
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/api/worktrees.py"],
                "target_files": ["src/tandem_agents/api/worktrees.py"],
                "scope_note": "existing",
            }
        ]

        _merge_or_defer_sticky_expected_files(
            ctx,
            subtasks,
            ["src/tandem_agents/api/worktrees.py"],
            [
                "src/tandem_agents/api/worktrees.py",
                "src/tandem_agents/api/main.py",
            ],
        )

        self.assertEqual(
            subtasks[0]["files"],
            ["src/tandem_agents/api/worktrees.py", "src/tandem_agents/api/main.py"],
        )
        self.assertEqual(
            subtasks[0]["target_files"],
            ["src/tandem_agents/api/worktrees.py", "src/tandem_agents/api/main.py"],
        )
        self.assertIn("must not narrow the run contract", subtasks[0]["scope_note"])

    def test_extra_rejected_partial_diff_repair_keeps_parent_targets(self) -> None:
        parent_targets = [
            "crates/tandem-server/src/http/coder_parts/part05.rs",
            "crates/tandem-server/src/http/coder_parts/part09.rs",
            "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
        ]
        ctx = SimpleNamespace(
            task={"target_files": parent_targets},
            blackboard={
                "repair": {
                    "attempt": 3,
                    "base_max_loops": 2,
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                            "worker_output_excerpt": (
                                "verification not run\n"
                                "The diff excerpt only shows helper/evaluator code and a fixture payload; "
                                "it does not show an actual regression test, assertions, or wiring.\n"
                                "The partial diff is not treated as a completed worker result; retry or block "
                                "with this evidence."
                            ),
                        }
                    ],
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                "target_files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                "acceptance_criteria": [
                    "Do not expand the edit set beyond `crates/tandem-server/src/http/coder_parts/part09.rs`.",
                    "Add regression coverage.",
                ],
            },
            {"id": "subtask-2", "files": ["docs/notes.md"], "target_files": ["docs/notes.md"]},
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)
        _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)

        self.assertEqual([subtask["id"] for subtask in subtasks], ["subtask-1"])
        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(subtasks[0]["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertEqual(subtasks[0]["files"], parent_targets)
        self.assertEqual(subtasks[0]["target_files"], parent_targets)
        self.assertEqual(subtasks[0]["repair_parent_target_files"], parent_targets)
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Keep repair edits scoped to the parent task target files", criteria)
        self.assertNotIn("Do not expand the edit set beyond", criteria)
        self.assertIn("Active repair targets are the parent task targets", subtasks[0]["scope_note"])
        self.assertIn("repair_deferred_acceptance_criteria", subtasks[0])
        self.assertNotIn("/runs/run-1/artifacts/worker-1.patch", "\n".join(subtasks[0]["acceptance_criteria"]))

    def test_align_python_test_targets_to_sibling_repo_convention(self) -> None:
        with self.subTest("top-level tests target rewrites only when repo uses sibling tests"):
            with tempfile.TemporaryDirectory() as tmp:
                repo_path = Path(tmp)
                (repo_path / "src" / "tandem_agents" / "api").mkdir(parents=True)
                (repo_path / "src" / "tandem_agents" / "api" / "main_test.py").write_text(
                    "import unittest\n",
                    encoding="utf-8",
                )
                subtasks = [
                    {
                        "id": "subtask-1",
                        "files": [
                            "src/tandem_agents/api/worktree_isolation.py",
                            "tests/test_worktree_isolation.py",
                        ],
                        "target_files": [
                            "src/tandem_agents/api/worktree_isolation.py",
                            "tests/test_worktree_isolation.py",
                        ],
                    }
                ]

                _align_python_test_targets_to_repo_conventions(repo_path, subtasks)

                expected = [
                    "src/tandem_agents/api/worktree_isolation.py",
                    "src/tandem_agents/api/worktree_isolation_test.py",
                ]
                self.assertEqual(subtasks[0]["files"], expected)
                self.assertEqual(subtasks[0]["target_files"], expected)
                self.assertIn("sibling *_test.py convention", subtasks[0]["scope_note"])

        with self.subTest("manager-prefixed top-level test target rewrites to clear source sibling"):
            with tempfile.TemporaryDirectory() as tmp:
                repo_path = Path(tmp)
                (repo_path / "src" / "tandem_agents" / "api").mkdir(parents=True)
                (repo_path / "src" / "tandem_agents" / "api" / "main_test.py").write_text(
                    "import unittest\n",
                    encoding="utf-8",
                )
                subtasks = [
                    {
                        "id": "subtask-1",
                        "files": [
                            "src/tandem_agents/api/worktrees.py",
                            "src/tandem_agents/api/main.py",
                            "tests/test_aca_worktrees.py",
                        ],
                        "target_files": [
                            "src/tandem_agents/api/worktrees.py",
                            "src/tandem_agents/api/main.py",
                            "tests/test_aca_worktrees.py",
                        ],
                    }
                ]

                _align_python_test_targets_to_repo_conventions(repo_path, subtasks)

                expected = [
                    "src/tandem_agents/api/worktrees.py",
                    "src/tandem_agents/api/main.py",
                    "src/tandem_agents/api/worktrees_test.py",
                ]
                self.assertEqual(subtasks[0]["files"], expected)
                self.assertEqual(subtasks[0]["target_files"], expected)
                self.assertIn("tests/test_aca_worktrees.py -> src/tandem_agents/api/worktrees_test.py", subtasks[0]["scope_note"])

    def test_mark_manager_planning_started_updates_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            ctx = SimpleNamespace(
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "phase": {"name": "task_resolution", "updated_at_ms": 1},
                    "blocker": {"active": False},
                    "metrics": {},
                },
                layout={"status": status_path},
            )

            _mark_manager_planning_started(ctx)

            self.assertEqual(ctx.status["phase"]["name"], "planning")
            self.assertEqual(ctx.status["phase"]["detail"], "manager planning")
            self.assertEqual(ctx.status["phase"]["role"], "manager")
            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["phase"]["name"], "planning")

    def test_manager_plan_sanitizes_absolute_partial_patch_paths(self) -> None:
        plan = {
            "subtasks": [
                {
                    "acceptance_criteria": [
                        "Read the preserved partial patch at /workspace/tandem-agents/runs/run-1/artifacts/worker.patch only as failure evidence.",
                        "Start from the preserved patch at `runs/run-1/artifacts/worker.patch` if useful.",
                        "Run the narrow test target.",
                    ],
                }
            ]
        }

        _sanitize_partial_diff_artifact_paths_in_plan(plan)

        criteria = plan["subtasks"][0]["acceptance_criteria"]
        rendered = "\n".join(criteria)
        self.assertNotIn("/workspace/", rendered)
        self.assertNotIn("runs/run-1/artifacts/worker.patch", rendered)
        self.assertIn("ACA's repair directive", criteria[0])
        self.assertIn("Run the narrow test target.", criteria)

    def test_manager_plan_from_stdout_rejects_plain_text(self) -> None:
        plan, error = _manager_plan_from_stdout("I used tools but could not answer cleanly.")

        self.assertIsNone(plan)
        self.assertIn("valid JSON object", error or "")

    def test_manager_plan_from_stdout_requires_contract_keys(self) -> None:
        plan, error = _manager_plan_from_stdout('{"summary":"ok","subtasks":[]}')

        self.assertIsNone(plan)
        self.assertIn("missing required key", error or "")

    def test_manager_plan_from_stdout_accepts_valid_contract(self) -> None:
        plan, error = _manager_plan_from_stdout(
            '{"summary":"ok","subtasks":[],"risks":[],"tests":[]}'
        )

        self.assertIsNone(error)
        self.assertEqual(plan, {"summary": "ok", "subtasks": [], "risks": [], "tests": []})

    def test_run_manager_prompt_treats_invalid_json_as_recoverable_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            status_path = run_dir / "status.json"
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(),
                repo_path=repo_path,
                task={
                    "task_id": "TAN-170",
                    "title": "Add worktree isolation",
                    "description": "Keep ACA runs isolated.",
                    "acceptance_criteria": ["Parallel issues do not share a mutable working directory."],
                },
                repo={"path": str(repo_path)},
                layout={
                    "run_dir": run_dir,
                    "artifacts": run_dir / "artifacts",
                    "logs": run_dir / "logs",
                    "events": run_dir / "events.jsonl",
                    "blackboard": run_dir / "blackboard.yaml",
                    "status": status_path,
                },
                run_dir=run_dir,
                run_id="run-1",
                blackboard={},
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "phase": {"name": "task_resolution", "detail": "task resolved"},
                    "blocker": {"active": False, "kind": None, "message": None, "owner_role": None},
                    "metrics": {},
                },
            )
            bad_stdout = (
                "I used tools for this request, but I couldn't turn the results into a clean final answer."
            )
            repo_context = SimpleNamespace(
                source="repo.context_bundle",
                fallback_used=False,
                error=None,
                artifact_path=str(run_dir / "artifacts" / "repo_context_bundle.json"),
                path_scope=".",
                required_files=[],
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="Suggested first reads:\n- src/tandem_agents/api/main.py",
            )

            with (
                mock.patch(
                    "src.tandem_agents.core.repository.repo_context.repo_context_for_task",
                    return_value=repo_context,
                ),
                mock.patch(
                    "src.tandem_agents.core.engine.engine.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5"},
                ),
                mock.patch(
                    "src.tandem_agents.core.engine.engine_runtime.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5"},
                ),
                mock.patch("src.tandem_agents.core.engine.engine.engine_env", return_value={}),
                mock.patch(
                    "src.tandem_agents.core.execution.runner_core._coordination_heartbeat",
                    return_value=contextlib.nullcontext(),
                ),
                mock.patch(
                    "src.tandem_agents.core.execution.worker.stream_tandem_prompt",
                    return_value={"returncode": 0, "stdout": bad_stdout},
                ),
            ):
                result = run_manager_prompt(ctx)

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "manager_invalid_plan")
            self.assertEqual(ctx.status["run"]["status"], "running")
            self.assertFalse(ctx.status["blocker"]["active"])
            self.assertIn("Falling back", ctx.status["phase"]["detail"])
            self.assertEqual(ctx.manager_plan["subtasks"], [])
            self.assertEqual(
                ctx.blackboard["manager_invalid_plan"]["reason"],
                "Manager planning did not return a valid JSON object.",
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            invalid_events = [event for event in events if event["type"] == "manager.invalid_plan"]
            self.assertEqual(invalid_events[-1]["payload"]["recoverable"], True)

    def test_run_manager_prompt_uses_deterministic_repo_context_plan_without_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            required_files = [
                "src/tandem_agents/core/scheduling/scheduler.py",
                "src/tandem_agents/core/scheduling/scheduler_test.py",
                "src/tandem_agents/core/phases/worker_dispatch.py",
                "src/tandem_agents/core/execution/runner_core.py",
                "src/tandem_agents/runtime/operator_view.py",
                "src/tandem_agents/runtime/operator_dashboard.py",
                "src/tandem_agents/runtime/operator_dashboard_test.py",
                "src/tandem_agents/config/config_types.py",
                "src/tandem_agents/config/config_loader.py",
                "src/tandem_agents/config/config_loader_test.py",
            ]
            for rel_path in required_files:
                path = repo_path / rel_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# test\n", encoding="utf-8")
            artifact_path = run_dir / "artifacts" / "repo_context_bundle.json"
            artifact_path.write_text(json.dumps({"bundle": {}, "graph_hints": {"required_files": required_files}}))
            status_path = run_dir / "status.json"
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}, swarm=SimpleNamespace(max_workers=4, enabled=False)),
                repo_path=repo_path,
                task={
                    "task_id": "TAN-173",
                    "title": "LACA-15 Add ACA throughput metrics, backpressure, and cost controls",
                    "description": "Add backpressure, cost controls, metrics, and cockpit visibility.",
                    "acceptance_criteria": [
                        "Add global and per-repo budget/concurrency caps.",
                        "Track issue cycle time, queue wait, active time, PR time, repair loops, merge time, token cost, tool calls, test time, and failure rate.",
                        "Operator can see active workers, queued issues, blocked issues, costs, and failures.",
                    ],
                    "execution_kind": "code_edit",
                    "source": {"type": "linear"},
                },
                repo={"path": str(repo_path)},
                layout={
                    "run_dir": run_dir,
                    "artifacts": run_dir / "artifacts",
                    "logs": run_dir / "logs",
                    "events": run_dir / "events.jsonl",
                    "blackboard": run_dir / "blackboard.yaml",
                    "status": status_path,
                },
                run_dir=run_dir,
                run_id="run-1",
                blackboard={},
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "phase": {"name": "task_resolution", "detail": "task resolved"},
                    "blocker": {"active": False, "kind": None, "message": None, "owner_role": None},
                    "metrics": {},
                },
            )
            repo_context = SimpleNamespace(
                source="repo.context_bundle",
                fallback_used=False,
                error=None,
                artifact_path=str(artifact_path),
                path_scope="src/tandem_agents",
                required_files=required_files,
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="Required edit files:\n" + "\n".join(f"- {path}" for path in required_files),
            )

            with (
                mock.patch(
                    "src.tandem_agents.core.repository.repo_context.repo_context_for_task",
                    return_value=repo_context,
                ),
                mock.patch(
                    "src.tandem_agents.core.engine.engine.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5"},
                ),
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt") as stream_prompt,
            ):
                result = run_manager_prompt(ctx)

            stream_prompt.assert_not_called()
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["engine"], {"skipped": True, "reason": "repo_context_required_files"})
            subtask_ids = [subtask["id"] for subtask in ctx.manager_plan["subtasks"]]
            self.assertIn("fallback-throughput-scheduler-controls", subtask_ids)
            self.assertIn("fallback-throughput-worker-metrics", subtask_ids)
            self.assertIn("fallback-throughput-operator-cockpit", subtask_ids)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "manager.deterministic_repo_context_plan")

    def test_invalid_manager_fallback_builds_deterministic_repo_context_subtasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            context_path = root / "repo_context_bundle.json"
            (repo_path / "src" / "tandem_agents" / "core" / "repository").mkdir(parents=True)
            (repo_path / "src" / "tandem_agents" / "core" / "phases").mkdir(parents=True)
            (repo_path / "src" / "tandem_agents" / "runtime").mkdir(parents=True)
            (repo_path / "src" / "tandem_agents" / "runtime" / "operator_dashboard.py").write_text(
                "def render(): pass\n",
                encoding="utf-8",
            )
            for rel_path in (
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/phases/finalize.py",
                "src/tandem_agents/core/phases/pr_body.py",
            ):
                (repo_path / rel_path).write_text("# target\n", encoding="utf-8")
            context_path.write_text(
                json.dumps(
                    {
                        "bundle": {
                            "suggested_first_reads": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/phases/finalize.py",
                                "src/tandem_agents/core/phases/pr_body.py",
                            ],
                            "likely_files": [
                                {"file_path": "src/tandem_agents/core/repository/repository.py"},
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=True, max_workers=3)),
                task={
                    "title": "Add worktree isolation",
                    "description": "Create one worktree and branch per claimed Linear issue.",
                    "acceptance_criteria": [
                        "Create one worktree and branch per claimed Linear issue.",
                        "Detect overlapping file edits across active ACA runs.",
                        "PR metadata links back to Linear issue and ACA run id.",
                    ],
                },
                manager_plan={"summary": "bad", "subtasks": [], "risks": [], "tests": []},
                repo={"path": str(repo_path)},
                repo_path=repo_path,
                blackboard={
                    "manager_invalid_plan": {"reason": "Manager planning did not return JSON."},
                    "repo_context": {
                        "artifact_path": str(context_path),
                        "required_files": [],
                    },
                },
            )
            setattr(ctx, "_manager_fallback_required", True)
            discovered_subtask = {
                "id": "subtask-1",
                "title": "Fallback",
                "goal": "Fallback",
                "files": ["src/tandem_agents/runtime/operator_dashboard.py"],
                "target_files": ["src/tandem_agents/runtime/operator_dashboard.py"],
                "acceptance_criteria": ["Do the task."],
            }

            with mock.patch(
                "src.tandem_agents.core.execution.runner_core._prepare_subtasks_with_discovery",
                return_value=(
                    ["src/tandem_agents/runtime/operator_dashboard.py"],
                    [dict(discovered_subtask)],
                ),
            ):
                discovered_files, subtasks = _prepare_subtasks(ctx)

            self.assertEqual(
                discovered_files,
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/phases/finalize.py",
                    "src/tandem_agents/core/phases/pr_body.py",
                ],
            )
            self.assertEqual(
                subtasks[0]["files"],
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
            )
            self.assertIn("worktree", " ".join(subtasks[0]["acceptance_criteria"]).lower())
            self.assertIn("repo-context fallback targets", subtasks[0]["scope_note"])
            self.assertEqual(subtasks[1]["files"], ["src/tandem_agents/core/phases/task_intake.py"])
            self.assertIn("overlapping", " ".join(subtasks[1]["acceptance_criteria"]).lower())
            self.assertEqual(
                subtasks[2]["files"],
                [
                    "src/tandem_agents/core/phases/finalize.py",
                    "src/tandem_agents/core/phases/pr_body.py",
                ],
            )
            self.assertIn("pr metadata", " ".join(subtasks[2]["acceptance_criteria"]).lower())

    def test_invalid_manager_fallback_returns_no_subtasks_without_safe_repo_context_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            repo_path.mkdir()
            context_path = root / "repo_context_bundle.json"
            context_path.write_text(
                json.dumps({"bundle": {"suggested_first_reads": ["src/missing.py"]}}),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=False, max_workers=1)),
                task={"title": "Add worktree isolation"},
                manager_plan={"summary": "bad", "subtasks": [], "risks": [], "tests": []},
                repo={"path": str(repo_path)},
                repo_path=repo_path,
                blackboard={
                    "manager_invalid_plan": {"reason": "Manager planning did not return JSON."},
                    "repo_context": {"artifact_path": str(context_path), "required_files": []},
                },
            )
            setattr(ctx, "_manager_fallback_required", True)

            with mock.patch(
                "src.tandem_agents.core.execution.runner_core._prepare_subtasks_with_discovery",
                return_value=(
                    ["src/tandem_agents/runtime/operator_dashboard.py"],
                    [
                        {
                            "id": "subtask-1",
                            "title": "Fallback",
                            "goal": "Fallback",
                            "files": ["src/tandem_agents/runtime/operator_dashboard.py"],
                            "acceptance_criteria": ["Do the task."],
                        }
                    ],
                ),
            ):
                discovered_files, subtasks = _prepare_subtasks(ctx)

            self.assertEqual(discovered_files, [])
            self.assertEqual(subtasks, [])

    def test_prepare_subtasks_preserves_serial_manager_plan_when_swarm_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=False, max_workers=1), env={}),
                task={"title": "Cap worker fan-out"},
                manager_plan={
                    "subtasks": [
                        {"id": "one", "title": "One", "goal": "First"},
                        {"id": "two", "title": "Two", "goal": "Second"},
                    ],
                },
                repo={"path": str(repo_path)},
                repo_path=repo_path,
                blackboard={},
            )

            with mock.patch(
                "src.tandem_agents.core.execution.runner_core._prepare_subtasks_with_discovery",
                return_value=([], [{"id": "subtask-1", "title": "Merged", "goal": "Merged"}]),
            ) as prepare:
                _prepare_subtasks(ctx)

            self.assertEqual(prepare.call_args.kwargs["merge_manager_subtasks"], False)
            self.assertEqual(prepare.call_args.args[3], 4)

    def test_prepare_subtasks_uses_configured_serial_subtask_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(
                    swarm=SimpleNamespace(enabled=False, max_workers=1),
                    env={"ACA_SERIAL_SUBTASK_LIMIT": "2"},
                ),
                task={"title": "Cap worker fan-out"},
                manager_plan={"subtasks": []},
                repo={"path": str(repo_path)},
                repo_path=repo_path,
                blackboard={},
            )

            with mock.patch(
                "src.tandem_agents.core.execution.runner_core._prepare_subtasks_with_discovery",
                return_value=([], []),
            ) as prepare:
                _prepare_subtasks(ctx)

            self.assertEqual(prepare.call_args.args[3], 2)


if __name__ == "__main__":
    unittest.main()
