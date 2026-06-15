from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.core.phases.planning import (
    _apply_repo_context_required_files_to_task,
    _carry_forward_partial_diff_artifacts,
    _constrain_extra_partial_diff_repair_subtasks,
    _completed_repair_worker_results,
    _manager_plan_from_stdout,
    _remote_code_task_requires_worker_execution,
    _sanitize_partial_diff_artifact_paths_in_plan,
)


class PlanningPreScreenTest(unittest.TestCase):
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
        self.assertIn("Active repair targets are limited", subtasks[0]["scope_note"])

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

    def test_manager_plan_sanitizes_absolute_partial_patch_paths(self) -> None:
        plan = {
            "subtasks": [
                {
                    "acceptance_criteria": [
                        "Read the preserved partial patch at /workspace/tandem-agents/runs/run-1/artifacts/worker.patch only as failure evidence.",
                        "Run the narrow test target.",
                    ],
                }
            ]
        }

        _sanitize_partial_diff_artifact_paths_in_plan(plan)

        criteria = plan["subtasks"][0]["acceptance_criteria"]
        self.assertNotIn("/workspace/", "\n".join(criteria))
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


if __name__ == "__main__":
    unittest.main()
