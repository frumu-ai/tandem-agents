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
    _compact_focused_test_failure_output,
    _compact_partial_diff_repair_context,
    _constrain_extra_partial_diff_repair_subtasks,
    _completed_repair_worker_results,
    _deterministic_repair_plan_kind,
    _deterministic_testless_partial_diff_repair_plan,
    _mark_manager_planning_started,
    _manager_plan_from_stdout,
    _manager_prompt_timeout_grace_seconds,
    _manager_prompt_timeout_seconds,
    _merge_or_defer_sticky_expected_files,
    _namespace_repair_retry_subtask_ids,
    _partial_diff_focused_verification_context,
    _prepare_subtasks,
    _remote_code_task_requires_worker_execution,
    _sanitize_partial_diff_artifact_paths_in_plan,
    _serial_subtask_limit,
    _should_use_deterministic_repo_context_plan,
    _split_dense_serial_subtasks,
    _subtask_has_source_or_test_targets,
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

    def test_repo_context_required_files_flag_preserved_when_targets_match(self) -> None:
        task = {
            "target_files": ["src/from-graph.py", "src/from-graph_test.py"],
            "task_contract": {"target_files": ["src/from-graph.py", "src/from-graph_test.py"]},
        }

        applied = _apply_repo_context_required_files_to_task(
            task,
            ["src/from-graph.py", "src/from-graph_test.py"],
        )

        self.assertTrue(applied)
        self.assertEqual(task["target_files"], ["src/from-graph.py", "src/from-graph_test.py"])

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

    def test_manager_prompt_timeout_uses_inner_engine_budget_floor(self) -> None:
        self.assertEqual(_manager_prompt_timeout_grace_seconds(SimpleNamespace(env={})), 10.0)
        self.assertEqual(_manager_prompt_timeout_seconds(SimpleNamespace(env={})), 390.0)
        self.assertEqual(
            _manager_prompt_timeout_seconds(
                SimpleNamespace(env={"ACA_MANAGER_PROMPT_TIMEOUT_SECONDS": "12.5"})
            ),
            390.0,
        )
        self.assertEqual(
            _manager_prompt_timeout_seconds(
                SimpleNamespace(env={"ACA_MANAGER_PROMPT_TIMEOUT_SECONDS": "500"})
            ),
            500.0,
        )
        self.assertEqual(
            _manager_prompt_timeout_seconds(
                SimpleNamespace(env={"ACA_MANAGER_PROMPT_TIMEOUT_SECONDS": "not-a-number"})
            ),
            390.0,
        )

    def test_serial_subtask_limit_defaults_and_ignores_invalid_env(self) -> None:
        self.assertEqual(_serial_subtask_limit(SimpleNamespace(env={})), 1)
        self.assertEqual(_serial_subtask_limit(SimpleNamespace(env={"ACA_SERIAL_SUBTASK_LIMIT": "2"})), 2)
        self.assertEqual(
            _serial_subtask_limit(SimpleNamespace(env={"ACA_SERIAL_SUBTASK_LIMIT": "not-a-number"})),
            1,
        )

    def test_dense_subtasks_split_into_serial_slices_when_swarm_disabled(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                swarm=SimpleNamespace(enabled=False, max_workers=3),
                env={"ACA_SERIAL_SUBTASK_LIMIT": "3"},
            )
        )
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
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                swarm=SimpleNamespace(enabled=False, max_workers=1),
                env={"ACA_SERIAL_SUBTASK_LIMIT": "2"},
            )
        )
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

    def test_manager_fallback_dense_split_is_capped_to_serial_limit(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                swarm=SimpleNamespace(enabled=False, max_workers=1),
                env={"ACA_SERIAL_SUBTASK_LIMIT": "2"},
            ),
            blackboard={"manager_invalid_plan": {"reason": "timeout"}},
        )
        subtasks = [
            {
                "id": "fallback-repository-isolation",
                "title": "Repository isolation",
                "acceptance_criteria": ["one", "two", "three", "four", "five"],
                "files": ["src/repository.py"],
                "target_files": ["src/repository.py"],
            },
            {
                "id": "fallback-finalize",
                "title": "Finalize",
                "acceptance_criteria": ["six", "seven", "eight", "nine"],
                "files": ["src/finalize.py"],
                "target_files": ["src/finalize.py"],
            },
        ]

        _split_dense_serial_subtasks(ctx, subtasks, max_acceptance_criteria=2)

        self.assertEqual(
            [item["id"] for item in subtasks],
            ["fallback-repository-isolation-part-1", "fallback-repository-isolation-part-2"],
        )
        self.assertTrue(all("capped deterministic fallback" in item["scope_note"] for item in subtasks))
        self.assertEqual(ctx.blackboard["manager_fallback_serial_cap"]["limit"], 2)
        self.assertEqual(ctx.blackboard["manager_fallback_serial_cap"]["original_planned_workers"], 5)
        self.assertIn(
            "fallback-finalize-part-1",
            ctx.blackboard["manager_fallback_serial_cap"]["deferred_subtask_ids"],
        )

    def test_manager_fallback_cap_does_not_truncate_normal_serial_plan(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                swarm=SimpleNamespace(enabled=False, max_workers=1),
                env={"ACA_SERIAL_SUBTASK_LIMIT": "1"},
            ),
            blackboard={},
        )
        subtasks = [
            {
                "id": "subtask-1",
                "title": "Normal dense slice",
                "acceptance_criteria": ["one", "two", "three", "four"],
                "files": ["src/a.py"],
                "target_files": ["src/a.py"],
            }
        ]

        _split_dense_serial_subtasks(ctx, subtasks, max_acceptance_criteria=2)

        self.assertEqual([item["id"] for item in subtasks], ["subtask-1-part-1", "subtask-1-part-2"])
        self.assertNotIn("manager_fallback_serial_cap", ctx.blackboard)

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

    def test_docs_only_timeout_partial_diff_is_carried_forward_with_remaining_docs_target(self) -> None:
        ctx = SimpleNamespace(
            task={},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "explicit-task-targets",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "blocker_kind": "worker_incomplete_diff",
                            "changed_files": ["docs/ACA_SMOKE_HARNESS.md"],
                            "subtask_files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                            "subtask_target_files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response.\n"
                                "Changed files:\n"
                                "- `docs/ACA_SMOKE_HARNESS.md` - new file\n"
                                "Verification:\n"
                                "- verification not run\n"
                                "Remaining implementation blockers:\n"
                                "- `docs/README.md` should link to `docs/ACA_SMOKE_HARNESS.md`."
                            ),
                        }
                    ]
                }
            },
        )
        subtasks = [
            {
                "id": "explicit-task-targets",
                "files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                "target_files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                "acceptance_criteria": [
                    "Add docs/ACA_SMOKE_HARNESS.md describing the harness.",
                    "Link the new document from docs/README.md.",
                ],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("discarded_partial_diff_patch", subtasks[0])
        self.assertEqual(subtasks[0]["files"], ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"])
        self.assertEqual(subtasks[0]["target_files"], ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"])
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("preserved docs diff is already applied", criteria)
        self.assertIn("remaining declared docs target", criteria)
        self.assertNotIn("production-backed assertion", criteria)
        self.assertNotIn("repair_deferred_files", subtasks[0])

    def test_source_only_engine_timeout_with_declared_test_discards_patch(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
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
                    "Add required regression coverage for repository worktree isolation.",
                ],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertIn(
            "Replace the rejected or incomplete partial-diff approach",
            subtasks[0]["acceptance_criteria"][0],
        )

    def test_source_only_engine_timeout_uses_parent_paired_test_target(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/worker-2.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "changed_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                        }
                    ],
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-2",
                "files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                ],
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                ],
                "acceptance_criteria": ["Finish repository intake isolation with regression coverage."],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-2.patch")
        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_parent_target_files"],
            [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Replace the rejected or incomplete partial-diff approach", criteria)
        self.assertIn("repository_test.py", criteria)

    def test_off_track_testless_partial_diff_discards_non_reusable_source_patch(self) -> None:
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
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
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

        self.assertEqual(subtasks[0]["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertEqual(
            subtasks[0]["repair_parent_target_files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertIn(
            "changed only non-test files",
            subtasks[0]["repair_worker_output_excerpt"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Replace the rejected or incomplete partial-diff approach", criteria)
        self.assertIn("Keep repair edits scoped to the active repair target files", criteria)
        self.assertIn("repository_test.py", criteria)
        self.assertIn("ACA rejected the preserved partial worker diff", subtasks[0]["scope_note"])
        self.assertIn("repository_test.py", subtasks[0]["scope_note"])

    def test_failed_focused_verifiable_partial_diff_is_carried_forward_for_repair(self) -> None:
        parent_targets = [
            "src/tandem_agents/config/config_types.py",
            "src/tandem_agents/config/config_loader.py",
            "src/tandem_agents/config/config_loader_test.py",
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
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/config/config_types.py",
                                "src/tandem_agents/config/config_loader_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced a source plus required-test partial diff, "
                                "but focused tests failed: AttributeError: config.aca.throughput"
                            ),
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.config.config_loader_test",
                            ],
                            "verification_output_excerpt": (
                                "ERROR: config_loader_test\n"
                                "AttributeError: config.aca.throughput"
                            ),
                        }
                    ],
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/config/config_types.py"],
                "target_files": ["src/tandem_agents/config/config_types.py"],
                "acceptance_criteria": [
                    "Assert exact config.scheduler fields.",
                ],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("discarded_partial_diff_patch", subtasks[0])
        self.assertEqual(
            subtasks[0]["files"],
            [
                "src/tandem_agents/config/config_types.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
        )
        self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
        self.assertTrue(subtasks[0]["repair_verification_first"])
        self.assertTrue(subtasks[0]["write_required"])
        self.assertEqual(
            subtasks[0]["verification_commands"],
            ["python3 -m unittest src.tandem_agents.config.config_loader_test"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("python3 -m unittest src.tandem_agents.config.config_loader_test", criteria)
        self.assertIn("AttributeError: config.aca.throughput", criteria)
        self.assertIn("fix only the failing behavior", criteria)

    def test_compact_focused_failure_keeps_assertion_pairs_without_traceback_noise(self) -> None:
        output = """.FF.............................
======================================================================
FAIL: test_branch_name_is_scoped_by_issue_and_slugged (src.tandem_agents.core.repository.repository_test.IssueIsolationBranchTest.test_branch_name_is_scoped_by_issue_and_slugged)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/tandem-agents/runs/run-1/worktrees/worker/src/tandem_agents/core/repository/repository_test.py", line 12, in test_branch_name_is_scoped_by_issue_and_slugged
    self.assertEqual(
AssertionError: 'aca/laca-12-add-per-issue-worktrees/task' != 'aca/laca-12-add-per-issue-worktrees'
- aca/laca-12-add-per-issue-worktrees/task
?                                    -----
+ aca/laca-12-add-per-issue-worktrees

FAIL: test_branch_uses_issue_fallback_for_empty_issue_key (src.tandem_agents.core.repository.repository_test.IssueIsolationBranchTest.test_branch_uses_issue_fallback_for_empty_issue_key)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/tandem-agents/runs/run-1/worktrees/worker/src/tandem_agents/core/repository/repository_test.py", line 24, in test_branch_uses_issue_fallback_for_empty_issue_key
    self.assertEqual(issue_isolation_branch(""), "aca/issue")
AssertionError: 'aca/task/task' != 'aca/issue'
- aca/task/task
+ aca/issue
"""

        context = _compact_focused_test_failure_output(output)

        self.assertIn("FAIL: test_branch_name_is_scoped_by_issue_and_slugged", context)
        self.assertIn("AssertionError: 'aca/laca-12-add-per-issue-worktrees/task'", context)
        self.assertIn("AssertionError: 'aca/task/task' != 'aca/issue'", context)
        self.assertNotIn("Traceback", context)
        self.assertNotIn("/workspace/tandem-agents/runs", context)
        self.assertLess(len(context), 700)

    def test_unterminated_verifiable_partial_diff_is_carried_forward_for_terminal_repair(self) -> None:
        parent_targets = [
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
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
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
                            "changed_files": parent_targets,
                            "worker_output_excerpt": (
                                "Worker produced a source plus required-test partial diff, "
                                "but did not return a terminal result after 244s. "
                                "Focused changed-file tests passed."
                            ),
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.core.repository.repository_test",
                            ],
                            "verification_returncode": 0,
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
                "acceptance_criteria": ["Finish repository isolation behavior."],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertEqual(subtasks[0]["files"], parent_targets)
        self.assertEqual(subtasks[0]["target_files"], parent_targets)
        self.assertTrue(subtasks[0]["repair_verification_first"])
        self.assertTrue(subtasks[0]["write_required"])
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("python3 -m unittest src.tandem_agents.core.repository.repository_test", criteria)
        self.assertIn("terminal", criteria.lower())

    def test_focused_verifiable_repair_reads_command_and_failure_from_patch_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = Path(tmp) / "worker.patch"
            patch_path.write_text(
                "# Partial worker diff\n\n"
                "## focused verification\n\n"
                "- command: python3 -m unittest src.tandem_agents.core.repository.repository_test\n"
                "- returncode: 1\n\n"
                "## test output\n\n"
                "E\n"
                "ImportError: Failed to import test module: repository_test\n"
                "ImportError: cannot import name 'claim_issue_worktree' from 'src.tandem_agents.core.repository.repository'\n\n"
                "## git diff --binary\n\n"
                "diff --git a/src/tandem_agents/core/repository/repository.py b/src/tandem_agents/core/repository/repository.py\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                task={"target_files": []},
                blackboard={
                    "repair": {
                        "partial_diff_artifacts": [
                            {
                                "subtask_id": "subtask-1",
                                "worker_id": "worker-1",
                                "patch_path": str(patch_path),
                                "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                                "changed_files": [
                                    "src/tandem_agents/core/repository/repository.py",
                                    "src/tandem_agents/core/repository/repository_test.py",
                                ],
                                "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
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
                    "acceptance_criteria": ["Finish repository isolation helper coverage."],
                }
            ]

            _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(
            subtasks[0]["verification_commands"],
            ["python3 -m unittest src.tandem_agents.core.repository.repository_test"],
        )
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("claim_issue_worktree", criteria)
        self.assertIn("Focused import repair", criteria)
        self.assertIn("exported production symbol", criteria)
        self.assertIn("python3 -m unittest src.tandem_agents.core.repository.repository_test", criteria)
        self.assertTrue(subtasks[0]["write_required"])

    def test_focused_verifiable_repair_calls_out_missing_pytest_dependency(self) -> None:
        ctx = SimpleNamespace(
            task={"target_files": []},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.core.repository.repository_test",
                            ],
                            "verification_output_excerpt": (
                                "ImportError: Failed to import test module: repository_test\n"
                                "ModuleNotFoundError: No module named 'pytest'"
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
                "acceptance_criteria": ["Finish repository isolation helper coverage."],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/worker.patch")
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Focused missing dependency repair", criteria)
        self.assertIn("remove the new `pytest` dependency", criteria)
        self.assertIn("unittest", criteria)
        self.assertIn("Do not add `pytest` as a project dependency", criteria)

    def test_repeated_missing_import_failure_discards_carried_source_test_patch(self) -> None:
        parent_targets = [
            "src/tandem_agents/core/repository/repository.py",
            "src/tandem_agents/core/repository/repository_test.py",
        ]
        missing_import_output = (
            "Worker produced a source plus required-test partial diff, but focused tests failed.\n"
            "ImportError: cannot import name 'create_isolated_run_worktree' from "
            "'src.tandem_agents.core.repository.repository'"
        )
        ctx = SimpleNamespace(
            task={"target_files": parent_targets},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/old.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": parent_targets,
                            "worker_output_excerpt": missing_import_output,
                            "verification_output_excerpt": missing_import_output,
                            "subtask_target_files": parent_targets,
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/new.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": parent_targets,
                            "worker_output_excerpt": missing_import_output,
                            "verification_output_excerpt": missing_import_output,
                            "subtask_target_files": parent_targets,
                        },
                    ],
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/core/repository/repository.py"],
                "target_files": ["src/tandem_agents/core/repository/repository.py"],
                "acceptance_criteria": ["Repair repository isolation."],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertNotIn("carry_forward_patch", subtasks[0])
        self.assertEqual(subtasks[0]["discarded_partial_diff_patch"], "/runs/run-1/artifacts/new.patch")
        self.assertEqual(subtasks[0]["repair_parent_target_files"], parent_targets)
        criteria = "\n".join(subtasks[0]["acceptance_criteria"])
        self.assertIn("Failure summary", criteria)
        self.assertIn("Repeated missing import failure", criteria)
        self.assertIn("Use the rejected diff only as failure evidence", criteria)
        self.assertIn("Replace the rejected partial worker diff", subtasks[0]["goal"])

    def test_repeated_partial_diff_repair_carries_forward_latest_patch(self) -> None:
        ctx = SimpleNamespace(
            task={"target_files": []},
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/old.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Old focused failure.",
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/new.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "New focused failure with narrower blocker.",
                        },
                    ],
                }
            },
        )
        subtasks = [
            {
                "id": "subtask-1",
                "files": ["src/tandem_agents/core/repository/repository.py"],
                "target_files": ["src/tandem_agents/core/repository/repository.py"],
                "acceptance_criteria": ["Repair repository isolation."],
            }
        ]

        _carry_forward_partial_diff_artifacts(ctx, subtasks)

        self.assertEqual(subtasks[0]["carry_forward_patch"], "/runs/run-1/artifacts/new.patch")
        self.assertIn("New focused failure", "\n".join(subtasks[0]["acceptance_criteria"]))
        self.assertNotIn("Old focused failure", "\n".join(subtasks[0]["acceptance_criteria"]))

    def test_deterministic_repair_prefers_latest_failed_source_test_patch(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-only.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: "
                                "after 244s it had changed only non-test files while required test files were "
                                "src/tandem_agents/core/repository/repository_test.py."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced a source plus required-test partial diff, "
                                "but focused tests failed after 242s."
                            ),
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.core.repository.repository_test",
                            ],
                            "verification_output_excerpt": (
                                "FAIL: test_task_run_branch_name_includes_issue_key_when_available\n"
                                "AssertionError: branch used ignored-title instead of LACA-12"
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Repair failed source+test partial diff")
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/source-and-test.patch")
        self.assertTrue(subtask["repair_verification_first"])
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(
            subtask["verification_commands"],
            ["python3 -m unittest src.tandem_agents.core.repository.repository_test"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("ignored-title instead of LACA-12", criteria)
        self.assertIn("verification fails, fix only the failing behavior", criteria)
        self.assertNotIn("changed only non-test files", criteria)

    def test_focused_failure_context_keeps_syntax_error_location(self) -> None:
        output = (
            "Traceback (most recent call last):\n"
            "  File \"/workspace/repo/src/tandem_agents/core/repository/repository_test.py\", line 103\n"
            "    return _resolve_config(root, env=dict(os.environ))\n"
            "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
            "SyntaxError: 'return' outside function\n"
        )

        compact = _compact_focused_test_failure_output(output)

        self.assertIn("repository_test.py\", line 103", compact)
        self.assertIn("return _resolve_config", compact)
        self.assertIn("SyntaxError: 'return' outside function", compact)

    def test_focused_failure_context_keeps_root_cause_with_long_worktree_path(self) -> None:
        output = (
            "E.............................\n"
            "======================================================================\n"
            "ERROR: test_create_issue_run_worktree_uses_distinct_branch_and_path_per_parallel_run "
            "(src.tandem_agents.core.repository.repository_test.IssueRunWorktreeTest."
            "test_create_issue_run_worktree_uses_distinct_branch_and_path_per_parallel_run)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"/workspace/tandem-agents/runs/run-20260618T185404Z-eb7d0b7a/"
            "worktrees/worker-1--subtask-1--exec-1-1781809056321417745/"
            "src/tandem_agents/core/repository/repository_test.py\", line 911, in "
            "test_create_issue_run_worktree_uses_distinct_branch_and_path_per_parallel_run\n"
            "    run_command([\"git\", \"init\", str(repo)], check=True)\n"
            "TypeError: run_command() got an unexpected keyword argument 'check'\n"
        )
        commands, focused_context = _partial_diff_focused_verification_context(
            {
                "verification_command": [
                    "python3",
                    "-m",
                    "unittest",
                    "src.tandem_agents.core.repository.repository_test",
                ],
                "verification_output_excerpt": output,
            }
        )

        repair_context = _compact_partial_diff_repair_context(focused_context)

        self.assertEqual(commands, ["python3 -m unittest src.tandem_agents.core.repository.repository_test"])
        self.assertIn("TypeError: run_command() got an unexpected keyword argument 'check'", focused_context)
        self.assertIn("TypeError: run_command() got an unexpected keyword argument 'check'", repair_context)

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
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("discarded_partial_diff_patch", subtask)
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Read and edit the required test file", criteria)
        self.assertIn("preserved source patch", criteria)
        self.assertNotIn("/runs/run-1/artifacts/worker-1.patch", criteria)
        self.assertEqual(
            subtask["repair_parent_target_files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
                "src/tandem_agents/core/phases/task_intake.py",
            ],
        )

    def test_testless_repair_after_no_diff_stall_gets_test_first_micro_repair(self) -> None:
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
                    "stalled_no_diff_repair_attempts": 1,
                    "last_repair_stall_kind": "engine_tool_loop_stalled_no_diff",
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: "
                                "after 153s it had changed only non-test files while required test files were "
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
        self.assertTrue(subtask["repair_stalled_no_diff_retry"])
        self.assertEqual(subtask["repair_focus_instruction"], subtask["repair_focus_instructions"][0])
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("stalled without producing any diff", criteria)
        self.assertIn("make a concrete first edit in the required test file", criteria)
        self.assertIn("repository_test.py", criteria)
        self.assertIn("repository.py", criteria)
        self.assertIn("stalled without producing any diff", subtask["scope_note"])

    def test_deterministic_repair_ignores_completed_stale_partial_artifacts(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/scheduling/scheduler.py",
                    "src/tandem_agents/core/scheduling/scheduler_test.py",
                    "src/tandem_agents/core/phases/worker_dispatch.py",
                    "src/tandem_agents/core/phases/worker_dispatch_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "completed_subtask_ids": [
                        "fallback-throughput-scheduler-controls-part-1",
                        "fallback-throughput-scheduler-controls-part-2",
                    ],
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "fallback-throughput-scheduler-controls-part-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/stale-test.patch",
                            "changed_files": ["src/tandem_agents/core/scheduling/scheduler_test.py"],
                            "worker_output_excerpt": "Worker changed only required test files for a regression subtask.",
                            "subtask_target_files": [
                                "src/tandem_agents/core/scheduling/scheduler.py",
                                "src/tandem_agents/core/scheduling/scheduler_test.py",
                            ],
                        },
                        {
                            "subtask_id": "fallback-throughput-scheduler-controls-part-2",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/stale-source.patch",
                            "changed_files": [
                                "src/tandem_agents/core/scheduling/scheduler.py",
                                "src/tandem_agents/core/scheduling/scheduler_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test diff.",
                            "subtask_target_files": [
                                "src/tandem_agents/core/scheduling/scheduler.py",
                                "src/tandem_agents/core/scheduling/scheduler_test.py",
                            ],
                        },
                        {
                            "subtask_id": "fallback-throughput-worker-metrics",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/current-timeout.patch",
                            "changed_files": ["src/tandem_agents/core/phases/worker_dispatch.py"],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt did not finish within 300s."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/phases/worker_dispatch.py",
                                "src/tandem_agents/core/phases/worker_dispatch_test.py",
                            ],
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["id"], "fallback-throughput-worker-metrics")
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/current-timeout.patch")
        self.assertNotIn("carry_forward_patch", subtask)
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/phases/worker_dispatch.py",
                "src/tandem_agents/core/phases/worker_dispatch_test.py",
            ],
        )
        self.assertIn(
            "src/tandem_agents/core/phases/worker_dispatch_test.py",
            "\n".join(subtask["acceptance_criteria"]),
        )
        self.assertIn(
            "Do not copy or replay the timed-out source-only partial patch",
            "\n".join(subtask["acceptance_criteria"]),
        )

    def test_non_reusable_testless_partial_diff_gets_clean_deterministic_repair_plan(self) -> None:
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
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: "
                                "after 245s it had changed only non-test files while required test files were "
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
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("carry_forward_patch", subtask)
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Do not copy or replay the rejected source-only partial patch", criteria)
        self.assertIn("Read and edit the required test file", criteria)
        self.assertIn("minimal semantic production change", criteria)
        self.assertIn("rebuild the repair from the clean target files", subtask["scope_note"])

    def test_unproductive_source_partial_gets_deterministic_repair_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/scheduling/scheduler.py",
                    "src/tandem_agents/core/scheduling/scheduler_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "fallback-throughput-scheduler-controls-part-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/comment-only.patch",
                            "changed_files": ["src/tandem_agents/core/scheduling/scheduler.py"],
                            "worker_output_excerpt": (
                                "Worker produced an unproductive partial diff: worker diff is comment-only "
                                "after the comment-only guard budget."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/scheduling/scheduler.py",
                                "src/tandem_agents/core/scheduling/scheduler_test.py",
                            ],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["id"], "fallback-throughput-scheduler-controls-part-2")
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/comment-only.patch")
        self.assertNotIn("carry_forward_patch", subtask)
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/scheduling/scheduler.py",
                "src/tandem_agents/core/scheduling/scheduler_test.py",
            ],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Do not apply or copy the rejected comment-only partial patch", criteria)
        self.assertIn("non-comment production behavior change", criteria)

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

    def test_latest_test_only_partial_diff_supersedes_older_unproductive_artifact(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/comment-only.patch",
                            "failure_reason": "WORKER_UNPRODUCTIVE_DIFF",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker produced an unproductive partial diff: worker diff is comment-only "
                                "after the comment-only guard budget."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test-only.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": (
                                "Worker changed only required test files for a regression subtask: after 245s "
                                "it had not made the required production change."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Repair test-only partial diff")
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/test-only.patch")
        self.assertEqual(
            subtask["repair_requires_production_followup"],
            ["src/tandem_agents/core/repository/repository.py"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Make the first new repair edit in the required production file", criteria)
        self.assertNotIn("comment-only partial patch", criteria)

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

    def test_guard_rejected_partials_do_not_get_complementary_carry_forward_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/config/config_types.py",
                    "src/tandem_agents/config/config_loader.py",
                    "src/tandem_agents/config/config_loader_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test.patch",
                            "patch_reusable": False,
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
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/config/config_types.py"],
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
        self.assertNotEqual(subtask["title"], "Verify complementary source and test partial diffs")
        self.assertNotIn("carry_forward_patches", subtask)
        self.assertIn("Do not copy or replay the rejected partial patch", "\n".join(subtask["acceptance_criteria"]))

    def test_syntax_invalid_source_test_partial_diff_carries_patch_before_complementary_rebuild(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": "Worker changed only non-test files.",
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": "Worker changed only required test files.",
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/paired.patch",
                            "failure_reason": "WORKER_SYNTAX_INVALID_DIFF",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "syntax_errors": [
                                "src/tandem_agents/core/repository/repository_test.py:1:17: invalid syntax"
                            ],
                            "worker_output_excerpt": (
                                "Worker produced a source plus required-test partial diff, but changed Python "
                                "files did not parse: src/tandem_agents/core/repository/repository_test.py:1:17: invalid syntax"
                            ),
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "syntax_invalid_source_test_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Repair syntax-invalid source+test partial diff")
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/paired.patch")
        self.assertNotIn("carry_forward_patches", subtask)
        self.assertEqual(
            subtask["repair_syntax_errors"],
            ["src/tandem_agents/core/repository/repository_test.py:1:17: invalid syntax"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("syntax", criteria.lower())
        self.assertIn("python3 -m py_compile", "\n".join(subtask.get("verification_commands") or []))
        self.assertNotEqual(subtask.get("repair_mode"), "complementary_guarded_partial_diff")

    def test_one_sided_guarded_source_and_test_partials_get_composed_verify_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/core/phases/finalize.py",
                    "src/tandem_agents/core/phases/pr_body.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: after 245s "
                                "it had changed only non-test files while required test files were "
                                "src/tandem_agents/core/repository/repository_test.py."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": (
                                "Worker changed only required test files for a regression subtask: after 248s "
                                "it had not made the required production change."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository_test.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(plan["kind"], "complementary_guarded_partial_diff")
        self.assertEqual(subtask["title"], "Verify complementary source and test partial diffs")
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(
            subtask["carry_forward_patches"],
            ["/runs/run-1/artifacts/source.patch", "/runs/run-1/artifacts/test.patch"],
        )
        self.assertFalse(subtask["write_required"])
        self.assertEqual(subtask["repair_mode"], "complementary_guarded_partial_diff")
        self.assertTrue(subtask["repair_requires_paired_source_test_diff"])
        self.assertTrue(subtask["repair_verification_first"])
        self.assertIn("mechanically composed", "\n".join(subtask["repair_focus_instructions"]))
        self.assertEqual(
            subtask["repair_requires_production_followup"],
            ["src/tandem_agents/core/repository/repository.py"],
        )
        self.assertEqual(
            subtask["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("ACA applied the preserved production patch and test patch", criteria)
        self.assertIn("Run the narrowest deterministic verification", criteria)
        self.assertNotIn("Make one substantive production edit", criteria)
        self.assertNotIn("task_intake.py", criteria)

        _carry_forward_partial_diff_artifacts(ctx, plan["subtasks"])

        subtask = plan["subtasks"][0]
        self.assertNotIn("carry_forward_patch", subtask)
        self.assertEqual(
            subtask["carry_forward_patches"],
            ["/runs/run-1/artifacts/source.patch", "/runs/run-1/artifacts/test.patch"],
        )
        self.assertIn(
            "ACA applied the preserved production patch and test patch",
            "\n".join(subtask["acceptance_criteria"]),
        )

    def test_destructive_complementary_rebuild_plan_adds_deletion_averse_constraints(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/destructive.patch",
                            "failure_reason": "WORKER_DESTRUCTIVE_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "complementary_rejected_partial_diff")
        subtask = plan["subtasks"][0]
        criteria = "\n".join(subtask["acceptance_criteria"])
        focus = "\n".join(subtask["repair_focus_instructions"])
        self.assertIn("destructive rewrite guard", criteria)
        self.assertIn("keep deletions near zero", criteria)
        self.assertIn("small, additive, and deletion-averse", focus)
        self.assertIn("near-zero deletion", subtask["scope_note"])
        self.assertEqual(
            subtask["discarded_destructive_partial_diff_patches"],
            ["/runs/run-1/artifacts/destructive.patch"],
        )

    def test_structured_one_sided_failure_reasons_get_complementary_guarded_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": "worker stopped after changing repository implementation",
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "patch_reusable": False,
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": "worker stopped after changing repository coverage",
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "complementary_guarded_partial_diff")
        subtask = plan["subtasks"][0]
        self.assertTrue(subtask["repair_requires_paired_source_test_diff"])
        self.assertTrue(subtask["repair_verification_first"])
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(
            subtask["carry_forward_patches"],
            ["/runs/run-1/artifacts/source.patch", "/runs/run-1/artifacts/test.patch"],
        )

    def test_weak_source_test_partial_diff_gets_narrow_deterministic_repair_plan(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                    "src/tandem_agents/core/phases/finalize.py",
                    "src/tandem_agents/core/phases/pr_body.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                            "patch_reusable": False,
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced source plus required-test file changes, but the test diff did "
                                "not add a test method or assertion after 249s."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "weak_source_test_diff")
        self.assertEqual(_deterministic_repair_plan_kind(plan), "weak_source_test_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Repair weak source+test partial diff")
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtask["target_files"], subtask["files"])
        self.assertEqual(subtask["repair_parent_target_files"], subtask["files"])
        self.assertEqual(
            subtask["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("carry_forward_patch", subtask)
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("real test method or assertion", criteria)
        self.assertIn("Do not copy or replay the rejected weak source+test partial patch", criteria)
        self.assertIn("minimal semantic production change", criteria)
        self.assertNotIn("task_intake.py", criteria)
        self.assertNotIn("finalize.py", "\n".join(subtask["files"]))

    def test_misaligned_source_test_partial_diff_repair_warns_about_untested_public_api(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_MISALIGNED_TEST",
                            "patch_reusable": False,
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced source plus required-test file changes, but the required test "
                                "additions did not exercise newly introduced production symbol(s): "
                                "IsolatedRunCheckout, isolated_run_branch_name"
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "weak_source_test_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-1.patch")
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Do not add or keep newly introduced public production helpers", criteria)
        self.assertIn("imports or calls those exact helpers", criteria)
        self.assertIn("existing production API under test", criteria)
        self.assertIn("newly introduced production symbols", subtask["scope_note"])
        self.assertIn(
            "required tests did not exercise newly introduced production API",
            subtask["repair_failure_summary"],
        )

    def test_weak_source_test_partial_diff_keeps_focused_typeerror_repair(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced source plus required-test file changes, but ACA rejected the "
                                "diff before syncing it: ERROR: test_task_run_branch_name_includes_issue_key\n"
                                "TypeError: task_run_branch_name() got an unexpected keyword argument 'issue_key'"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "weak_source_test_diff")
        subtask = plan["subtasks"][0]
        criteria = "\n".join(subtask["acceptance_criteria"])
        focus = "\n".join(subtask["repair_focus_instructions"])
        self.assertIn("First repair the exact focused verification failure", criteria)
        self.assertIn("issue_key", criteria)
        self.assertIn("Focused TypeError repair", focus)
        self.assertIn("Prefer changing the newly added test/caller", focus)
        self.assertIn("repair_failure_focus", subtask)

    def test_reusable_weak_source_test_partial_diff_is_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced source plus required-test file changes, but the test diff did "
                                "not add a test method or assertion after 249s."
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/worker-1.patch")
        self.assertNotIn("discarded_partial_diff_patch", subtask)
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("preserved weak source+test patch is applied", criteria)
        self.assertIn("Make the first new repair edit in the required test file", criteria)
        self.assertIn("real test method or assertion", criteria)

    def test_weak_source_test_repair_calls_out_zero_division_exception_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = Path(tmp) / "worker-1.patch"
            patch_path.write_text(
                "\n".join(
                    [
                        "diff --git a/src/tandem_agents/aca_harness/calculator_test.py b/src/tandem_agents/aca_harness/calculator_test.py",
                        "--- a/src/tandem_agents/aca_harness/calculator_test.py",
                        "+++ b/src/tandem_agents/aca_harness/calculator_test.py",
                        "@@ -1,3 +1,6 @@",
                        "+    def test_divide_rejects_zero_divisor(self):",
                        "+        with self.assertRaises(ValueError):",
                        "+            divide(8, 0)",
                    ]
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                task={
                    "target_files": [
                        "src/tandem_agents/aca_harness/calculator.py",
                        "src/tandem_agents/aca_harness/calculator_test.py",
                    ]
                },
                blackboard={
                    "repair": {
                        "partial_diff_artifacts": [
                            {
                                "subtask_id": "subtask-1",
                                "worker_id": "worker-1",
                                "patch_path": str(patch_path),
                                "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                                "changed_files": [
                                    "src/tandem_agents/aca_harness/calculator.py",
                                    "src/tandem_agents/aca_harness/calculator_test.py",
                                ],
                                "worker_output_excerpt": (
                                    "ERROR: test_divide_rejects_zero_divisor\n"
                                    "ZeroDivisionError: cannot divide by zero"
                                ),
                            }
                        ],
                    }
                },
            )

            plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Focused zero-division repair", criteria)
        self.assertIn("assert `ZeroDivisionError`", criteria)
        self.assertIn("do not change production to raise `ValueError`", criteria)
        self.assertEqual(subtask["repair_focus_instruction"].split(":", 1)[0], "Focused zero-division repair")

    def test_latest_weak_source_test_artifact_takes_precedence_over_complementary_pair(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-only.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker drifted off the required regression/test coverage path: changed only non-test files "
                                "while required test files were src/tandem_agents/core/repository/repository_test.py."
                            ),
                            "patch_reusable": False,
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test-only.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": "Worker changed only required test files for a regression subtask.",
                            "patch_reusable": False,
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/weak-source-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced source plus required-test file changes, but the test diff did "
                                "not add a test method or assertion after 182s."
                            ),
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/destructive.patch",
                            "failure_reason": "WORKER_DESTRUCTIVE_DIFF",
                            "blocker_kind": "worker_runaway_diff",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": "Worker diff tripped ACA destructive rewrite guard.",
                            "patch_reusable": False,
                        },
                    ]
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "weak_source_test_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/weak-source-test.patch")
        self.assertTrue(subtask["repair_requires_paired_source_test_diff"])
        self.assertTrue(subtask["repair_precision_edit"])
        self.assertEqual(subtask["repair_diff_line_budget"], 80)
        self.assertEqual(
            subtask["repair_requires_production_followup"],
            ["src/tandem_agents/core/repository/repository.py"],
        )
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("real test method or assertion", criteria)
        self.assertIn("source-only and test-only diffs", criteria)
        self.assertIn("80 changed diff lines", criteria)

    def test_source_timeout_partial_without_test_target_is_carried_forward(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/worker-2.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "changed_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                            "worker_output_excerpt": (
                                "engine_prompt_timeout: worker timed out before a terminal response"
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "source_engine_timeout_partial_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Repair source-only engine timeout partial diff")
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/worker-2.patch")
        self.assertNotIn("discarded_partial_diff_patch", subtask)
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
            ],
        )
        self.assertEqual(subtask["repair_requires_test_followup"], [])
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("No required test target was declared", criteria)
        self.assertIn("Do not discard or restart the carried source patch", criteria)

    def test_source_timeout_partial_with_terminalizer_blockers_is_discarded_without_test_target(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/worker-2.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "changed_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                            ],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response.\n"
                                "Verification: verification not run.\n"
                                "Remaining implementation blockers: helper functions were added but the diff was not wired into the production path."
                            ),
                            "subtask_target_files": ["src/tandem_agents/core/phases/task_intake.py"],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "source_engine_timeout_partial_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-2.patch")
        self.assertNotIn("carry_forward_patch", subtask)
        self.assertEqual(subtask["repair_source_failure_reason"], "ENGINE_PROMPT_TIMEOUT")
        self.assertIn("rebuild the smallest production repair", "\n".join(subtask["acceptance_criteria"]))
        self.assertIn("not wired into the production path", subtask["repair_failure_summary"])

    def test_completed_one_sided_artifacts_do_not_mislabel_latest_engine_timeout_repair(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "completed_subtask_ids": ["subtask-1"],
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-only.patch",
                            "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                            "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                            "worker_output_excerpt": (
                                "Worker changed only non-test files while required test files were "
                                "src/tandem_agents/core/repository/repository_test.py."
                            ),
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/test-only.patch",
                            "failure_reason": "WORKER_TEST_ONLY_DIFF",
                            "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                            "worker_output_excerpt": "Worker changed only required test files.",
                        },
                        {
                            "subtask_id": "subtask-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/task-intake-timeout.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "changed_files": ["src/tandem_agents/core/phases/task_intake.py"],
                            "worker_output_excerpt": (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response. "
                                "Remaining implementation blockers: _allocate_claim_worktree is not invoked."
                            ),
                            "subtask_target_files": ["src/tandem_agents/core/phases/task_intake.py"],
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "source_engine_timeout_partial_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["id"], "subtask-2")
        self.assertEqual(subtask["carry_forward_patch"], "/runs/run-1/artifacts/task-intake-timeout.patch")
        self.assertEqual(subtask["repair_source_failure_reason"], "ENGINE_PROMPT_TIMEOUT")
        self.assertEqual(subtask["repair_requires_test_followup"], [])

    def test_source_timeout_partial_with_parent_paired_test_target_is_discarded(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/phases/task_intake.py",
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-2",
                            "worker_id": "worker-2",
                            "patch_path": "/runs/run-1/artifacts/worker-2.patch",
                            "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                            "changed_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                            "worker_output_excerpt": (
                                "engine_prompt_timeout: worker timed out before a terminal response"
                            ),
                            "subtask_target_files": [
                                "src/tandem_agents/core/phases/task_intake.py",
                                "src/tandem_agents/core/repository/repository.py",
                            ],
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "source_engine_timeout_partial_diff")
        subtask = plan["subtasks"][0]
        self.assertEqual(subtask["title"], "Repair source-only engine timeout partial diff")
        self.assertEqual(subtask["discarded_partial_diff_patch"], "/runs/run-1/artifacts/worker-2.patch")
        self.assertNotIn("carry_forward_patch", subtask)
        self.assertEqual(
            subtask["files"],
            [
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
        )
        self.assertEqual(subtask["target_files"], subtask["files"])
        self.assertEqual(
            subtask["repair_requires_test_followup"],
            ["src/tandem_agents/core/repository/repository_test.py"],
        )
        self.assertEqual(subtask["repair_parent_target_files"], subtask["files"])
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Do not copy or replay the timed-out source-only partial patch", criteria)
        self.assertIn("Read and edit the required test file(s) first", criteria)
        self.assertIn("repository_test.py", criteria)

    def test_focused_verifiable_repair_calls_out_unexpected_keyword_typeerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": (
                                "Worker produced a source plus required-test partial diff, "
                                "but focused tests failed after 241s."
                            ),
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.core.repository.repository_test",
                            ],
                            "verification_output_excerpt": (
                                "TypeError: task_run_branch_name() got an unexpected keyword argument 'issue_key'\n"
                                "TypeError: worker_worktree_name() got an unexpected keyword argument 'issue_key'"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused TypeError repair", criteria)
        self.assertIn("task_run_branch_name", criteria)
        self.assertIn("worker_worktree_name", criteria)
        self.assertIn("issue_key", criteria)
        self.assertIn("Do not add unused keyword parameters", criteria)
        self.assertIn("Do not spend the repair on imports", criteria)

    def test_focused_verifiable_repair_calls_out_zero_division_exception_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = Path(tmp) / "source-and-test.patch"
            patch_path.write_text(
                "\n".join(
                    [
                        "diff --git a/src/tandem_agents/aca_harness/calculator_test.py b/src/tandem_agents/aca_harness/calculator_test.py",
                        "--- a/src/tandem_agents/aca_harness/calculator_test.py",
                        "+++ b/src/tandem_agents/aca_harness/calculator_test.py",
                        "@@ -1,3 +1,6 @@",
                        "+    def test_describe_operation_rejects_divide_by_zero(self):",
                        "+        with self.assertRaises(ValueError):",
                        '+            describe_operation("divide", 8, 0)',
                    ]
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                task={
                    "target_files": [
                        "src/tandem_agents/aca_harness/calculator.py",
                        "src/tandem_agents/aca_harness/calculator_test.py",
                    ]
                },
                blackboard={
                    "repair": {
                        "partial_diff_artifacts": [
                            {
                                "subtask_id": "subtask-1",
                                "worker_id": "worker-1",
                                "patch_path": str(patch_path),
                                "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                                "changed_files": [
                                    "src/tandem_agents/aca_harness/calculator.py",
                                    "src/tandem_agents/aca_harness/calculator_test.py",
                                ],
                                "worker_output_excerpt": (
                                    "Worker produced a source plus required-test partial diff."
                                ),
                                "verification_output_excerpt": (
                                    "ERROR: test_describe_operation_rejects_divide_by_zero\n"
                                    "ZeroDivisionError: cannot divide by zero"
                                ),
                            }
                        ],
                    }
                },
            )

            plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(_deterministic_repair_plan_kind(plan), "failed_verifiable_source_test_diff")
        subtask = plan["subtasks"][0]
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertIn("Focused zero-division repair", criteria)
        self.assertIn("assert `ZeroDivisionError`", criteria)
        self.assertEqual(subtask["repair_focus_instruction"].split(":", 1)[0], "Focused zero-division repair")

    def test_focused_verifiable_repair_keeps_nameerror_and_typeerror_focuses(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.core.repository.repository_test",
                            ],
                            "verification_output_excerpt": (
                                "NameError: name '_slug' is not defined\n"
                                "TypeError: task_run_branch_name() got an unexpected keyword argument 'issue_id'\n"
                                "TypeError: worker_worktree_name() got an unexpected keyword argument 'issue_id'"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        self.assertEqual(_deterministic_repair_plan_kind(plan), "failed_verifiable_source_test_diff")
        subtask = plan["subtasks"][0]
        criteria = "\n".join(subtask["acceptance_criteria"])
        self.assertEqual(plan["kind"], "failed_verifiable_source_test_diff")
        self.assertIn("Focused NameError repair", criteria)
        self.assertIn("_slug", criteria)
        self.assertIn("Focused TypeError repair", criteria)
        self.assertIn("issue_id", criteria)
        self.assertEqual(len(subtask["repair_focus_instructions"]), 2)
        self.assertIn("Focused NameError repair", subtask["repair_focus_instruction"])

    def test_focused_verifiable_repair_calls_out_test_module_alias_nameerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_command": [
                                "python3",
                                "-m",
                                "unittest",
                                "src.tandem_agents.core.repository.repository_test",
                            ],
                            "verification_output_excerpt": (
                                "ERROR: test_issue_worktree_path_includes_run_and_issue_slug\n"
                                "NameError: name 'repository' is not defined"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        subtask = plan["subtasks"][0]
        criteria = "\n".join(subtask["acceptance_criteria"])
        focus = "\n".join(subtask["repair_focus_instructions"])
        self.assertIn("First repair the exact focused verification failure", criteria)
        self.assertIn("NameError: name 'repository' is not defined", criteria)
        self.assertIn("module alias used only by the new test", focus)
        self.assertIn("fix the test import or call the already-imported function", focus)

    def test_focused_verifiable_repair_calls_out_positional_argument_typeerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_output_excerpt": (
                                "TypeError: worker_worktree_name() takes from 1 to 2 positional arguments but 5 were given"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused TypeError repair", plan["subtasks"][0]["repair_focus_instruction"])
        self.assertIn("Focused TypeError repair", criteria)
        self.assertIn("worker_worktree_name", criteria)
        self.assertIn("positional arguments", criteria)
        self.assertIn("Inspect the current function definition", criteria)
        self.assertIn("existing call sites", criteria)
        self.assertIn("Prefer changing the newly added test/caller", criteria)
        self.assertIn("Do not merely add unused optional parameters", criteria)
        self.assertIn("Scan the entire failing test method", criteria)

    def test_focused_verifiable_repair_calls_out_missing_required_argument_typeerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_output_excerpt": (
                                "TypeError: isolated_run_branch_name() missing 1 required positional argument: 'run_id'"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused TypeError repair", criteria)
        self.assertIn("isolated_run_branch_name", criteria)
        self.assertIn("required positional argument", criteria)
        self.assertIn("run_id", criteria)
        self.assertIn("Inspect the current production signature", criteria)
        self.assertIn("pass the real required values", criteria)
        self.assertIn("Do not hide the error", criteria)

    def test_focused_verifiable_repair_calls_out_string_dict_attributeerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_output_excerpt": (
                                "AttributeError: 'str' object has no attribute 'get'"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused AttributeError repair", criteria)
        self.assertIn("expects a task dict", criteria)
        self.assertIn("Inspect the current production signature", criteria)
        self.assertIn("pass the task dict shape", criteria)
        self.assertIn("helper/API that actually accepts issue/run strings", criteria)
        self.assertIn("Scan the whole failing test method", criteria)

    def test_focused_verifiable_repair_calls_out_nonetype_dict_attributeerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_output_excerpt": (
                                "AttributeError: 'NoneType' object has no attribute 'get'"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused AttributeError repair", criteria)
        self.assertIn("string or `None`", criteria)
        self.assertIn("dict-only `.get(...)` code", criteria)

    def test_focused_verifiable_repair_calls_out_future_import_syntaxerror(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_output_excerpt": (
                                "SyntaxError: from __future__ imports must occur at the beginning of the file"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused SyntaxError repair", criteria)
        self.assertIn("from __future__", criteria)
        self.assertIn("below the existing future import", criteria)

    def test_focused_verifiable_repair_calls_out_assertion_failure(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/source-and-test.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "worker_output_excerpt": "Worker produced a source plus required-test partial diff.",
                            "verification_output_excerpt": (
                                "FAIL: test_task_run_branch_name_is_stable_and_issue_scoped\n"
                                "AssertionError: 'aca/generated-branch' != 'aca/LACA-12/run-1'\n"
                                "FAILED (failures=1)"
                            ),
                        }
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Focused assertion repair", criteria)
        self.assertIn("Scan the entire failing test method", criteria)
        self.assertIn("same naming/serialization contract", criteria)
        self.assertIn("original task contract", criteria)
        self.assertIn("existing production callers", criteria)
        self.assertIn("update the production code", criteria)
        self.assertIn("invented or unsupported public behavior", criteria)
        self.assertIn("slugify", criteria)
        self.assertIn("normalize case", criteria)
        self.assertIn("truncate values", criteria)
        self.assertIn("stable semantic properties", criteria)

    def test_repeated_assertion_failure_calls_out_whole_method_repair(self) -> None:
        ctx = SimpleNamespace(
            task={
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ]
            },
            blackboard={
                "repair": {
                    "partial_diff_artifacts": [
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/branch-failure.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "verification_output_excerpt": (
                                "FAIL: test_issue_run_names_include_issue_and_run_for_isolation "
                                "(src.tandem_agents.core.repository.repository_test.IssueRunIsolationNamingTest."
                                "test_issue_run_names_include_issue_and_run_for_isolation)\n"
                                "AssertionError: 'aca/laca-12/run-1' != 'aca/LACA-12/run-1'"
                            ),
                        },
                        {
                            "subtask_id": "subtask-1",
                            "worker_id": "worker-1",
                            "patch_path": "/runs/run-1/artifacts/worktree-failure.patch",
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "changed_files": [
                                "src/tandem_agents/core/repository/repository.py",
                                "src/tandem_agents/core/repository/repository_test.py",
                            ],
                            "verification_output_excerpt": (
                                "FAIL: test_issue_run_names_include_issue_and_run_for_isolation "
                                "(src.tandem_agents.core.repository.repository_test.IssueRunIsolationNamingTest."
                                "test_issue_run_names_include_issue_and_run_for_isolation)\n"
                                "AssertionError: 'issue-laca-12--run-run-1' != 'laca-12--run-1'"
                            ),
                        },
                    ],
                }
            },
        )

        plan = _deterministic_testless_partial_diff_repair_plan(ctx)

        self.assertIsNotNone(plan)
        criteria = "\n".join(plan["subtasks"][0]["acceptance_criteria"])
        self.assertIn("Repeated assertion failure after a focused repair retry", criteria)
        self.assertIn("test_issue_run_names_include_issue_and_run_for_isolation", criteria)
        self.assertIn("scan the entire failing test method", criteria)
        self.assertIn("update all related expectations consistently", criteria)

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
            "Keep repair edits scoped to the active repair target files",
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
        self.assertTrue(subtasks[0]["write_required"])
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

    def test_verification_first_prescreen_keeps_repair_write_capable(self) -> None:
        subtask = {"pre_satisfied": False, "repair_verification_first": True}

        self.assertTrue(_write_required_after_prescreen(subtask))

    def test_normal_unsatisfied_prescreen_requires_write(self) -> None:
        subtask = {"pre_satisfied": False}

        self.assertTrue(_write_required_after_prescreen(subtask))

    def test_source_target_subtask_is_write_capable_for_code_edit(self) -> None:
        self.assertTrue(
            _subtask_has_source_or_test_targets(
                {
                    "files": ["docs/README.md"],
                    "target_files": ["src/tandem_agents/core/phases/task_intake.py"],
                }
            )
        )
        self.assertFalse(
            _subtask_has_source_or_test_targets(
                {
                    "files": ["docs/README.md"],
                    "target_files": ["README.md"],
                }
            )
        )

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
        self.assertIn("Keep repair edits scoped to the active repair target files", criteria)
        self.assertNotIn("Do not expand the edit set beyond", criteria)
        self.assertIn("Active repair targets are:", subtasks[0]["scope_note"])
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

    def test_run_manager_prompt_accepts_late_valid_json_during_timeout_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}),
                repo_path=repo_path,
                task={"task_id": "TAN-170", "title": "Add worktree isolation"},
                repo={"path": str(repo_path)},
                layout={
                    "run_dir": run_dir,
                    "artifacts": run_dir / "artifacts",
                    "logs": run_dir / "logs",
                    "events": run_dir / "events.jsonl",
                    "blackboard": run_dir / "blackboard.yaml",
                    "status": run_dir / "status.json",
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
                artifact_path=str(run_dir / "artifacts" / "repo_context_bundle.json"),
                path_scope=".",
                required_files=[],
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="",
            )
            plan = {
                "summary": "late valid plan",
                "subtasks": [
                    {
                        "id": "subtask-1",
                        "title": "Implement isolation",
                        "goal": "Keep runs isolated.",
                        "files": ["src/tandem_agents/core/repository/repository.py"],
                        "target_files": ["src/tandem_agents/core/repository/repository.py"],
                        "acceptance_criteria": ["Create one worktree and branch per claimed issue."],
                    }
                ],
                "risks": [],
                "tests": [],
            }

            def late_stream(*_args, **_kwargs):
                time.sleep(0.04)
                return {"returncode": 0, "stdout": json.dumps(plan)}

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
                mock.patch("src.tandem_agents.core.phases.planning._manager_prompt_timeout_seconds", return_value=0.01),
                mock.patch("src.tandem_agents.core.phases.planning._manager_prompt_timeout_grace_seconds", return_value=0.2),
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt", side_effect=late_stream),
                mock.patch("src.tandem_agents.core.phases.planning._cancel_active_manager_engine_session") as cancel_session,
            ):
                result = run_manager_prompt(ctx)

            cancel_session.assert_not_called()
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(ctx.manager_plan["summary"], "late valid plan")
            self.assertNotIn("manager_invalid_plan", ctx.blackboard)

    def test_run_manager_prompt_cancels_only_after_timeout_grace_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}),
                repo_path=repo_path,
                task={"task_id": "TAN-170", "title": "Add worktree isolation"},
                repo={"path": str(repo_path)},
                layout={
                    "run_dir": run_dir,
                    "artifacts": run_dir / "artifacts",
                    "logs": run_dir / "logs",
                    "events": run_dir / "events.jsonl",
                    "blackboard": run_dir / "blackboard.yaml",
                    "status": run_dir / "status.json",
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
                artifact_path=str(run_dir / "artifacts" / "repo_context_bundle.json"),
                path_scope=".",
                required_files=[],
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="",
            )

            def stuck_stream(*_args, **_kwargs):
                time.sleep(0.2)
                return {"returncode": 0, "stdout": json.dumps({"summary": "too late", "subtasks": [], "risks": [], "tests": []})}

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
                mock.patch("src.tandem_agents.core.phases.planning._manager_prompt_timeout_seconds", return_value=0.01),
                mock.patch("src.tandem_agents.core.phases.planning._manager_prompt_timeout_grace_seconds", return_value=0.02),
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt", side_effect=stuck_stream),
                mock.patch("src.tandem_agents.core.phases.planning._cancel_active_manager_engine_session") as cancel_session,
            ):
                result = run_manager_prompt(ctx)

            cancel_session.assert_called_once_with(ctx, "manager_prompt_timeout")
            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "manager_invalid_plan")
            self.assertEqual(
                ctx.blackboard["manager_engine"]["engine"],
                {"stream_reason": "manager_prompt_timeout", "timeout_seconds": 0.01, "grace_seconds": 0.02},
            )

    def test_run_manager_prompt_blocks_engine_dispatch_failure_without_fallback(self) -> None:
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
                "ENGINE_ERROR: ENGINE_DISPATCH_FAILED: failed to reach provider `openai-codex` "
                "at https://chatgpt.com/backend-api/codex (request error)."
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
                    return_value={"returncode": 1, "stdout": bad_stdout},
                ),
            ):
                result = run_manager_prompt(ctx)

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "manager_engine_dispatch_failed")
            self.assertEqual(ctx.status["run"]["status"], "blocked")
            self.assertTrue(ctx.status["blocker"]["active"])
            self.assertEqual(ctx.status["blocker"]["kind"], "manager_engine_dispatch_failed")
            self.assertIn("will not launch fallback workers", ctx.status["phase"]["detail"])
            self.assertNotIn("manager_invalid_plan", ctx.blackboard)
            self.assertEqual(ctx.blackboard["manager_engine_failure"]["kind"], "manager_engine_dispatch_failed")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            failure_events = [event for event in events if event["type"] == "manager.engine_dispatch_failed"]
            self.assertEqual(failure_events[-1]["payload"]["recoverable"], False)

    def test_deterministic_repo_context_plan_uses_graph_first_by_default(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(env={}),
            blackboard={
                "repo_context": {
                    "required_files_applied_as_target_files": True,
                    "source": "repo.context_bundle",
                    "fallback_used": False,
                }
            },
            task={"execution_kind": "code_edit", "source": {"type": "linear"}},
        )

        self.assertTrue(_should_use_deterministic_repo_context_plan(ctx))

    def test_deterministic_repo_context_plan_can_disable_graph_first_until_manager_failure(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(env={"ACA_MANAGER_GRAPH_FIRST_REPO_CONTEXT_PLAN": "false"}),
            blackboard={
                "repo_context": {
                    "required_files_applied_as_target_files": True,
                    "source": "repo.context_bundle",
                    "fallback_used": False,
                }
            },
            task={"execution_kind": "code_edit", "source": {"type": "linear"}},
        )

        self.assertFalse(_should_use_deterministic_repo_context_plan(ctx))
        ctx.blackboard["manager_invalid_plan"] = {"reason": "Manager planning did not return JSON."}
        self.assertTrue(_should_use_deterministic_repo_context_plan(ctx))

    def test_deterministic_repo_context_plan_still_requires_graph_backed_first_attempt(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(env={}),
            blackboard={
                "repo_context": {
                    "required_files_applied_as_target_files": True,
                    "source": "fallback",
                    "fallback_used": True,
                }
            },
            task={"execution_kind": "code_edit", "source": {"type": "linear"}},
        )

        self.assertFalse(_should_use_deterministic_repo_context_plan(ctx))

    def test_run_manager_prompt_uses_graph_first_repo_context_plan_without_manager_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            required_files = [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
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
                    "task_id": "TAN-170",
                    "title": "LACA-12 Add per-issue worktree and branch isolation",
                    "description": "Add per-issue worktree and branch isolation for parallel ACA.",
                    "acceptance_criteria": [
                        "Create one worktree and branch per claimed Linear issue.",
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
            self.assertEqual(
                result["engine"],
                {"skipped": True, "reason": "repo_context_required_files_graph_first"},
            )
            self.assertEqual(
                [subtask["id"] for subtask in ctx.manager_plan["subtasks"]],
                ["fallback-repository-isolation"],
            )
            self.assertEqual(
                ctx.blackboard["manager_deterministic_repo_context_plan"]["reason"],
                "repo_context_required_files_graph_first",
            )

    def test_run_manager_prompt_uses_explicit_docs_plan_without_manager_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            (repo_path / "docs").mkdir(parents=True)
            (repo_path / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")
            issue_body = """## Task contract

Repo: frumu-ai/tandem-agents

Source files:

* docs/ACA_SMOKE_HARNESS.md
* docs/README.md

Required test files:

* None; docs-only unless verification fails.
"""
            status_path = run_dir / "status.json"
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}, swarm=SimpleNamespace(max_workers=1, enabled=False)),
                repo_path=repo_path,
                task={
                    "task_id": "TAN-347",
                    "title": "ACA smoke 05: document the smoke harness contract",
                    "description": issue_body,
                    "raw_issue_body": issue_body,
                    "acceptance_criteria": [
                        "Add `docs/ACA_SMOKE_HARNESS.md` describing the harness.",
                        "Link the new document from `docs/README.md`.",
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
                artifact_path=str(run_dir / "artifacts" / "repo_context_bundle.json"),
                path_scope="docs",
                required_files=[],
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="Likely docs files",
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
            self.assertEqual(
                result["engine"],
                {"skipped": True, "reason": "explicit_docs_task_targets_graph_first"},
            )
            self.assertEqual(
                ctx.manager_plan["subtasks"][0]["files"],
                ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            )
            self.assertEqual(
                ctx.blackboard["manager_deterministic_explicit_target_plan"]["reason"],
                "explicit_docs_task_targets_graph_first",
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "manager.deterministic_explicit_target_plan")

    def test_run_manager_prompt_uses_deterministic_repo_context_plan_after_manager_failure(self) -> None:
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
                blackboard={"manager_invalid_plan": {"reason": "Manager planning did not return JSON."}},
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
            self.assertEqual(
                result["engine"],
                {"skipped": True, "reason": "repo_context_required_files_after_manager_failure"},
            )
            subtask_ids = [subtask["id"] for subtask in ctx.manager_plan["subtasks"]]
            self.assertEqual(
                subtask_ids[:3],
                [
                    "fallback-throughput-config-types",
                    "fallback-throughput-config-loader",
                    "fallback-throughput-config-loader-tests",
                ],
            )
            config_types = ctx.manager_plan["subtasks"][0]
            self.assertEqual(config_types["files"], ["src/tandem_agents/config/config_types.py"])
            self.assertIn("defaults", " ".join(config_types["acceptance_criteria"]).lower())
            self.assertIn("max_concurrent_worker_runs", " ".join(config_types["acceptance_criteria"]))
            self.assertIn("config_types.py", config_types["scope_note"])
            config_loader = ctx.manager_plan["subtasks"][1]
            self.assertEqual(config_loader["files"], ["src/tandem_agents/config/config_loader.py"])
            loader_criteria = " ".join(config_loader["acceptance_criteria"])
            self.assertIn("ACA_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS", loader_criteria)
            self.assertIn("Do not add alias helpers", loader_criteria)
            self.assertIn("config.aca", loader_criteria)
            config_tests = ctx.manager_plan["subtasks"][2]
            self.assertEqual(config_tests["files"], ["src/tandem_agents/config/config_loader_test.py"])
            test_criteria = " ".join(config_tests["acceptance_criteria"])
            self.assertIn("resolve_config(root, env={...})", test_criteria)
            self.assertIn("config.scheduler.max_concurrent_worker_runs", test_criteria)
            self.assertIn("test-only slice", config_tests["scope_note"])
            self.assertIn("fallback-throughput-scheduler-controls", subtask_ids)
            scheduler_subtask = next(
                subtask
                for subtask in ctx.manager_plan["subtasks"]
                if subtask["id"] == "fallback-throughput-scheduler-controls"
            )
            scheduler_criteria = " ".join(scheduler_subtask["acceptance_criteria"])
            self.assertIn("max_concurrent_worker_runs", scheduler_criteria)
            self.assertIn("worker_concurrency_reached", scheduler_criteria)
            self.assertIn("plan_task_admissions", scheduler_subtask["scope_note"])
            self.assertNotIn("fallback-throughput-worker-metrics", subtask_ids)
            self.assertNotIn("fallback-throughput-operator-cockpit", subtask_ids)
            self.assertEqual(len(subtask_ids), ctx.cfg.swarm.max_workers)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "manager.deterministic_repo_context_plan")

    def test_run_manager_prompt_reuses_deterministic_repair_queue_without_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            status_path = run_dir / "status.json"
            failed_subtask = {
                "id": "fallback-throughput-config-loader",
                "title": "Config loader",
                "files": ["src/tandem_agents/config/config_loader.py"],
                "target_files": ["src/tandem_agents/config/config_loader.py"],
                "acceptance_criteria": ["Load exact scheduler fields."],
            }
            deferred_subtask = {
                "id": "fallback-throughput-config-loader-tests",
                "title": "Config loader tests",
                "files": ["src/tandem_agents/config/config_loader_test.py"],
                "target_files": ["src/tandem_agents/config/config_loader_test.py"],
                "acceptance_criteria": ["Test exact scheduler fields."],
            }
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}, swarm=SimpleNamespace(max_workers=4, enabled=False)),
                repo_path=repo_path,
                task={
                    "task_id": "TAN-173",
                    "title": "LACA-15 Add ACA throughput metrics, backpressure, and cost controls",
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
                blackboard={
                    "manager_deterministic_repo_context_plan": {"reason": "repo_context_required_files"},
                    "repair": {
                        "attempt": 2,
                        "completed_subtask_ids": ["fallback-throughput-config-types"],
                        "failed_subtask": failed_subtask,
                        "deferred_subtasks": [deferred_subtask],
                    },
                },
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "phase": {"name": "task_resolution", "detail": "task resolved"},
                    "blocker": {"active": False, "kind": None, "message": None, "owner_role": None},
                    "metrics": {},
                    "repair": {"attempt": 2},
                },
            )
            repo_context = SimpleNamespace(
                source="repo.context_bundle",
                fallback_used=False,
                error=None,
                artifact_path="",
                path_scope="src/tandem_agents",
                required_files=[],
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="",
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
            self.assertEqual(result["engine"], {"skipped": True, "reason": "deterministic_repo_context_repair_queue"})
            self.assertEqual(
                [subtask["id"] for subtask in ctx.manager_plan["subtasks"]],
                ["fallback-throughput-config-loader", "fallback-throughput-config-loader-tests"],
            )
            self.assertIn("retrying this deterministic repo-context slice", ctx.manager_plan["subtasks"][0]["scope_note"])
            self.assertIn("deferred this deterministic repo-context slice", ctx.manager_plan["subtasks"][1]["scope_note"])
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "manager.deterministic_repo_context_repair_plan")

    def test_run_manager_prompt_prioritizes_complementary_partial_repair_over_repo_context_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            repo_path = Path(tmp) / "repo"
            for child in ("artifacts", "logs"):
                (run_dir / child).mkdir(parents=True, exist_ok=True)
            repo_path.mkdir()
            status_path = run_dir / "status.json"
            failed_subtask = {
                "id": "fallback-repository-isolation-part-1",
                "title": "Repository isolation",
                "files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "acceptance_criteria": ["Create one worktree and branch per claimed Linear issue."],
            }
            deferred_subtask = {
                "id": "fallback-repository-isolation-part-2",
                "title": "Repository cleanup",
                "files": ["src/tandem_agents/core/repository/repository.py"],
                "target_files": ["src/tandem_agents/core/repository/repository.py"],
                "acceptance_criteria": ["Branch cleanup still works."],
            }
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(env={}, swarm=SimpleNamespace(max_workers=4, enabled=False)),
                repo_path=repo_path,
                task={
                    "task_id": "TAN-170",
                    "title": "LACA-12 Add per-issue worktree and branch isolation for parallel ACA runs",
                    "execution_kind": "code_edit",
                    "source": {"type": "linear"},
                    "target_files": [
                        "src/tandem_agents/core/repository/repository.py",
                        "src/tandem_agents/core/repository/repository_test.py",
                    ],
                    "task_contract": {
                        "target_files": [
                            "src/tandem_agents/core/repository/repository.py",
                            "src/tandem_agents/core/repository/repository_test.py",
                        ]
                    },
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
                blackboard={
                    "manager_deterministic_repo_context_plan": {"reason": "invalid_manager_repo_context_fallback"},
                    "repair": {
                        "attempt": 4,
                        "failed_subtask": failed_subtask,
                        "deferred_subtasks": [deferred_subtask],
                        "partial_diff_artifacts": [
                            {
                                "subtask_id": "fallback-repository-isolation-part-1",
                                "worker_id": "worker-1",
                                "patch_path": "/runs/run-1/artifacts/source.patch",
                                "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                                "patch_reusable": False,
                                "changed_files": ["src/tandem_agents/core/repository/repository.py"],
                                "worker_output_excerpt": (
                                    "Worker drifted off the required regression/test coverage path: after 122s "
                                    "it had changed only non-test files while required test files were "
                                    "src/tandem_agents/core/repository/repository_test.py."
                                ),
                                "subtask_target_files": [
                                    "src/tandem_agents/core/repository/repository.py",
                                    "src/tandem_agents/core/repository/repository_test.py",
                                ],
                            },
                            {
                                "subtask_id": "fallback-repository-isolation-part-1",
                                "worker_id": "worker-1",
                                "patch_path": "/runs/run-1/artifacts/test.patch",
                                "failure_reason": "WORKER_TEST_ONLY_DIFF",
                                "patch_reusable": False,
                                "changed_files": ["src/tandem_agents/core/repository/repository_test.py"],
                                "worker_output_excerpt": (
                                    "Worker changed only required test files for a regression subtask: after 191s "
                                    "it had not made the required production change."
                                ),
                                "subtask_target_files": [
                                    "src/tandem_agents/core/repository/repository.py",
                                    "src/tandem_agents/core/repository/repository_test.py",
                                ],
                            },
                        ],
                    },
                },
                status={
                    "run": {"run_id": "run-1", "status": "running"},
                    "phase": {"name": "task_resolution", "detail": "task resolved"},
                    "blocker": {"active": False, "kind": None, "message": None, "owner_role": None},
                    "metrics": {},
                    "repair": {"attempt": 4},
                },
            )
            repo_context = SimpleNamespace(
                source="repo.context_bundle",
                fallback_used=False,
                error=None,
                artifact_path="",
                path_scope="src/tandem_agents/core",
                required_files=[],
                index_source="stored",
                index_status="refreshed",
                index_error=None,
                text="",
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
            self.assertEqual(result["engine"], {"skipped": True, "reason": "complementary_guarded_partial_diff"})
            self.assertEqual(ctx.manager_plan["kind"], "complementary_guarded_partial_diff")
            subtask = ctx.manager_plan["subtasks"][0]
            self.assertEqual(subtask["title"], "Verify complementary source and test partial diffs")
            self.assertTrue(subtask["repair_requires_paired_source_test_diff"])
            self.assertTrue(subtask["repair_verification_first"])
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "manager.deterministic_repair_plan")
            self.assertEqual(events[-1]["payload"]["kind"], "complementary_guarded_partial_diff")

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
            self.assertEqual(
                ctx.blackboard["manager_deterministic_repo_context_plan"]["reason"],
                "invalid_manager_repo_context_fallback",
            )
            self.assertEqual(ctx.blackboard["manager_deterministic_repo_context_plan"]["planned_workers"], 3)

    def test_invalid_manager_fallback_prefers_explicit_task_targets_over_repo_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            context_path = root / "repo_context_bundle.json"
            for rel_path in (
                "docs/AUTONOMOUS_CODING_PYTHON_SDK_GIT_ACCESS.md",
                "docs/CODING_TASKS_WITH_TANDEM.md",
                "docs/CONFIG_SCHEMA.md",
                "docs/RUN_STATE_SCHEMA.md",
                "docs/README.md",
            ):
                (repo_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
                (repo_path / rel_path).write_text("# doc\n", encoding="utf-8")
            context_path.write_text(
                json.dumps(
                    {
                        "bundle": {
                            "suggested_first_reads": [
                                "docs/AUTONOMOUS_CODING_PYTHON_SDK_GIT_ACCESS.md",
                                "docs/CODING_TASKS_WITH_TANDEM.md",
                                "docs/CONFIG_SCHEMA.md",
                                "docs/RUN_STATE_SCHEMA.md",
                                "docs/README.md",
                            ],
                            "likely_files": [
                                {"file_path": "docs/RUN_STATE_SCHEMA.md"},
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            issue_body = """## Task contract

Repo: frumu-ai/tandem-agents

Goal: document the deterministic ACA smoke harness.

Source files:

* docs/ACA_SMOKE_HARNESS.md
* docs/README.md

Required test files:

* None; docs-only unless verification fails.
"""
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=False, max_workers=1), env={}),
                task={
                    "title": "ACA smoke 05: document the smoke harness contract",
                    "execution_kind": "code_edit",
                    "source": {"type": "linear"},
                    "description": issue_body,
                    "raw_issue_body": issue_body,
                    "acceptance_criteria": [
                        "Add `docs/ACA_SMOKE_HARNESS.md` describing the harness.",
                        "Link the new document from `docs/README.md`.",
                    ],
                },
                manager_plan={"summary": "bad", "subtasks": [], "risks": [], "tests": []},
                repo={"path": str(repo_path)},
                repo_path=repo_path,
                blackboard={
                    "manager_invalid_plan": {"reason": "Manager planning timed out."},
                    "repo_context": {
                        "artifact_path": str(context_path),
                        "required_files": [],
                    },
                },
            )
            setattr(ctx, "_manager_fallback_required", True)

            with mock.patch(
                "src.tandem_agents.core.execution.runner_core._prepare_subtasks_with_discovery",
                return_value=([], []),
            ):
                discovered_files, subtasks = _prepare_subtasks(ctx)

            self.assertEqual(discovered_files, ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"])
            self.assertEqual(len(subtasks), 1)
            self.assertEqual(subtasks[0]["files"], ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"])
            self.assertEqual(subtasks[0]["target_files"], ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"])
            self.assertIn("explicit task fallback targets", subtasks[0]["scope_note"])
            self.assertEqual(
                ctx.blackboard["manager_deterministic_repo_context_plan"]["reason"],
                "invalid_manager_explicit_task_targets_fallback",
            )
            self.assertEqual(
                ctx.blackboard["manager_deterministic_repo_context_plan"]["required_files"],
                ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            )

    def test_invalid_manager_fallback_caps_after_dense_serial_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            context_path = root / "repo_context_bundle.json"
            for rel_path in (
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/phases/finalize.py",
                "src/tandem_agents/core/phases/pr_body.py",
            ):
                (repo_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
                (repo_path / rel_path).write_text("# target\n", encoding="utf-8")
            context_path.write_text(json.dumps({"bundle": {"suggested_first_reads": []}}), encoding="utf-8")
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(
                    swarm=SimpleNamespace(enabled=False, max_workers=1),
                    env={"ACA_SERIAL_SUBTASK_LIMIT": "2"},
                ),
                task={
                    "title": "Add worktree isolation",
                    "execution_kind": "code_edit",
                    "source": {"type": "linear"},
                    "acceptance_criteria": [
                        "Create one worktree and branch per claimed Linear issue.",
                        "Pin repo base revision at claim time.",
                        "Track touched files and generated artifacts per run.",
                        "Detect overlapping file edits across active ACA runs.",
                        "Pause, serialize, or request operator approval when conflicts are likely.",
                        "Parallel issues do not share a mutable working directory.",
                        "Workers must not share a parallel mutable working directory.",
                    ],
                },
                manager_plan={"summary": "bad", "subtasks": [], "risks": [], "tests": []},
                repo={"path": str(repo_path)},
                repo_path=repo_path,
                blackboard={
                    "manager_invalid_plan": {"reason": "Manager planning timed out."},
                    "repo_context": {
                        "artifact_path": str(context_path),
                        "required_files": [
                            "src/tandem_agents/core/repository/repository.py",
                            "src/tandem_agents/core/repository/repository_test.py",
                            "src/tandem_agents/core/phases/task_intake.py",
                            "src/tandem_agents/core/phases/finalize.py",
                            "src/tandem_agents/core/phases/pr_body.py",
                        ],
                    },
                },
            )
            setattr(ctx, "_manager_fallback_required", True)

            with mock.patch(
                "src.tandem_agents.core.execution.runner_core._prepare_subtasks_with_discovery",
                return_value=([], []),
            ):
                _discovered_files, subtasks = _prepare_subtasks(ctx)
            _split_dense_serial_subtasks(ctx, subtasks)

            self.assertEqual(len(subtasks), 2)
            self.assertEqual(
                [subtask["id"] for subtask in subtasks],
                ["fallback-repository-isolation-part-1", "fallback-repository-isolation-part-2"],
            )
            self.assertEqual(ctx.blackboard["manager_fallback_serial_cap"]["limit"], 2)
            self.assertGreater(ctx.blackboard["manager_fallback_serial_cap"]["original_planned_workers"], 2)

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

    def test_prepare_subtasks_merges_manager_plan_when_swarm_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            ctx = SimpleNamespace(
                cfg=SimpleNamespace(swarm=SimpleNamespace(enabled=False, max_workers=1), env={}),
                task={"title": "Do complete manager plan"},
                manager_plan={
                    "subtasks": [
                        {"id": "one", "title": "One", "goal": "First"},
                        {"id": "two", "title": "Two", "goal": "Second"},
                        {"id": "three", "title": "Three", "goal": "Third"},
                        {"id": "four", "title": "Four", "goal": "Fourth"},
                        {"id": "five", "title": "Five", "goal": "Fifth"},
                        {"id": "six", "title": "Six", "goal": "Sixth"},
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

            self.assertEqual(prepare.call_args.kwargs["merge_manager_subtasks"], True)
            self.assertEqual(prepare.call_args.args[3], 1)

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
