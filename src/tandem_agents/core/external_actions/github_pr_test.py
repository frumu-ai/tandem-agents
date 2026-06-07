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
    def test_extracts_pr_numbers_and_default_plan_gates_writes(self) -> None:
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
        self.assertIn("comment_pr", action_types)
        self.assertIn("close_pr", action_types)
        self.assertIn("leave_open", action_types)
        self.assertIn("post_linear_summary", action_types)
        self.assertTrue(all(action["action_type"] != "close_pr" or action["target"]["pr_number"] != 1400 for action in actions))

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
        ):
            contexts = fetch_pr_contexts(cfg, task)

        self.assertEqual([context["number"] for context in contexts], [1459, 1449])
        self.assertTrue(all(context["base_repo"] == "frumu-ai/tandem" for context in contexts))

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
