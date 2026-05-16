from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.runtime.task_sources import (
    _collect_project_items,
    _select_github_project_item,
    _task_from_project,
    github_project_board_snapshot,
)


class GitHubProjectTaskSourceStatusTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_TASK_SOURCE_TYPE=github_project",
                    "ACA_TASK_SOURCE_OWNER=frumu-ai",
                    "ACA_TASK_SOURCE_REPO=example",
                    "ACA_TASK_SOURCE_PROJECT=1",
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

    def test_only_actionable_statuses_are_selected_for_intake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            items = [
                {
                    "project_item_id": 1,
                    "title": "Blocked item",
                    "effective_status_name": "Blocked",
                    "effective_status_key": "blocked",
                },
                {
                    "project_item_id": 2,
                    "title": "Ready item",
                    "effective_status_name": "Ready",
                    "effective_status_key": "ready",
                },
            ]

            chosen, eligible, warning = _select_github_project_item(
                cfg,
                owner="frumu-ai",
                project=1,
                items=items,
                allow_non_actionable=False,
            )

            self.assertTrue(eligible)
            self.assertIsNone(warning)
            self.assertEqual(chosen["project_item_id"], 2)

    def test_non_actionable_selection_is_rejected_when_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            items = [
                {
                    "project_item_id": 1,
                    "title": "Blocked item",
                    "effective_status_name": "Blocked",
                    "effective_status_key": "blocked",
                }
            ]

            chosen, eligible, warning = _select_github_project_item(
                cfg,
                owner="frumu-ai",
                project=1,
                items=items,
                allow_non_actionable=True,
            )

            self.assertFalse(eligible)
            self.assertIsNotNone(warning)
            self.assertEqual(chosen["project_item_id"], 1)

    def test_github_project_task_carries_full_repo_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir(parents=True, exist_ok=True)
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "task_source:",
                        "  type: github_project",
                        "  owner: frumu-ai",
                        "  repo: tandem",
                        "  project: 1",
                        "repository:",
                        "  slug: frumu-ai/tandem",
                        "  clone_url: https://github.com/frumu-ai/tandem",
                        "  path: /workspace/repos/tandem",
                        "  default_branch: main",
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
            cfg = resolve_config(root)
            schema = {"name": "Project 1", "fields": [{"id": 1, "name": "Status", "options": [{"id": 2, "name": "Ready"}]}]}
            items = [
                {
                    "project_item_id": 123,
                    "title": "Fix clone",
                    "effective_status_name": "Ready",
                    "effective_status_key": "ready",
                    "content": {"number": 19, "body": "Make clone work"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                task, _, _ = _task_from_project(cfg)

            self.assertEqual(task["repo"]["slug"], "frumu-ai/tandem")
            self.assertEqual(task["repo"]["clone_url"], "https://github.com/frumu-ai/tandem")
            self.assertEqual(task["repo"]["path"], "/workspace/repos/tandem")

    def test_todos_status_items_are_actionable_in_board_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [
                            {"id": 2, "name": "TODOS"},
                            {"id": 3, "name": "In progress"},
                        ],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 1356,
                    "title": "Workflow automation task",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
                    "content": {"number": 1356, "title": "Workflow automation task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            item = snapshot["items"][0]
            self.assertEqual(item["status_name"], "TODOS")
            self.assertEqual(item["status_key"], "todos")
            self.assertTrue(item["actionable"])

    def test_collect_project_items_reads_top_level_string_status(self) -> None:
        collected: list[dict[str, object]] = []

        _collect_project_items(
            {
                "items": [
                    {
                        "id": "PVTI_123",
                        "status": "TODOS",
                        "content": {
                            "type": "DraftIssue",
                            "title": "Tenant isolation task",
                        },
                    }
                ]
            },
            collected,
        )

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["project_item_id"], "PVTI_123")
        self.assertEqual(collected[0]["status_name"], "TODOS")
        self.assertEqual(collected[0]["title"], "Tenant isolation task")


if __name__ == "__main__":
    unittest.main()
