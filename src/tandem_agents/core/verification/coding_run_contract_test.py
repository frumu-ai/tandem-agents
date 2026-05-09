from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract


class CodingRunContractTest(unittest.TestCase):
    def test_code_editing_contract_requires_diff_review_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = build_coding_run_contract(
                run_id="run-1",
                task={"title": "Fix README", "source": {"type": "github_project"}},
                repo_path=root,
                branch_name="aca/example/fix-readme-run-1",
                worktree_path=root / "worktree",
                expected_repo_files=["README.md", "src/app.py"],
            )

            self.assertTrue(contract.code_editing)
            self.assertTrue(contract.requires_diff_review_before_handoff)
            self.assertTrue(contract.requires_minimal_verification_before_handoff)
            self.assertEqual(contract.handoff_mode, "code_edit")
            self.assertIn("Review the diff before handoff.", contract.handoff_rules)

    def test_non_code_editing_contract_is_task_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = build_coding_run_contract(
                run_id="run-2",
                task={"title": "Status update", "source": {"type": "manual"}},
                repo_path=root,
                branch_name="aca/example/status-update-run-2",
                worktree_path=None,
                expected_repo_files=[],
            )

            self.assertFalse(contract.code_editing)
            self.assertFalse(contract.requires_diff_review_before_handoff)
            self.assertFalse(contract.requires_minimal_verification_before_handoff)
            self.assertEqual(contract.handoff_mode, "task_only")
            self.assertIn("No code-edit diff review is required for this run.", contract.handoff_rules)


if __name__ == "__main__":
    unittest.main()
