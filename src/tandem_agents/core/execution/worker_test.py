from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.core.execution.worker import _coerce_worker_failure
from src.tandem_agents.core.phases.worker_dispatch import _apply_tolerated_failures


class WorkerFailureCoercionTest(unittest.TestCase):
    def test_github_tasks_do_not_treat_readable_targets_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 1, "stdout": "no changes"},
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertNotEqual(result.get("verified_existing"), True)

    def test_local_tasks_can_still_verify_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 1, "stdout": "no changes"},
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
            )

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result.get("verified_existing"))

    def test_github_tasks_do_not_tolerate_no_change_worker_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text(
                "tenant principal explicit hosted automation workspace actor constructor\n"
                "authority request local implicit defaults organization context\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                task={"source": {"type": "github_project"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Add explicit tenant principal constructors",
                        "goal": "Add explicit tenant principal constructors for hosted automation",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[{"subtask_id": "subtask-1", "status": "failed"}],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")


if __name__ == "__main__":
    unittest.main()
