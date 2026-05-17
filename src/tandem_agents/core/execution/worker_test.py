from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.tandem_agents.core.execution.worker import _coerce_worker_failure


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


if __name__ == "__main__":
    unittest.main()
