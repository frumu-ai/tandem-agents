from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.integrations.linear_mcp import (
    _resolve_linear_tool_id,
    _tool_failed,
    ensure_linear_mcp_connected,
    linear_count_issues,
    linear_fetch_issue,
    linear_list_comments,
    linear_list_issues,
)


class LinearMcpIntegrationTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "task_source:",
                    "  type: linear",
                    "  team: TAN",
                    "repository:",
                    "  path: /workspace/repo",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-4.1-mini",
                    "linear_mcp:",
                    "  enabled: true",
                    "  server: linear",
                    "swarm:",
                    "  enabled: false",
                    "output:",
                    "  root: runs",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def test_linear_list_issues_prefers_linear_native_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            calls: list[dict[str, object]] = []

            def fake_execute(_cfg, aliases, args):
                calls.append(args)
                return {"metadata": {"result": {"issues": [{"id": "lin-1", "identifier": "TAN-1"}]}}}

            with patch("src.tandem_agents.core.integrations.linear_mcp._execute_linear_tool", side_effect=fake_execute):
                issues = linear_list_issues(
                    cfg,
                    team="Tandem",
                    project="Runtime",
                    statuses="Backlog,Todo",
                    labels="Coder Runtime",
                    query="intake",
                )

            self.assertEqual(issues[0]["identifier"], "TAN-1")
            self.assertEqual(
                calls[0],
                {
                    "limit": 50,
                    "team": "Tandem",
                    "project": "Runtime",
                    "query": "intake",
                    "state": "Backlog,Todo",
                    "label": "Coder Runtime",
                },
            )

    def test_linear_list_issues_retries_split_statuses_after_empty_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            calls: list[dict[str, object]] = []

            def fake_execute(_cfg, aliases, args):
                calls.append(args)
                if args.get("state") == "Backlog":
                    return {"metadata": {"result": {"issues": [{"id": "lin-1", "identifier": "TAN-1"}]}}}
                return {"metadata": {"result": {"issues": []}}}

            with patch("src.tandem_agents.core.integrations.linear_mcp._execute_linear_tool", side_effect=fake_execute):
                issues = linear_list_issues(cfg, team="Tandem", project="Runtime", statuses="Backlog,Todo")

            self.assertEqual([issue["identifier"] for issue in issues], ["TAN-1"])
            self.assertTrue(any(call.get("state") == "Backlog,Todo" for call in calls))
            self.assertTrue(any(call.get("state") == "Backlog" for call in calls))

    def test_linear_count_issues_dedupes_mcp_output_and_content_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            output = '{"issues":[{"id":"lin-1","identifier":"TAN-1"},{"id":"lin-2","identifier":"TAN-2"}],"hasNextPage":false}'

            def fake_execute(_cfg, aliases, args):
                return {
                    "metadata": {"result": {"content": [{"type": "text", "text": output}]}},
                    "output": output,
                }

            with patch("src.tandem_agents.core.integrations.linear_mcp._execute_linear_tool", side_effect=fake_execute):
                count = linear_count_issues(cfg, team="Tandem", project="Runtime")

            self.assertEqual(count, 2)

    def test_linear_fetch_issue_prefers_parsed_issue_over_mcp_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            output = (
                '{"id":"TAN-109","identifier":"TAN-109","title":"Inventory",'
                '"description":"Full Linear issue body"}'
            )

            def fake_execute(_cfg, aliases, args):
                return {"metadata": {"result": {"content": [{"type": "text", "text": output}]}}}

            with patch("src.tandem_agents.core.integrations.linear_mcp._execute_linear_tool", side_effect=fake_execute):
                issue = linear_fetch_issue(cfg, "TAN-109")

            self.assertEqual(issue["identifier"], "TAN-109")
            self.assertEqual(issue["description"], "Full Linear issue body")

    def test_tool_failed_recognizes_unknown_tool_output(self) -> None:
        self.assertTrue(_tool_failed({"output": "Unknown tool: mcp.linear.listComments"}))

    def test_resolve_linear_tool_does_not_guess_private_underscore_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            with patch("src.tandem_agents.core.integrations.linear_mcp.list_engine_tool_ids", return_value=[]), \
                patch(
                    "src.tandem_agents.core.integrations.linear_mcp.get_mcp_server",
                    return_value={"connected": True, "last_error": ""},
                ):
                with self.assertRaisesRegex(RuntimeError, "did not expose a tool") as raised:
                    _resolve_linear_tool_id(cfg, ["issues"])
                self.assertIn("mcp.linear.issues", str(raised.exception))
                self.assertNotIn("mcp.linear._issues", str(raised.exception))

    def test_ensure_linear_mcp_connected_fails_fast_when_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            server = {
                "connected": False,
                "enabled": True,
                "last_error": "Authorization required.",
                "last_auth_challenge": {"status": "pending"},
            }
            with patch("src.tandem_agents.core.integrations.linear_mcp.get_mcp_server", return_value=server), \
                patch("src.tandem_agents.core.integrations.linear_mcp._connect_mcp_server"), \
                patch("src.tandem_agents.core.integrations.linear_mcp.list_engine_tool_ids", return_value=[]), \
                patch("src.tandem_agents.core.integrations.linear_mcp.time.time", side_effect=[0.0, 11.0]):
                with self.assertRaisesRegex(RuntimeError, "not connected: Authorization required"):
                    ensure_linear_mcp_connected(cfg)

    def test_linear_list_comments_uses_issue_id_and_parses_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            calls: list[dict[str, object]] = []
            output = '{"comments":[{"id":"comment-1","body":"ACA run marker run-1"}]}'

            def fake_execute(_cfg, aliases, args):
                calls.append(args)
                return {"metadata": {"result": {"content": [{"type": "text", "text": output}]}}}

            task = {"source": {"type": "linear", "issue_id": "TAN-109", "identifier": "TAN-109"}}
            with patch("src.tandem_agents.core.integrations.linear_mcp._execute_linear_tool", side_effect=fake_execute):
                comments = linear_list_comments(cfg, task)

            self.assertEqual(calls[0], {"issueId": "TAN-109"})
            self.assertEqual(comments[0]["id"], "comment-1")
            self.assertIn("run-1", comments[0]["body"])


if __name__ == "__main__":
    unittest.main()
