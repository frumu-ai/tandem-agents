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

    def test_passed_repo_commands_clear_tester_missing_validation_human_review(self) -> None:
        review_result = {"returncode": 0, "stdout": json.dumps({"status": "pass"})}
        test_result = {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "next_action": "human_review_needed",
                    "results": {
                        "missing_validation": "Frontend checks were not successfully run by the tester.",
                    },
                }
            ),
        }
        repo_validation = {
            "ok": True,
            "command_checks": [{"command": "pnpm -C packages/tandem-control-panel run build", "status": "pass"}],
            "command_failures": [],
        }

        decision = evaluate_verification_policy(review_result, test_result, repo_validation=repo_validation)

        self.assertEqual(decision.outcome, "pass")
        self.assertIsNone(decision.test_blocker)
        self.assertEqual(decision.test_outcome, "pass")

    def test_tester_human_review_still_blocks_without_passed_command_checks(self) -> None:
        review_result = {"returncode": 0, "stdout": json.dumps({"status": "pass"})}
        test_result = {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "next_action": "human_review_needed",
                    "results": {"missing_validation": "Frontend checks were not run."},
                }
            ),
        }

        decision = evaluate_verification_policy(review_result, test_result, repo_validation={"ok": True})

        self.assertEqual(decision.outcome, "human_review_needed")
        self.assertEqual(decision.test_blocker, "Tester status is `human_review_needed`.")

    def test_passed_repo_commands_clear_tester_blocked_missing_validation(self) -> None:
        review_result = {"returncode": 0, "stdout": json.dumps({"status": "pass"})}
        test_result = {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "next_action": "blocked",
                    "commands": [
                        {"command": "pnpm -C packages/tandem-control-panel run build", "status": "not_run"},
                    ],
                    "results": {
                        "build_verified": False,
                        "smoke_tests_verified": False,
                    },
                    "notes": ["Validation is inconclusive because build and smoke tests were not run."],
                }
            ),
        }
        repo_validation = {
            "ok": True,
            "command_checks": [
                {"command": "pnpm -C packages/tandem-control-panel run build", "status": "pass"},
                {"command": "pnpm -C packages/tandem-control-panel run test:smoke", "status": "pass"},
            ],
            "command_failures": [],
        }

        decision = evaluate_verification_policy(review_result, test_result, repo_validation=repo_validation)

        self.assertEqual(decision.outcome, "pass")
        self.assertIsNone(decision.test_blocker)
        self.assertEqual(decision.test_outcome, "pass")

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

    def test_repair_needed_takes_category_precedence_over_missing_verification(self) -> None:
        decision = evaluate_verification_policy(
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "next_action": "repair_needed",
                        "required_fixes": ["add concrete eval cases"],
                    }
                ),
            },
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "next_action": "repair_needed",
                        "results": [{"status": "failed", "message": "manifest is scaffold-only"}],
                    }
                ),
            },
            repo_validation={"ok": False, "verification_missing": True},
        )

        self.assertEqual(decision.outcome, "blocked")
        self.assertEqual(decision.failure_category, "review_repair_needed")
        self.assertEqual(decision.validation_blocker, "Reviewer reported required fixes.")


if __name__ == "__main__":
    unittest.main()
