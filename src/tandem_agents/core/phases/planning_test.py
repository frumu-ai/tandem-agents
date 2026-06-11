from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.core.phases.planning import (
    _completed_repair_worker_results,
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


if __name__ == "__main__":
    unittest.main()
