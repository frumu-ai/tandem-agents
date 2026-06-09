from __future__ import annotations

import unittest

from src.tandem_agents.core.task_contract import apply_task_contract, task_plan_validation


class TaskContractBugMonitorTest(unittest.TestCase):
    def test_bug_monitor_issue_sections_map_to_normal_coding_contract(self) -> None:
        body = """## What happened?

Workflow failed while publishing a Bug Monitor issue.

## Files likely involved

- crates/tandem-server/src/bug_monitor_github.rs
- crates/tandem-server/src/http/bug_monitor_parts/part05.rs

## Acceptance criteria

- Bug Monitor publish failures are reported once
- Existing GitHub issues are not duplicated

## Verification steps

- cargo test -p tandem-server --lib bug_monitor

## Recommended fix

Deduplicate issue creation before publishing.
"""

        task = apply_task_contract({"title": "Fix Bug Monitor duplicate issue spam", "description": body})

        self.assertEqual(
            task["target_files"],
            [
                "crates/tandem-server/src/bug_monitor_github.rs",
                "crates/tandem-server/src/http/bug_monitor_parts/part05.rs",
            ],
        )
        self.assertEqual(
            task["acceptance_criteria"],
            [
                "Bug Monitor publish failures are reported once",
                "Existing GitHub issues are not duplicated",
            ],
        )
        self.assertEqual(
            task["verification_commands"],
            ["cargo test -p tandem-server --lib bug_monitor"],
        )
        self.assertIn("Deduplicate issue creation before publishing.", task["notes_for_agent"])
        self.assertIsNone(task["tandem_coder_handoff"])

    def test_files_likely_touched_maps_to_target_files_and_strips_notes(self) -> None:
        body = """## Files Likely Touched

- `crates/tandem-enterprise-contract/src/lib.rs`
- `crates/tandem-server/src/http/middleware.rs` only if needed
- `crates/tandem-server/src/http/tests/enterprise.rs` only if needed
"""

        task = apply_task_contract({"title": "Add tenant constructors", "description": body})

        self.assertEqual(
            task["target_files"],
            [
                "crates/tandem-enterprise-contract/src/lib.rs",
                "crates/tandem-server/src/http/middleware.rs",
                "crates/tandem-server/src/http/tests/enterprise.rs",
            ],
        )

    def test_tandem_coder_handoff_enriches_without_being_required(self) -> None:
        body = """## What happened?

Provider stream failures masked a missing workspace artifact.

<!-- tandem:coder_handoff:v1
{
  "handoff_type": "tandem_autonomous_coder_issue",
  "source": "bug_monitor",
  "repo": "frumu-ai/tandem",
  "triage_run_id": "automation-v2-run-123",
  "incident_id": "incident-123",
  "draft_id": "draft-123",
  "failure_type": "workflow_contract_failure",
  "likely_files_to_edit": [
    "crates/tandem-core/src/engine_loop/prompt_execution.rs"
  ],
  "acceptance_criteria": [
    "Provider stream decode retries do not mask prior contract failures"
  ],
  "verification_steps": [
    "cargo test -p tandem-core --lib provider_stream"
  ],
  "risk_level": "medium",
  "coder_ready": true
}
-->
"""

        task = apply_task_contract({"title": "Fix provider stream masking", "description": body})

        self.assertEqual(
            task["target_files"],
            ["crates/tandem-core/src/engine_loop/prompt_execution.rs"],
        )
        self.assertEqual(
            task["acceptance_criteria"],
            ["Provider stream decode retries do not mask prior contract failures"],
        )
        self.assertEqual(
            task["verification_commands"],
            ["cargo test -p tandem-core --lib provider_stream"],
        )
        self.assertEqual(task["tandem_coder_handoff"]["source"], "bug_monitor")
        self.assertIn("Bug Monitor triage run: automation-v2-run-123", task["notes_for_agent"])
        self.assertIn("Bug Monitor coder-ready signal: True", task["notes_for_agent"])

    def test_linear_decision_note_task_is_not_code_edit(self) -> None:
        body = """## Context

There are 19 open Bolt/Jules-style PRs.

## Acceptance

* Each PR gets one of: close, cherry-pick, superseded-by-consolidated-PR, needs-manual-review.
* Decision notes are posted in this Linear issue or linked follow-up comments.
* No PR is merged directly without current-main validation.
"""

        task = apply_task_contract(
            {
                "title": "Inventory Bolt/Jules PRs and record close/merge decision per PR",
                "description": body,
                "source": {"type": "linear", "identifier": "TAN-109"},
            }
        )

        self.assertEqual(task["execution_kind"], "linear_comment")
        self.assertEqual(task["target_files"], [])

    def test_linear_code_task_stays_code_edit(self) -> None:
        body = """## Files likely touched

- crates/tandem-server/src/http/linear.rs

## Acceptance

- Implement Linear routing for task intake.
"""

        task = apply_task_contract(
            {
                "title": "Implement Linear routing",
                "description": body,
                "source": {"type": "linear", "identifier": "TAN-130"},
            }
        )

        self.assertEqual(task["execution_kind"], "code_edit")
        self.assertEqual(task["target_files"], ["crates/tandem-server/src/http/linear.rs"])
        self.assertEqual(task["acceptance_criteria"], ["Implement Linear routing for task intake."])

    def test_plain_acceptance_section_populates_contract_criteria(self) -> None:
        body = """## Context

Migrated from Signal Triage roadmap.

## Acceptance

* Research/Evidence triage vertical slice can intake a signal and produce a reviewed recommendation proposal.
* Use-Case Discovery can produce reviewed proposals without auto-enabling workflows.

## Verification

* Demo or tests for both additional vertical slices.
"""

        task = apply_task_contract(
            {
                "title": "SIG-03 Prove Research/Evidence and Use-Case Discovery triage domains",
                "description": body,
                "source": {"type": "linear", "identifier": "TAN-69"},
            }
        )

        self.assertEqual(
            task["acceptance_criteria"],
            [
                "Research/Evidence triage vertical slice can intake a signal and produce a reviewed recommendation proposal.",
                "Use-Case Discovery can produce reviewed proposals without auto-enabling workflows.",
            ],
        )
        self.assertEqual(task["verification_commands"], ["Demo or tests for both additional vertical slices."])

    def test_task_plan_validation_accepts_deliverable_as_subtask_checklist(self) -> None:
        task = {
            "title": "Verify Bug Monitor gates",
            "verification_commands": ["node scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
        }
        subtasks = [
            {
                "title": "Map gate flow",
                "goal": "Confirm existing Bug Monitor gate flow.",
                "deliverable": "A short note identifying gate APIs and the verification command.",
            }
        ]

        validation = task_plan_validation(task, subtasks)

        self.assertTrue(validation["ok"])
        self.assertEqual(validation["issues"], [])

    def test_task_plan_validation_accepts_required_work_as_subtask_checklist(self) -> None:
        task = {
            "title": "Verify Bug Monitor gates",
            "verification_commands": ["node scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
        }
        subtasks = [
            {
                "title": "Add focused fixture coverage",
                "goal": "Exercise mixed Bug Monitor signal fixtures.",
                "required_work": [
                    "Assert minor retries do not create draft work.",
                    "Assert blocked signals include quality-gate reasons.",
                ],
                "verification": ["Run the focused fixture test."],
            }
        ]

        validation = task_plan_validation(task, subtasks)

        self.assertTrue(validation["ok"])
        self.assertEqual(validation["issues"], [])

    def test_task_plan_validation_accepts_expected_verification_as_subtask_checklist(self) -> None:
        task = {
            "title": "Verify Bug Monitor gates",
            "verification_commands": ["node scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
        }
        subtasks = [
            {
                "title": "Add focused fixture coverage",
                "goal": "Exercise mixed Bug Monitor signal fixtures.",
                "instructions": [
                    "Add or refine a focused fixture that covers quality-gate outcomes.",
                ],
                "expected_verification": [
                    "Focused Bug Monitor tests pass and cover accepted, retried, and blocked signals.",
                ],
            }
        ]

        validation = task_plan_validation(task, subtasks)

        self.assertTrue(validation["ok"])
        self.assertEqual(validation["issues"], [])

    def test_task_plan_validation_accepts_scope_as_subtask_checklist(self) -> None:
        task = {
            "title": "Add prompt-injection exfiltration evals",
            "verification_commands": ["cargo test -p tandem-server eval"],
        }
        subtasks = [
            {
                "title": "Add KB-MCP bulk export scenarios",
                "goal": "Cover prompt-injected memory export attempts.",
                "scope": "Add YAML eval scenarios and bounded-exposure assertions for no bulk export.",
            }
        ]

        validation = task_plan_validation(task, subtasks)

        self.assertTrue(validation["ok"])
        self.assertEqual(validation["issues"], [])

    def test_linear_github_pr_action_task_is_not_code_edit(self) -> None:
        body = """## Context

11 generated-style PRs currently have at least one failed check.

Candidates:

* #1457 — failing
* #1400 — failing deep gate, 54 files

## Acceptance

* Re-check whether any candidate has become mergeable on current main.
* Close clear duplicates/stale generated branches with a concise GitHub comment.
* Do not close #1400 without explicit manual confirmation.
"""

        task = apply_task_contract(
            {
                "title": "Close duplicate or stale failing Bolt PRs",
                "description": body,
                "source": {"type": "linear", "identifier": "TAN-110"},
            }
        )

        self.assertEqual(task["execution_kind"], "github_pr_action")
        self.assertEqual(task["target_files"], [])


if __name__ == "__main__":
    unittest.main()
