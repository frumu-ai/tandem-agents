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


if __name__ == "__main__":
    unittest.main()
