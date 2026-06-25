from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.runtime.task_sources import (
    _collect_project_items,
    _hydrate_project_item_statuses_from_graphql,
    _github_project_board_cache_key,
    _github_project_schema,
    _linear_board_cache_key,
    _linear_status_is_actionable,
    _load_github_project_live_data,
    _select_github_project_item,
    _select_linear_issue,
    _task_from_project,
    _task_from_linear,
    github_project_board_snapshot,
    linear_board_snapshot,
    preview_task,
)
from src.tandem_agents.core.integrations.github_mcp import remember_project_item_status


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

    def test_forced_selection_matches_project_item_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.task_source.item = "188421137"
            items = [
                {
                    "project_item_id": "188421130",
                    "title": "First ready item",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
                },
                {
                    "project_item_id": "188421137",
                    "title": "Selected ready item",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
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
            self.assertEqual(chosen["project_item_id"], "188421137")

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
            self.assertEqual(task["repo"]["path"], str(Path("/workspace/repos/tandem").resolve()))

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

    def test_parent_status_items_are_not_actionable_in_board_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [{"id": 2, "name": "TODOS"}],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 1443,
                    "title": "[ACA Slice Parent] Launch gate",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
                    "content": {"number": 1443, "title": "[ACA Slice Parent] Launch gate"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            self.assertFalse(snapshot["items"][0]["actionable"])

    def test_board_scheduler_exposes_only_next_child_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [{"id": 2, "name": "TODOS"}],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 1440,
                    "title": "[ACA Slice Parent] Phase 0 - Foundations",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
                    "content": {"number": 1440, "title": "[ACA Slice Parent] Phase 0 - Foundations"},
                },
                {
                    "project_item_id": 1429,
                    "title": "[Tenant Isolation] Add explicit tenant/principal constructors for hosted automation",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
                    "content": {
                        "number": 1429,
                        "title": "[Tenant Isolation] Add explicit tenant/principal constructors for hosted automation",
                        "body": "Parent: [ACA Slice Parent] Phase 0 - Foundations\n",
                    },
                },
                {
                    "project_item_id": 1430,
                    "title": "[Tenant Isolation] Add shared two-tenant denial test helpers and matrix",
                    "effective_status_name": "TODOS",
                    "effective_status_key": "todos",
                    "content": {
                        "number": 1430,
                        "title": "[Tenant Isolation] Add shared two-tenant denial test helpers and matrix",
                        "body": "Parent: [ACA Slice Parent] Phase 0 - Foundations\n",
                    },
                },
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            by_issue = {item["issue_number"]: item for item in snapshot["items"]}
            self.assertFalse(by_issue[1440]["actionable"])
            self.assertTrue(by_issue[1429]["actionable"])
            self.assertEqual(by_issue[1429]["launch_state"], "next")
            self.assertFalse(by_issue[1430]["actionable"])
            self.assertEqual(by_issue[1430]["launch_state"], "queued")
            self.assertEqual(snapshot["scheduler"]["active_phase"], 0)

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

    def test_live_project_item_read_requests_status_field_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {"id": 267766852, "name": "Title"},
                    {"id": 267766854, "name": "Status", "options": [{"id": "ready-id", "name": "Ready"}]},
                ],
            }
            items_payload = {
                "output": '{"items":[{"id":188421130,"title":"Draft task","fields":[{"id":267766854,"name":"Status","value":"Ready"}]}]}'
            }

            def fake_tool(_cfg, tool, args):
                if tool == "mcp.github.projects_get" and args.get("method") == "get_project":
                    return {"output": json.dumps(schema)}
                if tool == "mcp.github.projects_list" and args.get("method") == "list_project_items":
                    self.assertEqual(args.get("fields"), ["267766854"])
                    return items_payload
                return {"output": "unknown tool: test"}

            with patch("src.tandem_agents.runtime.task_sources.ensure_github_mcp_connected"):
                with patch("src.tandem_agents.runtime.task_sources.execute_engine_tool", side_effect=fake_tool):
                    _, items = _load_github_project_live_data(cfg, owner="frumu-ai", project_number=1)

            self.assertEqual(items[0]["effective_status_name"], "Ready")
            self.assertEqual(items[0]["effective_status_key"], "ready")

    def test_github_project_schema_drift_error_identifies_read_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)

            with patch("src.tandem_agents.runtime.task_sources.execute_engine_tool", return_value={"output": "{}"}):
                with self.assertRaisesRegex(RuntimeError, "GitHub Projects read readiness degraded.*schema drift"):
                    _github_project_schema(cfg, owner="frumu-ai", project=1)

    def test_github_project_board_snapshot_reports_schema_drift_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {"name": "Project 1", "fields": [{"id": 1, "name": "Title"}]}
            items = [
                {
                    "project_item_id": 188421130,
                    "title": "Drifted task",
                    "content": {"number": 188421130, "title": "Drifted task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            self.assertFalse(snapshot["readiness"]["read"]["ready"])
            self.assertIn("GitHub Projects read readiness degraded", snapshot["readiness"]["read"]["message"])
            self.assertIn("schema drift", snapshot["readiness"]["read"]["message"])
            self.assertFalse(snapshot["readiness"]["write"]["ready"])
            self.assertIn("GitHub Projects write readiness degraded", snapshot["readiness"]["write"]["message"])

    def test_github_project_stale_cache_degrades_read_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [{"id": "ready-id", "name": "Ready"}],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 188421130,
                    "title": "Cached ready task",
                    "status_name": "Ready",
                    "content": {"number": 188421130, "title": "Cached ready task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    live_snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            self.assertTrue(live_snapshot["readiness"]["read"]["ready"])
            self.assertTrue(live_snapshot["readiness"]["write"]["ready"])

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                side_effect=RuntimeError("schema drift: live Project items unavailable"),
            ):
                stale_snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            self.assertEqual(stale_snapshot["source"], "cached")
            self.assertTrue(stale_snapshot["is_stale"])
            self.assertEqual(stale_snapshot["warning"], "schema drift: live Project items unavailable")
            self.assertFalse(stale_snapshot["readiness"]["read"]["ready"])
            self.assertIn("GitHub Projects read readiness degraded", stale_snapshot["readiness"]["read"]["message"])
            self.assertIn("schema drift", stale_snapshot["readiness"]["read"]["message"])
            self.assertTrue(stale_snapshot["readiness"]["write"]["ready"])

    def test_github_project_stale_legacy_cache_preserves_write_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cache_path = cfg.output_root() / "state" / "github_project_boards.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cached_snapshot = {
                "project": {
                    "owner": "frumu-ai",
                    "repo": "example",
                    "project_number": 1,
                    "name": "Project 1",
                },
                "status_field_id": 7,
                "status_option_map": {"ready": "ready-id"},
                "columns": [{"id": "ready-id", "name": "Ready", "key": "ready", "item_count": 1}],
                "items": [
                    {
                        "id": "188421130",
                        "project_item_id": 188421130,
                        "title": "Cached ready task",
                        "status_name": "Ready",
                        "status_key": "ready",
                    }
                ],
                "scheduler": {},
                "readiness": {"read": {"ready": True}, "write": {"ready": True}},
                "source": "live",
                "is_stale": False,
                "warning": "",
                "last_synced_at_ms": 1,
                "cache_age_ms": 0,
            }
            cache_key = _github_project_board_cache_key("frumu-ai", 1)
            cache_path.write_text(json.dumps({cache_key: cached_snapshot}), encoding="utf-8")

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                side_effect=RuntimeError("schema drift: live Project items unavailable"),
            ):
                stale_snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            self.assertEqual(stale_snapshot["source"], "cached")
            self.assertTrue(stale_snapshot["is_stale"])
            self.assertFalse(stale_snapshot["readiness"]["read"]["ready"])
            self.assertTrue(stale_snapshot["readiness"]["write"]["ready"])

    def test_github_project_readiness_degrades_when_status_is_cached_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            remember_project_item_status(
                cfg,
                owner="frumu-ai",
                project_number=1,
                item_id=188421130,
                status_name="Ready",
                source="test",
            )
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [{"id": "ready-id", "name": "Ready"}],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 188421130,
                    "title": "Cached ready task",
                    "effective_status_name": "Ready",
                    "effective_status_key": "ready",
                    "content": {"number": 188421130, "title": "Cached ready task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            item = snapshot["items"][0]
            self.assertEqual(item["status_key"], "ready")
            self.assertTrue(item["actionable"])
            self.assertFalse(snapshot["readiness"]["read"]["ready"])
            self.assertIn("GitHub Projects read readiness degraded", snapshot["readiness"]["read"]["message"])
            self.assertIn("status is missing", snapshot["readiness"]["read"]["message"])
            self.assertTrue(snapshot["readiness"]["write"]["ready"])

    def test_github_project_readiness_uses_raw_status_before_preview_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [{"id": "ready-id", "name": "Ready"}],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 188421130,
                    "title": "Selected missing status task",
                    "content": {"number": 188421130, "title": "Selected missing status task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch(
                    "src.tandem_agents.runtime.task_sources.preview_task",
                    return_value={"task": {"source": {"project_item_id": 188421130}}},
                ):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            item = snapshot["items"][0]
            self.assertEqual(item["status_key"], "ready")
            self.assertTrue(item["actionable"])
            self.assertFalse(snapshot["readiness"]["read"]["ready"])
            self.assertIn("GitHub Projects read readiness degraded", snapshot["readiness"]["read"]["message"])

    def test_reopened_terminal_project_item_prefers_live_actionable_status_over_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            remember_project_item_status(
                cfg,
                owner="frumu-ai",
                project_number=1,
                item_id=188421130,
                status_name="Done",
                source="test",
            )
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [
                            {"id": "todo-id", "name": "Todo"},
                            {"id": "done-id", "name": "Done"},
                        ],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 188421130,
                    "title": "Reopened task",
                    "status_name": "Todo",
                    "content": {"number": 188421130, "title": "Reopened task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                with patch("src.tandem_agents.runtime.task_sources.preview_task", return_value={}):
                    snapshot = github_project_board_snapshot(cfg, force_refresh=True)

            item = snapshot["items"][0]
            self.assertEqual(item["status_key"], "todo")
            self.assertTrue(item["actionable"])
            self.assertTrue(snapshot["readiness"]["read"]["ready"])
            self.assertTrue(snapshot["readiness"]["write"]["ready"])

    def test_github_project_preview_exposes_read_write_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "name": "Project 1",
                "fields": [
                    {
                        "id": 1,
                        "name": "Status",
                        "options": [{"id": "ready-id", "name": "Ready"}],
                    }
                ],
            }
            items = [
                {
                    "project_item_id": 188421130,
                    "title": "Ready task",
                    "status_name": "Ready",
                    "effective_status_name": "Ready",
                    "effective_status_key": "ready",
                    "content": {"number": 188421130, "title": "Ready task"},
                }
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_github_project_live_data",
                return_value=(schema, items),
            ):
                preview = preview_task(cfg)

            self.assertTrue(preview["readiness"]["read"]["ready"])
            self.assertTrue(preview["readiness"]["write"]["ready"])

    def test_hydrate_project_item_statuses_from_graphql_uses_node_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "fields": [
                    {
                        "name": "Status",
                        "options": [
                            {"id": "todo-id", "name": "TODOS"},
                            {"id": "done-id", "name": "Done"},
                        ],
                    }
                ]
            }
            items = [
                {
                    "project_item_id": "188421130",
                    "title": "Tenant isolation task",
                    "status_name": "",
                    "raw": {"node_id": "PVTI_123"},
                }
            ]
            graphql_payload = {
                "data": {
                    "nodes": [
                        {
                            "id": "PVTI_123",
                            "fieldValues": {
                                "nodes": [
                                    {"name": "TODOS"},
                                ]
                            },
                        }
                    ]
                }
            }

            with patch(
                "src.tandem_agents.runtime.task_sources._github_graphql",
                return_value=graphql_payload,
            ):
                _hydrate_project_item_statuses_from_graphql(cfg, schema, items)

            self.assertEqual(items[0]["status_name"], "TODOS")

    def test_hydrate_project_item_statuses_from_graphql_uses_database_ids_for_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            schema = {
                "fields": [
                    {
                        "name": "Status",
                        "options": [
                            {"id": "todo-id", "name": "TODOS"},
                            {"id": "done-id", "name": "Done"},
                        ],
                    }
                ]
            }
            items = [
                {
                    "project_item_id": "188421130",
                    "title": "Tenant isolation draft",
                    "status_name": "",
                    "raw": {"content": {"type": "DraftIssue", "title": "Tenant isolation draft"}},
                }
            ]
            graphql_payload = {
                "data": {
                    "organization": {
                        "projectV2": {
                            "items": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "PVTI_node",
                                        "databaseId": 188421130,
                                        "fieldValues": {
                                            "nodes": [
                                                {"name": "TODOS"},
                                            ]
                                        },
                                    }
                                ],
                            }
                        }
                    },
                    "user": None,
                }
            }

            with patch(
                "src.tandem_agents.runtime.task_sources._github_graphql",
                return_value=graphql_payload,
            ):
                _hydrate_project_item_statuses_from_graphql(cfg, schema, items)

            self.assertEqual(items[0]["status_name"], "TODOS")

    def test_github_token_uses_hosted_secret_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            with patch("src.tandem_agents.runtime.task_sources.Path.read_text", autospec=True, return_value="ghp_hosted\n") as read_text:
                from src.tandem_agents.runtime.task_sources import _github_token

                token = _github_token(cfg)

            self.assertEqual(token, "ghp_hosted")
            self.assertEqual(str(read_text.call_args.args[0]), "/run/secrets/github_token")


class LinearTaskSourceTest(unittest.TestCase):
    def _config(self, root: Path, *, require_repo_hint: bool = False):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        payload_lines = []
        if require_repo_hint:
            payload_lines = [
                "  payload:",
                "    repo_routing:",
                "      require_explicit_repo_hint: true",
            ]
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "task_source:",
                    "  type: linear",
                    "  team: ENG",
                    "  project: Runtime",
                    "  statuses: Backlog,Todo,Triage,Ready",
                    *payload_lines,
                    "repository:",
                    "  slug: frumu-ai/tandem",
                    "  clone_url: https://github.com/frumu-ai/tandem",
                    "  path: /workspace/repos/tandem",
                    "  default_branch: main",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-4.1-mini",
                    "linear_mcp:",
                    "  enabled: true",
                    "  server: linear",
                    "  scope: intake_finalize",
                    "  remote_sync: rich",
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

    def test_linear_board_cache_key_includes_repo_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            cfg_a = self._config(Path(tmp_a))
            cfg_b = self._config(Path(tmp_b))
            cfg_b.repository.slug = "frumu-ai/tandem-agents"
            cfg_b.repository.clone_url = "https://github.com/frumu-ai/tandem-agents"
            cfg_b.repository.path = "/workspace/repos/tandem-agents"

            self.assertNotEqual(_linear_board_cache_key(cfg_a), _linear_board_cache_key(cfg_b))

    def test_linear_cached_snapshots_do_not_report_github_project_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cache_path = cfg.output_root() / "state" / "linear_boards.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_key = _linear_board_cache_key(cfg)
            cached_snapshot = {
                "project": {"team": "ENG", "project": "Runtime", "name": "Runtime"},
                "columns": [],
                "items": [],
                "scheduler": {"next_item_ids": [], "next_issue_numbers": []},
                "source": "live",
                "is_stale": False,
                "warning": "",
                "last_synced_at_ms": 9_999_999_999_999,
                "cache_age_ms": 0,
                "readiness": {
                    "read": {
                        "ready": False,
                        "message": "GitHub Projects read readiness degraded: stale cache",
                    },
                    "write": {
                        "ready": False,
                        "message": "GitHub Projects write readiness degraded: stale cache",
                    },
                },
            }
            cache_path.write_text(json.dumps({cache_key: cached_snapshot}), encoding="utf-8")

            fresh_cached = linear_board_snapshot(cfg)

            self.assertEqual(fresh_cached["source"], "cached")
            self.assertNotIn("readiness", fresh_cached)

            cached_snapshot["last_synced_at_ms"] = 1
            cache_path.write_text(json.dumps({cache_key: cached_snapshot}), encoding="utf-8")
            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                side_effect=RuntimeError("linear unavailable"),
            ):
                stale_cached = linear_board_snapshot(cfg, force_refresh=True)

            self.assertEqual(stale_cached["source"], "cached")
            self.assertTrue(stale_cached["is_stale"])
            self.assertEqual(stale_cached["warning"], "linear unavailable")
            self.assertNotIn("readiness", stale_cached)

    def test_linear_selection_prefers_configured_actionable_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issues = [
                {"id": "lin-1", "identifier": "ENG-1", "title": "Started", "state": {"name": "In Progress", "type": "started"}},
                {"id": "lin-2", "identifier": "ENG-2", "title": "Ready", "state": {"name": "Todo", "type": "unstarted"}},
            ]

            chosen, eligible, warning = _select_linear_issue(cfg, issues=issues)

            self.assertTrue(eligible)
            self.assertIsNone(warning)
            self.assertEqual(chosen["identifier"], "ENG-2")

    def test_linear_status_mapping_matches_tandem_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.statuses = ""

            expected = {
                "Backlog": True,
                "Todo": True,
                "In Progress": False,
                "In Review": False,
                "Done": False,
                "Canceled": False,
            }

            for status_name, actionable in expected.items():
                with self.subTest(status=status_name):
                    self.assertEqual(_linear_status_is_actionable(cfg, status_name), actionable)

    def test_linear_selection_reports_no_eligible_issue_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issues = [
                {"id": "lin-1", "identifier": "ENG-1", "title": "Started", "state": {"name": "In Progress", "type": "started"}},
                {"id": "lin-2", "identifier": "ENG-2", "title": "Done", "state": {"name": "Done", "type": "completed"}},
            ]

            with self.assertRaisesRegex(RuntimeError, "No actionable Linear issues.*(Done.*In Progress|In Progress.*Done)"):
                _select_linear_issue(cfg, issues=issues)

    def test_linear_explicit_selector_can_resume_started_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.item = "ENG-1"
            issues = [
                {"id": "lin-1", "identifier": "ENG-1", "title": "Started", "state": {"name": "In Progress", "type": "started"}},
            ]

            chosen, eligible, warning = _select_linear_issue(cfg, issues=issues)

            self.assertTrue(eligible)
            self.assertIsNone(warning)
            self.assertEqual(chosen["identifier"], "ENG-1")

    def test_linear_explicit_selector_rejects_completed_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.item = "ENG-2"
            issues = [
                {"id": "lin-2", "identifier": "ENG-2", "title": "Done", "state": {"name": "Done", "type": "completed"}},
            ]

            with self.assertRaisesRegex(RuntimeError, "not actionable: status is .Done."):
                _select_linear_issue(cfg, issues=issues)

    def test_linear_board_snapshot_marks_only_scheduler_next_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issues = [
                {"id": "lin-1", "identifier": "ENG-1", "title": "Second", "priority": 3, "state": {"name": "Todo", "type": "unstarted"}},
                {"id": "lin-2", "identifier": "ENG-2", "title": "First", "priority": 1, "state": {"name": "Backlog", "type": "backlog"}},
            ]
            statuses = [
                {"id": "st-1", "name": "Backlog", "type": "backlog"},
                {"id": "st-2", "name": "Todo", "type": "unstarted"},
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                return_value=(statuses, [], issues),
            ):
                snapshot = linear_board_snapshot(cfg, force_refresh=True)

            by_identifier = {item["identifier"]: item for item in snapshot["items"]}
            self.assertTrue(by_identifier["ENG-2"]["actionable"])
            self.assertEqual(by_identifier["ENG-2"]["launch_state"], "next")
            self.assertFalse(by_identifier["ENG-1"]["actionable"])
            self.assertEqual(snapshot["scheduler"]["next_issue_numbers"], ["ENG-2"])

    def test_linear_board_snapshot_skips_incomplete_parent_for_scheduler_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issues = [
                {
                    "id": "lin-1",
                    "identifier": "ENG-1",
                    "title": "Parent umbrella",
                    "priority": 1,
                    "description": "Collect runtime intake follow-up work.",
                    "state": {"name": "Todo", "type": "unstarted"},
                },
                {
                    "id": "lin-2",
                    "identifier": "ENG-2",
                    "title": "Concrete child",
                    "priority": 2,
                    "description": "Goal: Add the regression.\n\nAcceptance:\n- Exercise the production path.",
                    "state": {"name": "Todo", "type": "unstarted"},
                },
            ]
            statuses = [
                {"id": "st-1", "name": "Todo", "type": "unstarted"},
            ]

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                return_value=(statuses, [], issues),
            ):
                snapshot = linear_board_snapshot(cfg, force_refresh=True)

            by_identifier = {item["identifier"]: item for item in snapshot["items"]}
            self.assertEqual(by_identifier["ENG-1"]["launch_state"], "waiting_contract")
            self.assertFalse(by_identifier["ENG-1"]["actionable"])
            self.assertTrue(by_identifier["ENG-2"]["actionable"])
            self.assertEqual(snapshot["scheduler"]["next_issue_numbers"], ["ENG-2"])

    def test_linear_board_snapshot_includes_active_items_outside_runnable_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            filtered_issues = [
                {"id": "lin-2", "identifier": "ENG-2", "title": "First", "priority": 1, "state": {"name": "Backlog", "type": "backlog"}},
            ]
            all_issues = [
                *filtered_issues,
                {"id": "lin-3", "identifier": "ENG-3", "title": "Started", "priority": 2, "state": {"name": "In Progress", "type": "started"}},
            ]
            statuses = [
                {"id": "st-1", "name": "Backlog", "type": "backlog"},
                {"id": "st-2", "name": "In Progress", "type": "started"},
            ]

            def fake_list_issues(_cfg, **kwargs):
                return filtered_issues if kwargs.get("statuses") else all_issues

            with (
                patch("src.tandem_agents.runtime.task_sources.ensure_linear_mcp_connected"),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issue_statuses", return_value=statuses),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issue_labels", return_value=[]),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issues", side_effect=fake_list_issues),
            ):
                snapshot = linear_board_snapshot(cfg, force_refresh=True)

            by_identifier = {item["identifier"]: item for item in snapshot["items"]}
            self.assertIn("ENG-3", by_identifier)
            self.assertEqual(by_identifier["ENG-3"]["project_column"], "In Progress")
            self.assertEqual(by_identifier["ENG-3"]["launch_state"], "in_progress")
            self.assertFalse(by_identifier["ENG-3"]["actionable"])
            self.assertEqual(snapshot["scheduler"]["next_issue_numbers"], ["ENG-2"])

    def test_linear_task_preview_uses_all_project_statuses_for_scheduler_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            filtered_issues = [
                {"id": "lin-3", "identifier": "ENG-3", "title": "Filtered backlog", "priority": 4, "state": {"name": "Backlog", "type": "backlog"}},
            ]
            todo_issue = {
                "id": "lin-1",
                "identifier": "ENG-1",
                "title": "Todo should run first",
                "priority": 2,
                "description": "Todo body\n\nAcceptance:\n- Run the Todo issue",
                "state": {"name": "Todo", "type": "unstarted"},
            }
            all_issues = [
                *filtered_issues,
                todo_issue,
                {"id": "lin-2", "identifier": "ENG-2", "title": "Later todo", "priority": 3, "state": {"name": "Todo", "type": "unstarted"}},
            ]
            statuses = [
                {"id": "st-1", "name": "Backlog", "type": "backlog"},
                {"id": "st-2", "name": "Todo", "type": "unstarted"},
            ]

            def fake_list_issues(_cfg, **kwargs):
                return filtered_issues if kwargs.get("statuses") else all_issues

            with (
                patch("src.tandem_agents.runtime.task_sources.ensure_linear_mcp_connected"),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issue_statuses", return_value=statuses),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issue_labels", return_value=[]),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issues", side_effect=fake_list_issues),
                patch("src.tandem_agents.runtime.task_sources.linear_fetch_issue", return_value=todo_issue),
            ):
                task, _board, _path = _task_from_linear(cfg)
                preview = preview_task(cfg)

            self.assertEqual(task["source"]["identifier"], "ENG-1")
            self.assertEqual(task["title"], "Todo should run first")
            self.assertIn("Run the Todo issue", task["acceptance_criteria"])
            self.assertEqual(preview["task"]["source"]["identifier"], "ENG-1")
            self.assertEqual(preview["task"]["title"], "Todo should run first")

    def test_linear_task_carries_repo_and_issue_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "Make Linear intake work\n- Add tests",
                "url": "https://linear.app/acme/issue/ENG-2/fix-runtime",
                "state": {"name": "Todo", "type": "unstarted"},
                "stateId": "ignored",
                "team": "ENG",
                "teamId": "team-1",
                "project": "Runtime",
                "projectId": "project-1",
                "labels": [{"name": "bug"}],
            }

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                return_value=([{"name": "Todo", "type": "unstarted"}], [], [issue]),
            ):
                task, _board, _path = _task_from_linear(cfg)

            self.assertEqual(task["source"]["type"], "linear")
            self.assertEqual(task["source"]["team"], "ENG")
            self.assertEqual(task["source"]["team_id"], "team-1")
            self.assertEqual(task["source"]["project_id"], "project-1")
            self.assertEqual(task["source"]["identifier"], "ENG-2")
            self.assertEqual(task["source"]["issue_id"], "lin-2")
            self.assertEqual(task["source"]["status_id"], "ignored")
            self.assertEqual(task["source"]["initial_status_key"], "todo")
            self.assertEqual(task["repo"]["slug"], "frumu-ai/tandem")
            self.assertIn("Add tests", task["acceptance_criteria"])

    def test_linear_task_repo_hint_matches_configured_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "Make Linear intake work\n\n## Repo\n`/home/evan/tandem`\n\nAcceptance:\n- Add tests",
                "state": {"name": "Todo", "type": "unstarted"},
            }

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                return_value=([{"name": "Todo", "type": "unstarted"}], [], [issue]),
            ):
                task, _board, _path = _task_from_linear(cfg)

            self.assertEqual(task["source"]["repo_hints"], ["/home/evan/tandem"])
            self.assertTrue(task["repo_routing"]["matched_configured_repo"])
            self.assertTrue(task["contract_completeness"]["ok"])

    def test_linear_task_repo_hint_mismatch_blocks_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "Make Linear intake work\n\n## Repo\n`/home/evan/tandem-agents`\n\nAcceptance:\n- Add tests",
                "state": {"name": "Todo", "type": "unstarted"},
            }

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                return_value=([{"name": "Todo", "type": "unstarted"}], [], [issue]),
            ):
                with self.assertRaisesRegex(RuntimeError, "repo hint does not match"):
                    _task_from_linear(cfg)

                preview = preview_task(cfg)

            self.assertFalse(preview["eligible"])
            self.assertEqual(preview["task"]["contract_completeness"]["blocker_kind"], "repo_binding_mismatch")
            self.assertIn("/home/evan/tandem-agents", preview["task"]["repo_routing"]["repo_hints"])

    def test_linear_task_source_can_require_explicit_repo_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), require_repo_hint=True)
            issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "Make Linear intake work\n\nAcceptance:\n- Add tests",
                "state": {"name": "Todo", "type": "unstarted"},
            }

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                return_value=([{"name": "Todo", "type": "unstarted"}], [], [issue]),
            ):
                with self.assertRaisesRegex(RuntimeError, "missing an explicit Repo/Repos section"):
                    _task_from_linear(cfg)

                preview = preview_task(cfg)

            self.assertFalse(preview["eligible"])
            self.assertEqual(preview["task"]["contract_completeness"]["blocker_kind"], "repo_hint_required")
            self.assertTrue(preview["task"]["repo_routing"]["require_explicit_repo_hint"])

    def test_linear_board_snapshot_hydrates_truncated_issue_for_repo_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), require_repo_hint=True)
            listed_issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "short ... (truncated, use `get_issue` for full description)",
                "state": {"name": "Todo", "type": "unstarted"},
            }
            full_issue = {
                **listed_issue,
                "description": "Full body\n\n## Repo\n`frumu-ai/tandem`\n\nAcceptance:\n- Add tests",
            }

            with (
                patch(
                    "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                    return_value=([{"name": "Todo", "type": "unstarted"}], [], [listed_issue]),
                ),
                patch("src.tandem_agents.runtime.task_sources.linear_fetch_issue", return_value=full_issue) as fetch_issue,
            ):
                snapshot = linear_board_snapshot(cfg, force_refresh=True)

            by_identifier = {item["identifier"]: item for item in snapshot["items"]}
            self.assertEqual(snapshot["scheduler"]["next_issue_numbers"], ["ENG-2"])
            self.assertEqual(by_identifier["ENG-2"]["launch_state"], "next")
            self.assertTrue(by_identifier["ENG-2"]["actionable"])
            self.assertEqual(by_identifier["ENG-2"]["contract_completeness"]["ok"], True)
            self.assertEqual(by_identifier["ENG-2"]["repo_routing"]["repo_hints"], ["frumu-ai/tandem"])
            fetch_issue.assert_called_once_with(cfg, "ENG-2")

    def test_linear_selection_hydrates_truncated_issue_before_repo_hint_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), require_repo_hint=True)
            listed_issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "short ... (truncated, use `get_issue` for full description)",
                "state": {"name": "Todo", "type": "unstarted"},
            }
            full_issue = {
                **listed_issue,
                "description": "Full body\n\n## Repo\n`frumu-ai/tandem`\n\nAcceptance:\n- Add tests",
            }

            with patch("src.tandem_agents.runtime.task_sources.linear_fetch_issue", return_value=full_issue) as fetch_issue:
                chosen, eligible, warning = _select_linear_issue(cfg, issues=[listed_issue])

            self.assertTrue(eligible)
            self.assertIsNone(warning)
            self.assertEqual(chosen["description"], full_issue["description"])
            fetch_issue.assert_called_once_with(cfg, "ENG-2")

    def test_linear_task_hydrates_selected_issue_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            listed_issue = {
                "id": "ENG-2",
                "identifier": "ENG-2",
                "title": "Fix runtime",
                "description": "short … (truncated, use `get_issue` for full description)",
                "state": {"name": "Todo", "type": "unstarted"},
            }
            full_issue = {
                **listed_issue,
                "description": "Full body\n\nAcceptance criteria:\n- Real criterion",
            }

            with (
                patch("src.tandem_agents.runtime.task_sources._load_linear_live_data", return_value=([], [], [listed_issue])),
                patch("src.tandem_agents.runtime.task_sources.linear_fetch_issue", return_value=full_issue),
            ):
                task, _board, _path = _task_from_linear(cfg)

            self.assertIn("Full body", task["description"])
            self.assertNotIn("truncated", task["description"])
            self.assertIn("Real criterion", task["acceptance_criteria"])

    def test_linear_task_fetches_selected_issue_missing_from_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.task_source.item = "ENG-2"
            listed_issue = {
                "id": "lin-1",
                "identifier": "ENG-1",
                "title": "Wrong next task",
                "description": "Not the selected task",
                "state": {"name": "Todo", "type": "unstarted"},
            }
            selected_issue = {
                "id": "lin-2",
                "identifier": "ENG-2",
                "title": "Explicitly selected task",
                "description": "Selected body\n\nAcceptance:\n- Selected criterion",
                "state": {"name": "Todo", "type": "unstarted"},
            }

            with (
                patch("src.tandem_agents.runtime.task_sources.ensure_linear_mcp_connected"),
                patch(
                    "src.tandem_agents.runtime.task_sources.linear_list_issue_statuses",
                    return_value=[{"name": "Todo", "type": "unstarted"}],
                ),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issue_labels", return_value=[]),
                patch("src.tandem_agents.runtime.task_sources.linear_list_issues", return_value=[listed_issue]),
                patch("src.tandem_agents.runtime.task_sources.linear_fetch_issue", return_value=selected_issue) as fetch_issue,
            ):
                task, _board, _path = _task_from_linear(cfg)

            fetch_issue.assert_called()
            self.assertEqual(task["source"]["identifier"], "ENG-2")
            self.assertEqual(task["title"], "Explicitly selected task")
            self.assertIn("Selected criterion", task["acceptance_criteria"])

    def test_linear_task_extracts_plain_acceptance_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            issue = {
                "id": "lin-69",
                "identifier": "TAN-69",
                "title": "SIG-03 Prove Research/Evidence and Use-Case Discovery triage domains",
                "description": "\n".join(
                    [
                        "## Context",
                        "",
                        "Migrated from Signal Triage roadmap.",
                        "",
                        "## Acceptance",
                        "",
                        "* Research/Evidence triage vertical slice can intake a signal.",
                        "* Use-Case Discovery can produce reviewed proposals.",
                        "",
                        "## Verification",
                        "",
                        "* Demo or tests for both additional vertical slices.",
                    ]
                ),
                "url": "https://linear.app/frumu/issue/TAN-69/sig-03",
                "state": {"name": "Backlog", "type": "backlog"},
                "team": {"name": "Tandem", "id": "team-1"},
                "project": {"name": "Signal Triage & Bug Monitor", "id": "project-1"},
            }

            with (
                patch(
                    "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                    return_value=([{"name": "Backlog", "type": "backlog"}], [], [issue]),
                ),
                patch("src.tandem_agents.runtime.task_sources.linear_fetch_issue", return_value=issue),
            ):
                task, _board, _path = _task_from_linear(cfg)

            self.assertEqual(
                task["acceptance_criteria"],
                [
                    "Research/Evidence triage vertical slice can intake a signal.",
                    "Use-Case Discovery can produce reviewed proposals.",
                ],
            )
            self.assertEqual(task["verification_commands"], ["Demo or tests for both additional vertical slices."])

    def test_linear_task_source_reports_connector_failure_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))

            with patch(
                "src.tandem_agents.runtime.task_sources._load_linear_live_data",
                side_effect=RuntimeError("Linear MCP server 'linear' is not configured"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Could not read Linear issues.*connected Linear MCP path.*ENG"):
                    _task_from_linear(cfg)


if __name__ == "__main__":
    unittest.main()
