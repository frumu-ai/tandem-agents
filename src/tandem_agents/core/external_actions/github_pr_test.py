from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.external_actions.github_pr import (
    default_action_plan,
    enqueue_approvals_for_plan,
    execute_approved_action,
    extract_pr_numbers,
    fetch_pr_contexts,
)


class GithubPrExternalActionTest(unittest.TestCase):
    def test_extracts_pr_numbers_and_default_plan_is_non_destructive(self) -> None:
        task = {
            "task_id": "TAN-110",
            "title": "Close duplicate or stale failing Bolt PRs",
            "description": "#1457 and #1400 need triage. Do not close #1400 without approval.",
            "source": {"type": "linear", "identifier": "TAN-110"},
        }
        prs = [
            {"number": 1457, "base_repo": "frumu-ai/tandem"},
            {"number": 1400, "base_repo": "frumu-ai/tandem"},
        ]

        self.assertEqual(extract_pr_numbers(task), [1457, 1400])
        actions = default_action_plan("run-1", task, prs)

        action_types = [action["action_type"] for action in actions]
        # Non-destructive by default: comment + leave_open for every PR, no close_pr.
        self.assertIn("comment_pr", action_types)
        self.assertIn("leave_open", action_types)
        self.assertIn("post_linear_summary", action_types)
        self.assertNotIn("close_pr", action_types)
        # Regression guard for TAN2-6: no destructive action in any default plan.
        self.assertTrue(all(action["action_type"] != "close_pr" for action in actions))
        self.assertFalse(any(action.get("risk_level") == "high" for action in actions))
        # And no leftover hardcoded PR-number special-casing: both PRs treated identically.
        left_open = sorted(
            action["target"]["pr_number"] for action in actions if action["action_type"] == "leave_open"
        )
        self.assertEqual(left_open, [1400, 1457])

    def test_default_plan_closes_only_when_opted_in(self) -> None:
        task = {
            "task_id": "TAN-110",
            "title": "Close stale PRs",
            "source": {"type": "linear", "identifier": "TAN-110"},
            "external_action": {"allow_close_pr": True, "close_pr_numbers": [1457]},
        }
        prs = [
            {"number": 1457, "base_repo": "frumu-ai/tandem"},
            {"number": 1400, "base_repo": "frumu-ai/tandem"},
        ]

        actions = default_action_plan("run-1", task, prs)

        # Only the opted-in PR gets a close_pr; the other is left open.
        close_targets = [a["target"]["pr_number"] for a in actions if a["action_type"] == "close_pr"]
        leave_targets = [a["target"]["pr_number"] for a in actions if a["action_type"] == "leave_open"]
        self.assertEqual(close_targets, [1457])
        self.assertEqual(leave_targets, [1400])

    def test_default_plan_opt_in_all_prs_when_no_numbers_listed(self) -> None:
        task = {
            "task_id": "TAN-110",
            "source": {"type": "linear", "identifier": "TAN-110"},
            "external_action": {"allow_close_pr": True},
        }
        prs = [{"number": 1457, "base_repo": "frumu-ai/tandem"}]

        actions = default_action_plan("run-1", task, prs)
        self.assertIn("close_pr", [a["action_type"] for a in actions])

    def test_fetch_pr_contexts_records_tan_111_candidates(self) -> None:
        cfg = SimpleNamespace(repository=SimpleNamespace(slug="frumu-ai/tandem"))
        task = {
            "task_id": "TAN-111",
            "title": "Consolidate worthwhile small Bolt optimizations into one intentional PR",
            "description": "Inspect #1459 and #1449, then apply only safe changes.",
            "source": {"type": "linear", "identifier": "TAN-111"},
        }

        with patch(
            "src.tandem_agents.core.external_actions.github_pr.get_pull_request",
            side_effect=[
                {"number": 1459, "title": "Small cleanup", "state": "open", "base": {"repo": {"full_name": "frumu-ai/tandem"}}},
                {"number": 1449, "title": "Bolt tweak", "state": "closed", "base": {"repo": {"full_name": "frumu-ai/tandem"}}},
            ],
        ), patch(
            "src.tandem_agents.core.external_actions.github_pr.get_pull_request_files",
            side_effect=[
                [{"filename": "src/a.ts", "additions": 3, "deletions": 1, "patch": "@@ patch"}],
                [{"filename": "src/b.ts", "additions": 1, "deletions": 0}],
            ],
        ):
            contexts = fetch_pr_contexts(cfg, task)

        self.assertEqual([context["number"] for context in contexts], [1459, 1449])
        self.assertTrue(all(context["base_repo"] == "frumu-ai/tandem" for context in contexts))
        self.assertEqual(contexts[0]["changed_files"], ["src/a.ts"])
        self.assertEqual(contexts[0]["files"][0]["patch_excerpt"], "@@ patch")

    def test_approval_queue_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoordinationStore(backend="sqlite", db_path=Path(tmp) / "coordination.sqlite3")
            store.ensure_schema()
            task = {"task_id": "TAN-110", "source": {"type": "linear", "identifier": "TAN-110"}}
            actions = [
                {
                    "action_type": "comment_pr",
                    "target": {"base_repo": "frumu-ai/tandem", "pr_number": 1457},
                    "payload": {"body": "closing"},
                    "risk_level": "medium",
                    "verification_marker": "marker",
                }
            ]

            rows = enqueue_approvals_for_plan(store, run_id="run-1", task=task, actions=actions)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "pending")

            approved = store.decide_external_action_approval(
                rows[0]["approval_id"],
                decision="approve",
                actor="tester",
                reason="ok",
            )
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["decided_by"], "tester")

    def test_comment_pr_executes_and_verifies_marker(self) -> None:
        cfg = SimpleNamespace(repository=SimpleNamespace(slug="frumu-ai/tandem"))
        approval = {
            "action_type": "comment_pr",
            "target": {"base_repo": "frumu-ai/tandem", "pr_number": 1457},
            "payload": {"body": "body <!-- marker -->"},
            "verification_marker": "marker",
        }

        def fake_execute(_cfg, tool, args):
            if tool == "mcp.github.issue_read":
                return {"output": '[{"body":"body <!-- marker -->"}]'}
            return {"output": "{}"}

        with patch("src.tandem_agents.core.external_actions.github_pr.ensure_github_mcp_connected", return_value=None):
            with patch("src.tandem_agents.core.external_actions.github_pr.execute_engine_tool", side_effect=fake_execute):
                result = execute_approved_action(cfg, approval)

        self.assertTrue(result["verified"])
        self.assertEqual(result["action_type"], "comment_pr")

    def test_close_pr_executes_and_verifies_closed_state(self) -> None:
        cfg = SimpleNamespace(repository=SimpleNamespace(slug="frumu-ai/tandem"))
        approval = {
            "action_type": "close_pr",
            "target": {"base_repo": "frumu-ai/tandem", "pr_number": 1457},
            "payload": {"state": "closed"},
            "verification_marker": "",
        }

        with patch("src.tandem_agents.core.external_actions.github_pr.ensure_github_mcp_connected", return_value=None):
            with patch("src.tandem_agents.core.external_actions.github_pr.execute_engine_tool", return_value={"output": "{}"}) as execute:
                with patch(
                    "src.tandem_agents.core.external_actions.github_pr.get_pull_request",
                    return_value={"number": 1457, "state": "closed"},
                ):
                    result = execute_approved_action(cfg, approval)

        self.assertTrue(result["verified"])
        self.assertEqual(execute.call_args.args[1], "mcp.github.update_pull_request")


if __name__ == "__main__":
    unittest.main()
