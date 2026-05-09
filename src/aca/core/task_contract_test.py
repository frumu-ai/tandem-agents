from __future__ import annotations

import unittest

from src.aca.core.task_contract import apply_task_contract


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


if __name__ == "__main__":
    unittest.main()
