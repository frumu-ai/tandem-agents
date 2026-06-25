from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.integrations.github_mcp import (
    _project_item_status_name,
    add_issue_comment,
    build_pull_request_repair_prompt,
    collect_pull_request_repair_context,
    create_pull_request,
    create_pull_request_metadata,
    evaluate_auto_merge_gates,
    github_project_operator_actions,
    github_project_status_key_is_actionable,
    github_project_status_name_for_outcome,
    github_project_status_name_for_task_state,
    github_projects_readiness_message,
    guarded_auto_merge,
    list_pull_requests,
    normalize_pull_request_metadata,
    remember_project_item_status,
    refresh_pull_request_lifecycle,
    update_project_item_status,
)


class GitHubMcpIdempotenceTest(unittest.TestCase):
    def _config(self, root: Path, *, review_policy: str = "human_review"):
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
                    "review:",
                    f"  policy: {review_policy}",
                    "  auto_merge_strategy: squash",
                    "  auto_merge_allowed_strategies: squash",
                    "  merge_requires_approval: false",
                    "  branch_delete_requires_approval: false",
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

    def test_update_project_item_status_reports_missing_write_readiness_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            task = {
                "source": {
                    "type": "github_project",
                    "owner": "frumu-ai",
                    "project": 1,
                    "project_item_id": 2,
                    "status_option_map": {},
                }
            }

            warning = update_project_item_status(cfg, task, "In progress")

            self.assertIn("GitHub Projects write readiness degraded", warning or "")
            self.assertIn("status_field_id", warning or "")
            self.assertIn("status option", warning or "")
            self.assertIn("Connect GitHub Project", warning or "")

    def test_update_project_item_status_reports_remote_terminal_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            remember_project_item_status(
                cfg,
                owner="frumu-ai",
                project_number=1,
                item_id=2,
                status_name="In progress",
                source="test",
            )
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
                    fetch_mock.return_value = {"status": {"name": "Done"}}
                    warning = update_project_item_status(cfg, task, "In progress")

            self.assertIn("GitHub Projects write readiness degraded", warning or "")
            self.assertIn("remote divergence", warning or "")
            self.assertIn("cached status 'In progress'", warning or "")
            self.assertIn("live status 'Done'", warning or "")
            self.assertIn("Re-sync outward", warning or "")
            self.assertIn("Ignore remote drift", warning or "")
            self.assertIn("Start new run from reopened item", warning or "")
            tool_mock.assert_not_called()

    def test_update_project_item_status_blocks_terminal_drift_for_review_target(self) -> None:
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
                    "status_option_map": {"in_review": "opt-1"},
                }
            }
            with patch("src.tandem_agents.core.integrations.github_mcp.fetch_project_item") as fetch_mock:
                with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                    fetch_mock.return_value = {"status": {"name": "Done"}}
                    warning = update_project_item_status(
                        cfg,
                        task,
                        github_project_status_name_for_task_state("review"),
                    )

            self.assertIn("GitHub Projects write readiness degraded", warning or "")
            self.assertIn("remote divergence", warning or "")
            self.assertIn("live status 'Done'", warning or "")
            self.assertIn("target status 'In review'", warning or "")
            tool_mock.assert_not_called()

    def test_update_project_item_status_reports_non_terminal_remote_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            remember_project_item_status(
                cfg,
                owner="frumu-ai",
                project_number=1,
                item_id=2,
                status_name="Ready",
                source="test",
            )
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
                    fetch_mock.return_value = {"status": {"name": "Blocked"}}
                    warning = update_project_item_status(cfg, task, "In progress")

            self.assertIn("GitHub Projects write readiness degraded", warning or "")
            self.assertIn("remote divergence", warning or "")
            self.assertIn("cached status 'Ready'", warning or "")
            self.assertIn("live status 'Blocked'", warning or "")
            self.assertIn("target status 'In progress'", warning or "")
            tool_mock.assert_not_called()

    def test_github_project_readiness_action_labels_are_stable(self) -> None:
        labels = [action["label"] for action in github_project_operator_actions()]
        self.assertIn("Connect GitHub Project", labels)
        self.assertIn("Re-sync outward", labels)
        self.assertIn("Ignore remote drift", labels)
        self.assertIn("Start new run from reopened item", labels)
        message = github_projects_readiness_message(
            "read",
            "schema drift",
            actions=["connect_github_project"],
        )
        self.assertIn("GitHub Projects read readiness degraded", message)

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

    def test_list_pull_requests_accepts_list_tool_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                tool_mock.return_value = {
                    "output": '[{"number":44,"html_url":"https://github.com/acme/demo/pull/44"}]',
                    "metadata": {},
                }
                pulls = list_pull_requests(cfg, "acme", "demo", state="open")

            self.assertEqual(pulls[0]["number"], 44)

    def test_create_pull_request_metadata_falls_back_to_task_repo_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            cfg.repository.slug = ""
            cfg.repository.clone_url = ""
            task = {
                "run_id": "run-123",
                "title": "Task",
                "source": {"type": "manual"},
                "repo": {"path": str(root / "repo")},
            }
            with patch("src.tandem_agents.core.integrations.github_mcp._git_remote_slug", return_value="acme/demo"):
                with patch("src.tandem_agents.core.integrations.github_mcp.list_pull_requests", return_value=[]):
                    with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                        tool_mock.return_value = {
                            "output": '{"id":"3887993462","url":"https://github.com/acme/demo/pull/44"}',
                            "metadata": {},
                        }
                        metadata = create_pull_request_metadata(
                            cfg,
                            task,
                            head_branch="aca/task-123",
                            title="aca: Task",
                            body="PR body",
                        )

            self.assertEqual(metadata["url"], "https://github.com/acme/demo/pull/44")
            self.assertEqual(metadata["number"], 44)
            self.assertEqual(tool_mock.call_args.args[1], "mcp.github.create_pull_request")
            self.assertEqual(tool_mock.call_args.args[2]["owner"], "acme")
            self.assertEqual(tool_mock.call_args.args[2]["repo"], "demo")

    def test_pull_request_lifecycle_state_transitions(self) -> None:
        self.assertEqual(
            normalize_pull_request_metadata(
                {"url": "https://github.com/frumu-ai/example/pull/44"},
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["number"],
            44,
        )
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
        self.assertEqual(
            normalize_pull_request_metadata(
                {
                    "state": "open",
                    "number": 1,
                    "reviewDecision": "REVIEW_REQUIRED",
                    "statusCheckRollup": [
                        {
                            "name": "engine-checks (ubuntu-latest)",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "detailsUrl": "https://github.com/frumu-ai/tandem/actions/runs/1/job/2",
                        },
                        {"name": "Lint Frontend", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    ],
                },
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["lifecycle_state"],
            "needs-repair",
        )
        self.assertEqual(
            normalize_pull_request_metadata(
                {
                    "state": "open",
                    "number": 1,
                    "reviewDecision": "REVIEW_REQUIRED",
                    "statusCheckRollup": [
                        {"name": "engine-checks (ubuntu-latest)", "status": "IN_PROGRESS", "conclusion": None},
                        {"name": "Lint Frontend", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    ],
                },
                head_branch="aca/task",
                base_repo="frumu-ai/example",
            )["lifecycle_state"],
            "running",
        )

    def test_collect_pull_request_repair_context_skips_stale_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            pull_request = {
                "number": 7,
                "url": "https://github.com/frumu-ai/example/pull/7",
                "head_branch": "aca/task-123",
                "base_branch": "main",
                "base_repo": "frumu-ai/example",
            }

            with (
                patch(
                    "src.tandem_agents.core.integrations.github_mcp.get_pull_request",
                    return_value={
                        "number": 7,
                        "state": "open",
                        "reviewDecision": "CHANGES_REQUESTED",
                        "checks_status": "success",
                        "html_url": "https://github.com/frumu-ai/example/pull/7",
                    },
                ),
                patch(
                    "src.tandem_agents.core.integrations.github_mcp._list_pull_request_reviews",
                    return_value=[
                        {
                            "state": "CHANGES_REQUESTED",
                            "body": "Please tighten the validation path.",
                            "user": {"login": "reviewer"},
                        }
                    ],
                ),
                patch(
                    "src.tandem_agents.core.integrations.github_mcp._list_pull_request_review_comments",
                    return_value=[
                        {
                            "body": "Fix this boundary case.",
                            "path": "src/app.py",
                            "line": 12,
                            "user": {"login": "reviewer"},
                            "html_url": "https://github.com/frumu-ai/example/pull/7#discussion_r1",
                        },
                        {
                            "body": "Old comment",
                            "path": "src/old.py",
                            "isResolved": True,
                        },
                    ],
                ),
            ):
                context = collect_pull_request_repair_context(cfg, pull_request)

            self.assertTrue(context["actionable"])
            self.assertEqual([item["kind"] for item in context["feedback_items"]], ["requested_changes", "review_comment"])
            self.assertEqual(context["feedback_items"][1]["path"], "src/app.py")
            self.assertNotIn("Old comment", build_pull_request_repair_prompt(context))

    def test_collect_pull_request_repair_context_no_action_for_clean_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            pull_request = {
                "number": 7,
                "url": "https://github.com/frumu-ai/example/pull/7",
                "head_branch": "aca/task-123",
                "base_branch": "main",
                "base_repo": "frumu-ai/example",
            }

            with (
                patch(
                    "src.tandem_agents.core.integrations.github_mcp.get_pull_request",
                    return_value={
                        "number": 7,
                        "state": "open",
                        "reviewDecision": "APPROVED",
                        "checks_status": "success",
                    },
                ),
                patch("src.tandem_agents.core.integrations.github_mcp._list_pull_request_reviews", return_value=[]),
                patch("src.tandem_agents.core.integrations.github_mcp._list_pull_request_review_comments", return_value=[]),
            ):
                context = collect_pull_request_repair_context(cfg, pull_request)

            self.assertFalse(context["actionable"])
            self.assertEqual(context["reason"], "no_actionable_review_feedback")

    def test_collect_pull_request_repair_context_includes_failed_ci_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            pull_request = {
                "number": 1600,
                "url": "https://github.com/frumu-ai/tandem/pull/1600",
                "head_branch": "aca/tan-106",
                "base_branch": "main",
                "base_repo": "frumu-ai/tandem",
            }

            with (
                patch(
                    "src.tandem_agents.core.integrations.github_mcp.get_pull_request",
                    return_value={
                        "number": 1600,
                        "state": "open",
                        "reviewDecision": "REVIEW_REQUIRED",
                        "statusCheckRollup": [
                            {
                                "name": "engine-checks (ubuntu-latest)",
                                "workflowName": "Engine CI",
                                "status": "COMPLETED",
                                "conclusion": "FAILURE",
                                "detailsUrl": "https://github.com/frumu-ai/tandem/actions/runs/27522819773/job/81344132013",
                            },
                            {
                                "name": "Lint Frontend",
                                "workflowName": "CI",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                            },
                        ],
                    },
                ),
                patch("src.tandem_agents.core.integrations.github_mcp._list_pull_request_reviews", return_value=[]),
                patch("src.tandem_agents.core.integrations.github_mcp._list_pull_request_review_comments", return_value=[]),
            ):
                context = collect_pull_request_repair_context(cfg, pull_request)

            self.assertTrue(context["actionable"])
            self.assertEqual(context["pull_request"]["checks_state"], "failure")
            self.assertEqual(context["pull_request"]["lifecycle_state"], "needs-repair")
            self.assertEqual(context["feedback_items"][0]["kind"], "check_failure")
            self.assertEqual(context["feedback_items"][0]["name"], "engine-checks (ubuntu-latest)")
            self.assertEqual(context["feedback_items"][0]["workflow"], "Engine CI")
            self.assertIn("81344132013", context["feedback_items"][0]["url"])
            prompt = build_pull_request_repair_prompt(context)
            self.assertIn("Check: engine-checks (ubuntu-latest)", prompt)
            self.assertIn("Workflow: Engine CI", prompt)
            self.assertIn("State: failure", prompt)

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

    def test_refresh_pull_request_lifecycle_falls_back_to_gh_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            pull_request = {
                "number": 53,
                "head_branch": "aca/task",
                "base_branch": "main",
                "base_repo": "frumu-ai/example",
            }
            gh_payload = {
                "number": 53,
                "url": "https://github.com/frumu-ai/example/pull/53",
                "headRefName": "aca/task",
                "baseRefName": "main",
                "state": "OPEN",
                "isDraft": False,
                "reviewDecision": "REVIEW_REQUIRED",
                "statusCheckRollup": [],
            }
            completed = subprocess.CompletedProcess(
                args=["gh"],
                returncode=0,
                stdout=json.dumps(gh_payload),
                stderr="",
            )
            with (
                patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool", return_value={"output": "unknown tool"}),
                patch("src.tandem_agents.core.integrations.github_mcp.subprocess.run", return_value=completed) as run,
            ):
                refreshed = refresh_pull_request_lifecycle(cfg, pull_request)

            self.assertEqual(refreshed["url"], "https://github.com/frumu-ai/example/pull/53")
            self.assertEqual(refreshed["base_repo"], "frumu-ai/example")
            self.assertEqual(refreshed["head_branch"], "aca/task")
            self.assertEqual(refreshed["review_state"], "review_required")
            self.assertEqual(refreshed["checks_state"], "unknown")
            self.assertEqual(refreshed["lifecycle_state"], "waiting-for-review")
            self.assertFalse(refreshed["terminal"])
            self.assertIn("pr", run.call_args.args[0])

    def test_refresh_pull_request_lifecycle_falls_back_to_github_graphql(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "number": 53,
                                    "url": "https://github.com/frumu-ai/example/pull/53",
                                    "headRefName": "aca/task",
                                    "baseRefName": "main",
                                    "state": "OPEN",
                                    "isDraft": False,
                                    "merged": False,
                                    "reviewDecision": "APPROVED",
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "nodes": [
                                                {
                                                    "__typename": "CheckRun",
                                                    "name": "Test Rust",
                                                    "status": "COMPLETED",
                                                    "conclusion": "FAILURE",
                                                    "detailsUrl": "https://github.com/frumu-ai/example/actions/runs/1",
                                                    "workflowName": "CI",
                                                }
                                            ]
                                        }
                                    },
                                }
                            }
                        }
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            cfg.env["GITHUB_TOKEN"] = "token"
            pull_request = {
                "number": 53,
                "head_branch": "aca/task",
                "base_branch": "main",
                "base_repo": "frumu-ai/example",
            }
            with (
                patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool", return_value={"output": "unknown tool"}),
                patch("src.tandem_agents.core.integrations.github_mcp.urlopen", return_value=_Response()) as api,
                patch("src.tandem_agents.core.integrations.github_mcp.subprocess.run") as run,
            ):
                refreshed = refresh_pull_request_lifecycle(cfg, pull_request)

            self.assertEqual(refreshed["url"], "https://github.com/frumu-ai/example/pull/53")
            self.assertEqual(refreshed["review_state"], "approved")
            self.assertEqual(refreshed["checks_state"], "failure")
            self.assertEqual(refreshed["lifecycle_state"], "needs-repair")
            self.assertFalse(refreshed["terminal"])
            api.assert_called_once()
            run.assert_not_called()

    def test_refresh_pull_request_lifecycle_tries_token_file_after_bad_env_token(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "number": 53,
                                    "url": "https://github.com/frumu-ai/example/pull/53",
                                    "headRefName": "aca/task",
                                    "baseRefName": "main",
                                    "state": "OPEN",
                                    "isDraft": False,
                                    "merged": False,
                                    "reviewDecision": "REVIEW_REQUIRED",
                                    "statusCheckRollup": {"contexts": {"nodes": []}},
                                }
                            }
                        }
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            token_file = root / "secrets" / "github_token"
            token_file.parent.mkdir(parents=True)
            token_file.write_text("good-token\n", encoding="utf-8")
            cfg.env["GITHUB_TOKEN"] = "bad-token"
            cfg.env["GITHUB_TOKEN_FILE"] = "secrets/github_token"
            pull_request = {
                "number": 53,
                "head_branch": "aca/task",
                "base_branch": "main",
                "base_repo": "frumu-ai/example",
            }
            bad_credentials = HTTPError(
                "https://api.github.com/graphql",
                401,
                "Unauthorized",
                hdrs=None,
                fp=None,
            )
            with (
                patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool", return_value={"output": "unknown tool"}),
                patch("src.tandem_agents.core.integrations.github_mcp.urlopen", side_effect=[bad_credentials, _Response()]) as api,
                patch("src.tandem_agents.core.integrations.github_mcp.subprocess.run") as run,
            ):
                refreshed = refresh_pull_request_lifecycle(cfg, pull_request)

            self.assertEqual(refreshed["url"], "https://github.com/frumu-ai/example/pull/53")
            self.assertEqual(refreshed["lifecycle_state"], "waiting-for-review")
            self.assertEqual(api.call_count, 2)
            run.assert_not_called()

    def test_auto_merge_denies_when_policy_is_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), review_policy="human_review")
            gates = evaluate_auto_merge_gates(
                cfg,
                {
                    "number": 7,
                    "head_branch": "aca/task-123",
                    "base_repo": "frumu-ai/example",
                    "review_state": "approved",
                    "checks_state": "success",
                    "lifecycle_state": "ready-to-merge",
                },
            )

            self.assertFalse(gates["allowed"])
            self.assertTrue(any("not auto_merge" in item for item in gates["denials"]))

    def test_auto_merge_waits_for_merge_approval_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root, review_policy="auto_merge")
            cfg.review.merge_requires_approval = True
            pull_request = {
                "number": 7,
                "head_branch": "aca/task-123",
                "base_repo": "frumu-ai/example",
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                result = guarded_auto_merge(cfg, pull_request)

            self.assertEqual(result["status"], "pending_approval")
            self.assertFalse(result["merged"])
            self.assertEqual(result["pending_approvals"][0]["key"], "merge")
            tool_mock.assert_not_called()

    def test_branch_delete_approval_is_separate_from_merge_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root, review_policy="auto_merge")
            cfg.review.merge_requires_approval = True
            cfg.review.branch_delete_requires_approval = True
            pull_request = {
                "number": 7,
                "head_branch": "aca/task-123",
                "base_repo": "frumu-ai/example",
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                tool_mock.return_value = {"output": '{"merged": true, "sha": "abc123"}'}
                result = guarded_auto_merge(cfg, pull_request, approvals={"merge": "approved"})

            self.assertEqual(result["status"], "merged")
            self.assertTrue(result["merged"])
            self.assertFalse(result["branch_deleted"])
            self.assertEqual(result["pending_approvals"][0]["key"], "branch_delete")
            self.assertEqual(tool_mock.call_count, 1)
            self.assertEqual(tool_mock.call_args.args[1], "mcp.github.merge_pull_request")

    def test_auto_merge_denies_non_aca_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), review_policy="auto_merge")
            gates = evaluate_auto_merge_gates(
                cfg,
                {
                    "number": 7,
                    "head_branch": "feature/task-123",
                    "base_repo": "frumu-ai/example",
                    "review_state": "approved",
                    "checks_state": "success",
                    "lifecycle_state": "ready-to-merge",
                },
            )

            self.assertFalse(gates["allowed"])
            self.assertTrue(any("not ACA-created" in item for item in gates["denials"]))

    def test_auto_merge_denies_unclean_or_unknown_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), review_policy="auto_merge")
            gates = evaluate_auto_merge_gates(
                cfg,
                {
                    "number": 7,
                    "head_branch": "aca/task-123",
                    "base_repo": "frumu-ai/example",
                    "review_state": "changes_requested",
                    "checks_state": "unknown",
                    "lifecycle_state": "waiting-for-review",
                },
            )

            self.assertFalse(gates["allowed"])
            self.assertTrue(any("checks" in item for item in gates["denials"]))
            self.assertTrue(any("review state" in item for item in gates["denials"]))

    def test_guarded_auto_merge_merges_then_deletes_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), review_policy="auto_merge")
            pull_request = {
                "number": 7,
                "head_branch": "aca/task-123",
                "base_repo": "frumu-ai/example",
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                tool_mock.side_effect = [
                    {"output": '{"merged": true, "sha": "abc123"}'},
                    {"output": '{"deleted": true}'},
                ]
                result = guarded_auto_merge(cfg, pull_request)

            self.assertEqual(result["status"], "merged")
            self.assertTrue(result["merged"])
            self.assertTrue(result["branch_deleted"])
            self.assertEqual(tool_mock.call_args_list[0].args[1], "mcp.github.merge_pull_request")
            self.assertEqual(tool_mock.call_args_list[0].args[2]["merge_method"], "squash")
            self.assertEqual(tool_mock.call_args_list[1].args[1], "mcp.github.delete_branch")


if __name__ == "__main__":
    unittest.main()
