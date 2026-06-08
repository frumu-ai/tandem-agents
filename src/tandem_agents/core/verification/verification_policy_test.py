from __future__ import annotations

import json
import unittest

from src.tandem_agents.core.verification.verification_policy import evaluate_verification_policy, review_blocker_message, test_blocker_message


class VerificationPolicyTest(unittest.TestCase):
    def test_review_blocker_detects_required_fixes(self) -> None:
        review_result = {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "review_status": "pass_with_required_fixes",
                    "next_action": "repair_needed",
                    "required_fixes": ["update docs"],
                }
            ),
        }
        self.assertEqual(review_blocker_message(review_result), "Reviewer reported required fixes.")

    def test_review_blocker_detects_human_review_needed(self) -> None:
        review_result = {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "review_status": "review_inconclusive_verification_required",
                    "next_action": "human_review_needed",
                }
            ),
        }
        self.assertEqual(review_blocker_message(review_result), "Reviewer status is `review_inconclusive_verification_required`.")

    def test_test_blocker_detects_failed_results(self) -> None:
        test_result = {
            "returncode": 0,
            "stdout": json.dumps({"status": "pass", "next_action": "repair_needed", "results": [{"status": "failed"}]}),
        }
        self.assertEqual(test_blocker_message(test_result), "Tester results are `failed`.")

    def test_verification_policy_reports_retry_when_repair_needed(self) -> None:
        review_result = {"returncode": 0, "stdout": json.dumps({"status": "pass"})}
        test_result = {"returncode": 1, "stdout": json.dumps({"status": "failed"})}
        decision = evaluate_verification_policy(review_result, test_result, repo_validation={"ok": True, "expected_files": []})
        self.assertTrue(decision.should_retry)
        self.assertEqual(decision.outcome, "repair_needed")
        self.assertEqual(decision.test_blocker, "Tester results are `failed`.")

    def test_verification_policy_reports_human_review_needed(self) -> None:
        review_result = {
            "returncode": 0,
            "stdout": json.dumps({"status": "review_inconclusive_verification_required", "next_action": "human_review_needed"}),
        }
        test_result = {"returncode": 0, "stdout": json.dumps({"status": "pass"})}
        decision = evaluate_verification_policy(review_result, test_result, repo_validation={"ok": True, "expected_files": []})
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.outcome, "human_review_needed")
        self.assertEqual(decision.review_blocker, "Reviewer status is `review_inconclusive_verification_required`.")

    def test_verification_policy_categorizes_command_failure(self) -> None:
        decision = evaluate_verification_policy(
            {"returncode": 0, "stdout": json.dumps({"status": "pass"})},
            {"returncode": 0, "stdout": json.dumps({"status": "pass"})},
            repo_validation={
                "ok": False,
                "command_failures": [{"command": "pnpm -C packages/tandem-control-panel run build"}],
            },
        )

        self.assertEqual(decision.outcome, "blocked")
        self.assertEqual(decision.failure_category, "verification_failed")
        self.assertEqual(
            decision.repo_blocker,
            "Repository validation command failed: pnpm -C packages/tandem-control-panel run build",
        )

    def test_verification_policy_categorizes_unexpected_repo_changes(self) -> None:
        decision = evaluate_verification_policy(
            {"returncode": 0, "stdout": json.dumps({"status": "pass"})},
            {"returncode": 0, "stdout": json.dumps({"status": "pass"})},
            repo_validation={"ok": False, "unexpected_files": ["src/lib/utils.ts"]},
        )

        self.assertEqual(decision.outcome, "blocked")
        self.assertEqual(decision.failure_category, "unexpected_repo_changes")
        self.assertEqual(decision.repo_blocker, "Unexpected repository files changed: src/lib/utils.ts")

    def test_verification_policy_keeps_missing_verification_category(self) -> None:
        decision = evaluate_verification_policy(
            {"returncode": 0, "stdout": json.dumps({"status": "pass"})},
            {"returncode": 0, "stdout": json.dumps({"status": "pass"})},
            repo_validation={"ok": False, "verification_missing": True},
        )

        self.assertEqual(decision.outcome, "blocked")
        self.assertEqual(decision.failure_category, "verification_missing")


if __name__ == "__main__":
    unittest.main()
