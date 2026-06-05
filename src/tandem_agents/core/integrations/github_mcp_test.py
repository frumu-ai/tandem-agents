from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.integrations.github_mcp import (
    _project_item_status_name,
    add_issue_comment,
    create_pull_request,
    create_pull_request_metadata,
    github_project_status_key_is_actionable,
    github_project_status_name_for_outcome,
    github_project_status_name_for_task_state,
    normalize_pull_request_metadata,
    update_project_item_status,
)


class GitHubMcpIdempotenceTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_COORDINATION_SQLITE_PATH=tandem-data/coordination.sqlite3",
                    "ACA_OUTPUT_ROOT=runs",
                    "ACA_TASK_SOURCE_TYPE=github_project",
                    "ACA_TASK_SOURCE_OWNER=frumu-ai",
                    "ACA_TASK_SOURCE_REPO=example",
                    "ACA_TASK_SOURCE_PROJECT=1",
                    "ACA_TASK_SOURCE_ITEM=2",
                    "ACA_PROVIDER=openai",
                    "ACA_MODEL=gpt-4.1-mini",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "task_source:",
                    "  type: github_project",
                    "  owner: frumu-ai",
                    "  repo: example",
                    "  project: 1",
                    "  item: 2",
                    "repository:",
                    "  slug: frumu-ai/example",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-4.1-mini",
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

    def test_update_project_item_status_skips_when_live_status_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            task = {
                "source": {
                    "type": "github_project",
                    "owner": "frumu-ai",
                    "project": 1,
                    "project_item_id": 2,
                    "status_field_id": 7,
                    "status_option_map": {"in_progress": "opt-1"},
                }
            }
            with patch("src.tandem_agents.core.integrations.github_mcp.fetch_project_item") as fetch_mock:
                with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                    fetch_mock.return_value = {"status": {"name": "In progress"}}
                    warning = update_project_item_status(cfg, task, "In progress")
            self.assertIsNone(warning)
            fetch_mock.assert_called_once_with(cfg, "frumu-ai", 1, 2, fields=["7"])
            tool_mock.assert_not_called()

    def test_add_issue_comment_skips_existing_marker_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            task = {
                "run_id": "run-123",
                "title": "Task",
                "source": {
                    "type": "github_project",
                    "owner": "frumu-ai",
                    "repo_name": "example",
                    "issue_number": 12,
                    "issue_url": "https://github.com/frumu-ai/example/issues/12",
                },
            }
            body = "Hello\n\n<!-- aca:issue-comment:run-123 -->"
            with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                with patch("src.tandem_agents.core.integrations.github_mcp._fetch_issue_comments") as comments_mock:
                    comments_mock.return_value = [{"body": body}]
                    warning = add_issue_comment(cfg, task, body)
            self.assertIsNone(warning)
            tool_mock.assert_not_called()

    def test_create_pull_request_reuses_existing_head_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            task = {
                "run_id": "run-123",
                "title": "Task",
                "source": {"type": "github_project", "owner": "frumu-ai", "repo_name": "example"},
            }
            body = "PR body"
            marker = "<!-- aca:pull-request:run-123:tandem-agents/task-123 -->"
            with patch("src.tandem_agents.core.integrations.github_mcp.list_pull_requests") as list_mock:
                with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                    list_mock.return_value = [
                        {
                            "head": {"ref": "aca/task-123"},
                            "body": f"{body}\n\n{marker}",
                            "html_url": "https://github.com/frumu-ai/example/pull/7",
                        }
                    ]
                    url = create_pull_request(cfg, task, head_branch="aca/task-123", title="aca: Task", body=body)
            self.assertEqual(url, "https://github.com/frumu-ai/example/pull/7")
            tool_mock.assert_not_called()

    def test_create_pull_request_metadata_reuses_existing_head_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            task = {
                "run_id": "run-123",
                "title": "Task",
                "source": {"type": "github_project", "owner": "frumu-ai", "repo_name": "example"},
            }
            with patch("src.tandem_agents.core.integrations.github_mcp.list_pull_requests") as list_mock:
                with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                    list_mock.return_value = [
                        {
                            "number": 7,
                            "head": {"ref": "aca/task-123"},
                            "base": {"ref": "main", "repo": {"full_name": "frumu-ai/example"}},
                            "state": "open",
                            "reviewDecision": "REVIEW_REQUIRED",
                            "checks_status": "success",
                            "html_url": "https://github.com/frumu-ai/example/pull/7",
                        }
                    ]
                    metadata = create_pull_request_metadata(
                        cfg,
                        task,
                        head_branch="aca/task-123",
                        title="aca: Task",
                        body="PR body",
                    )

            self.assertTrue(metadata["reused"])
            self.assertEqual(metadata["url"], "https://github.com/frumu-ai/example/pull/7")
            self.assertEqual(metadata["number"], 7)
            self.assertEqual(metadata["head_branch"], "aca/task-123")
            self.assertEqual(metadata["base_branch"], "main")
            self.assertEqual(metadata["base_repo"], "frumu-ai/example")
            self.assertEqual(metadata["lifecycle_state"], "waiting-for-review")
            tool_mock.assert_not_called()

    def test_pull_request_lifecycle_state_transitions(self) -> None:
        self.assertEqual(
            normalize_pull_request_metadata(
                {"state": "open", "draft": True, "number": 1, "checks_status": "pending"},
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["lifecycle_state"],
            "running",
        )
        self.assertEqual(
            normalize_pull_request_metadata(
                {"state": "open", "number": 1, "reviewDecision": "CHANGES_REQUESTED", "checks_status": "success"},
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["lifecycle_state"],
            "needs-repair",
        )
        self.assertEqual(
            normalize_pull_request_metadata(
                {"state": "open", "number": 1, "reviewDecision": "APPROVED", "checks_status": "success"},
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["lifecycle_state"],
            "ready-to-merge",
        )
        self.assertEqual(
            normalize_pull_request_metadata(
                {"state": "closed", "merged": True, "number": 1},
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["lifecycle_state"],
            "merged",
        )

    def test_github_project_status_mapping_is_explicit(self) -> None:
        self.assertEqual(github_project_status_name_for_task_state("active"), "In progress")
        self.assertEqual(github_project_status_name_for_task_state("blocked"), "Blocked")
        self.assertEqual(github_project_status_name_for_outcome("completed"), "Review")
        self.assertEqual(github_project_status_name_for_outcome("blocked"), "Blocked")
        self.assertTrue(github_project_status_key_is_actionable("Ready"))

    def test_project_item_status_name_reads_top_level_string_status(self) -> None:
        self.assertEqual(_project_item_status_name({"id": "PVTI_123", "status": "TODOS"}), "TODOS")
        self.assertTrue(github_project_status_key_is_actionable("Backlog"))
        self.assertTrue(github_project_status_key_is_actionable("Todo"))
        self.assertTrue(github_project_status_key_is_actionable("TODOS"))
        self.assertFalse(github_project_status_key_is_actionable("Blocked"))
        self.assertFalse(github_project_status_key_is_actionable("In progress"))
        self.assertFalse(github_project_status_key_is_actionable("In review"))


if __name__ == "__main__":
    unittest.main()
