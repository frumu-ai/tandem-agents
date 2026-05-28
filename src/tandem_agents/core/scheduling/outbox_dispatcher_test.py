from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.scheduling.outbox_dispatcher import dispatch_outbox_tick


class OutboxDispatcherTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_COORDINATION_SQLITE_PATH=tandem-data/coordination.sqlite3",
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

    def test_dispatches_status_and_comment_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            task = {
                "task_id": "task-1",
                "title": "Task One",
                "source": {
                    "type": "github_project",
                    "owner": "frumu-ai",
                    "repo_name": "example",
                    "project": 1,
                    "project_item_id": 2,
                    "status_field_id": 7,
                    "status_option_map": {"in_progress": "opt-1"},
                },
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.enqueue_outbox(
                kind="github_project.status_update",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={"task": task, "target_status": "In progress"},
                dedupe_key="run-1:claim",
            )
            store.enqueue_outbox(
                kind="github_issue.comment",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={
                    "task": task,
                    "run_id": "run-1",
                    "outcome": "completed",
                    "summary": "Finished",
                    "body": "comment body",
                },
                dedupe_key="run-1:comment",
            )

            with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_connected", return_value=None):
                with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_disconnected", return_value=None):
                    with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.update_project_item_status", return_value=None) as status_mock:
                        with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.add_issue_comment", return_value=None) as comment_mock:
                            summary = dispatch_outbox_tick(cfg, coordination=store)

            self.assertEqual(summary["dispatched"], 2)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(status_mock.call_count, 1)
            self.assertEqual(comment_mock.call_count, 1)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["dispatched_outbox"], 2)
            self.assertEqual(snapshot["summary"]["pending_outbox"], 0)

    def test_terminal_failure_marks_outbox_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            store.enqueue_outbox(
                kind="github_project.status_update",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={"task": {"source": {"type": "github_project"}}, "target_status": "In progress"},
                dedupe_key="run-1:claim",
            )

            with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_connected", return_value=None):
                with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_disconnected", return_value=None):
                    with patch(
                        "src.tandem_agents.core.scheduling.outbox_dispatcher.update_project_item_status",
                        return_value="Missing GitHub Project status metadata for target status 'In progress'.",
                    ):
                        summary = dispatch_outbox_tick(cfg, coordination=store)

            self.assertEqual(summary["failed"], 1)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["failed_outbox"], 1)

    def test_dispatches_pull_request_create_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            task = {
                "run_id": "run-1",
                "task_id": "task-1",
                "title": "Task One",
                "source": {
                    "type": "github_project",
                    "owner": "frumu-ai",
                    "repo_name": "example",
                },
            }
            store.enqueue_outbox(
                kind="github_pull_request.create",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={
                    "run_id": "run-1",
                    "task": task,
                    "head_branch": "aca/task-1",
                    "title": "aca: Task One",
                    "body": "PR body",
                },
                dedupe_key="run-1:pr",
            )

            with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_connected", return_value=None):
                with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_disconnected", return_value=None):
                    with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.create_pull_request", return_value="https://github.com/frumu-ai/example/pull/7") as pr_mock:
                        summary = dispatch_outbox_tick(cfg, coordination=store)

            self.assertEqual(summary["dispatched"], 1)
            self.assertEqual(pr_mock.call_count, 1)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["dispatched_outbox"], 1)
            self.assertEqual(snapshot["summary"]["pending_outbox"], 0)

    def test_dispatches_linear_status_and_comment_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)
            task = {
                "task_id": "ENG-2",
                "title": "Linear Task",
                "source": {
                    "type": "linear",
                    "team": "ENG",
                    "issue_id": "lin-2",
                    "identifier": "ENG-2",
                },
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.enqueue_outbox(
                kind="linear_issue.status_update",
                aggregate_type="task",
                aggregate_id="ENG-2",
                payload={"task": task, "target_status": "In Progress", "labels": ["aca-running"]},
                dedupe_key="run-1:linear-status",
            )
            store.enqueue_outbox(
                kind="linear_issue.comment",
                aggregate_type="task",
                aggregate_id="ENG-2",
                payload={"task": task, "run_id": "run-1", "outcome": "completed", "summary": "Finished", "body": "done"},
                dedupe_key="run-1:linear-comment",
            )

            with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_linear_mcp_connected", return_value=None):
                with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_linear_mcp_disconnected", return_value=None):
                    with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.linear_update_issue", return_value=None) as status_mock:
                        with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.linear_add_comment", return_value=None) as comment_mock:
                            summary = dispatch_outbox_tick(cfg, coordination=store)

            self.assertEqual(summary["dispatched"], 2)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(status_mock.call_count, 1)
            self.assertEqual(comment_mock.call_count, 1)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["dispatched_outbox"], 2)
            self.assertEqual(snapshot["summary"]["pending_outbox"], 0)

    def test_dispatch_tick_runs_for_postgres_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SimpleNamespace(coordination=SimpleNamespace(backend="postgres"))
            store = CoordinationStore(backend="sqlite", db_path=root / "coordination.sqlite3")
            store.ensure_schema()
            store.enqueue_outbox(
                kind="github_project.status_update",
                aggregate_type="task",
                aggregate_id="task-1",
                payload={"task": {"source": {"type": "github_project"}}, "target_status": "In progress"},
                dedupe_key="run-1:claim",
            )

            with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.CoordinationStore.from_config", return_value=store):
                with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_connected", return_value=None):
                    with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.ensure_github_mcp_disconnected", return_value=None):
                        with patch("src.tandem_agents.core.scheduling.outbox_dispatcher.update_project_item_status", return_value=None):
                            summary = dispatch_outbox_tick(cfg, coordination=None)

            self.assertEqual(summary["dispatched"], 1)
            self.assertEqual(summary["failed"], 0)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["summary"]["dispatched_outbox"], 1)
            self.assertEqual(snapshot["summary"]["pending_outbox"], 0)


if __name__ == "__main__":
    unittest.main()
