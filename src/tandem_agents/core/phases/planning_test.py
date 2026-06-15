from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.core.phases.planning import (
    _carry_forward_partial_diff_artifacts,
    _constrain_extra_partial_diff_repair_subtasks,
    _completed_repair_worker_results,
    _manager_plan_from_stdout,
    _remote_code_task_requires_worker_execution,
)


class PlanningPreScreenTest(unittest.TestCase):
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
