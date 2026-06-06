from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.integrations.github_mcp import create_pull_request_metadata, guarded_auto_merge
from src.tandem_agents.core.scheduling import coder_supervisor
from src.tandem_agents.runtime.runstate import (
    initial_blackboard,
    initial_status,
    load_blackboard,
    load_status,
    save_blackboard,
    write_status,
)


def _config(root: Path):
    (root / "tandem-data").mkdir(parents=True, exist_ok=True)
    (root / "agent.yaml").write_text(
        dedent(
            """
            agent:
              name: ACA
            tandem:
              base_url: http://127.0.0.1:39733
            task_source:
              type: linear
              team: Tandem
              project: Tandem Coder Runtime & Intake
              item: TAN-SMOKE
            repository:
              slug: acme/demo
              default_branch: main
            provider:
              id: openai
              model: gpt-4.1-mini
            github_mcp:
              enabled: true
              scope: always
              remote_sync: status_comment
            linear_mcp:
              enabled: true
              scope: intake_finalize
              remote_sync: rich
            review:
              policy: auto_merge
              auto_merge_strategy: squash
              auto_merge_allowed_strategies: squash
              merge_requires_approval: true
              branch_delete_requires_approval: true
              delete_branch_after_merge: true
            output:
              root: runs
            coordination:
              sqlite_path: tandem-data/coordination.sqlite3
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return resolve_config(root)


def _seed_active_linear_run(cfg, store: CoordinationStore, *, run_id: str = "run-linear-smoke") -> dict:
    task = {
        "task_id": "task-linear-smoke",
        "run_id": run_id,
        "title": "Linear smoke task",
        "source": {
            "type": "linear",
            "issue_id": "lin-smoke",
            "identifier": "TAN-SMOKE",
            "team": "Tandem",
            "owner": "acme",
            "repo_name": "demo",
        },
        "repo": {"slug": "acme/demo", "path": str(cfg.root_dir / "repo")},
    }
    repo = {"slug": "acme/demo", "path": str(cfg.root_dir / "repo"), "branch": f"aca/{run_id}"}
    store.register_task(task, repo=repo)
    claim = store.claim_task(
        task,
        run_id=run_id,
        worker_id="worker-1",
        host_id="host-1",
        lease_ttl_seconds=60,
        repo=repo,
    )
    run_dir = cfg.output_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    status = initial_status(
        run_id,
        {**task, "task_key": claim["task"]["task_key"]},
        repo,
        {"version": "engine"},
        {"id": "openai", "model": "gpt-4.1-mini"},
        {"enabled": False, "shared_model": False, "max_workers": 1},
        run_dir,
    )
    status["run"]["status"] = "running"
    status["phase"] = {"name": "coder_execution", "detail": None, "role": "worker", "updated_at_ms": 1}
    status["coordination"] = {
        "worker_id": "worker-1",
        "host_id": "host-1",
        "lease_id": claim["lease"]["lease_id"],
        "task_key": claim["task"]["task_key"],
        "lease_expires_at_ms": claim["lease"]["expires_at_ms"],
    }
    write_status(run_dir / "status.json", status)
    blackboard = initial_blackboard(
        run_id,
        task,
        repo,
        {"id": "openai", "model": "gpt-4.1-mini"},
        {"version": "engine"},
        {},
    )
    blackboard["execution_backend"] = "coder"
    blackboard["coder_run"] = {"coder_run_id": run_id, "status": "running", "phase": "coding"}
    save_blackboard(run_dir / "blackboard.yaml", blackboard)
    return task


def _attach_pull_request(cfg, store: CoordinationStore, *, run_id: str, pull_request: dict) -> None:
    run_dir = cfg.output_root() / run_id
    status = load_status(run_dir / "status.json")
    blackboard = load_blackboard(run_dir / "blackboard.yaml")
    status["run"]["status"] = "completed"
    status["phase"] = {"name": "handoff", "detail": "task completed", "role": "manager", "updated_at_ms": 1}
    status["pull_request"] = pull_request["url"]
    status["pull_request_lifecycle"] = pull_request
    status.setdefault("task", {})["pull_request"] = pull_request["url"]
    status["task"]["pull_request_lifecycle"] = pull_request
    blackboard["pull_request"] = pull_request["url"]
    blackboard["pull_request_lifecycle"] = pull_request
    write_status(run_dir / "status.json", status)
    save_blackboard(run_dir / "blackboard.yaml", blackboard)
    run = store.get_run(run_id) or {}
    metadata = dict(run.get("metadata") or {})
    metadata["pull_request"] = pull_request["url"]
    metadata["pull_request_lifecycle"] = pull_request
    store.update_run(run_id, status="completed", phase="handoff", metadata=metadata, completed=True)


class LinearAcaCodingLoopSmokeTest(unittest.TestCase):
    def test_linear_to_pr_review_repair_approval_merge_and_done_sync_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            task = _seed_active_linear_run(cfg, store)
            run_id = "run-linear-smoke"

            coder_result = {
                "coder_run": {"coder_run_id": run_id, "status": "completed", "phase": "handoff"},
                "run": {"status": "completed", "phase": "handoff"},
                "status": "completed",
                "phase": "handoff",
                "artifacts": [],
            }
            completed = coder_supervisor.apply_coder_result(
                cfg,
                store,
                run_id=run_id,
                coder_result=coder_result,
            )
            self.assertTrue(completed["terminal"])
            self.assertEqual(store.list_pending_outbox()[0]["payload"]["target_status"], "In Review")

            with (
                patch("src.tandem_agents.core.integrations.github_mcp._fetch_pull_requests", return_value=[]),
                patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock,
            ):
                tool_mock.return_value = {
                    "output": (
                        '{"number": 7, "html_url": "https://github.com/acme/demo/pull/7", '
                        '"state": "open", "reviewDecision": "REVIEW_REQUIRED", "checks_status": "pending"}'
                    )
                }
                pull_request = create_pull_request_metadata(
                    cfg,
                    task,
                    head_branch="aca/run-linear-smoke",
                    title="aca: Linear smoke task",
                    body="Smoke PR",
                )
            self.assertEqual(pull_request["url"], "https://github.com/acme/demo/pull/7")
            self.assertEqual(pull_request["lifecycle_state"], "running")
            _attach_pull_request(cfg, store, run_id=run_id, pull_request=pull_request)

            needs_repair = {
                **pull_request,
                "review_state": "changes_requested",
                "checks_state": "success",
                "lifecycle_state": "needs-repair",
                "terminal": False,
            }
            repair_context = {
                "actionable": True,
                "pull_request": needs_repair,
                "feedback_items": [{"kind": "review_comment", "body": "Tighten the smoke assertion."}],
                "truncated": False,
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=needs_repair),
                patch.object(coder_supervisor, "collect_pull_request_repair_context", return_value=repair_context),
                patch.object(coder_supervisor, "sdk_coder_create_run", return_value={"ok": True}),
                patch.object(coder_supervisor, "sdk_coder_execute_all", return_value={"run": {"status": "completed"}}),
                patch.object(coder_supervisor, "linear_add_comment", return_value=None) as linear_comment,
            ):
                repair = coder_supervisor.reconcile_coder_run(cfg, run_id, coordination=store)
            self.assertEqual(repair["status"], "needs-repair")
            self.assertEqual(repair["repair"]["status"], "completed")
            self.assertEqual(linear_comment.call_count, 2)

            ready = {
                **pull_request,
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            with patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=ready):
                pending = coder_supervisor.reconcile_coder_run(cfg, run_id, coordination=store)
            self.assertEqual(pending["merge"]["status"], "pending_approval")
            self.assertEqual(pending["merge"]["pending_approvals"][0]["key"], "merge")

            run_dir = cfg.output_root() / run_id
            blackboard = load_blackboard(run_dir / "blackboard.yaml")
            blackboard["finalization_approvals"] = {"merge": "approved"}
            save_blackboard(run_dir / "blackboard.yaml", blackboard)
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=ready),
                patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock,
            ):
                tool_mock.return_value = {"output": '{"merged": true, "sha": "abc123"}'}
                merged = coder_supervisor.reconcile_coder_run(cfg, run_id, coordination=store)
            self.assertEqual(merged["status"], "merged")
            self.assertTrue(merged["merge"]["merged"])
            self.assertFalse(merged["merge"]["branch_deleted"])
            self.assertEqual(merged["merge"]["pending_approvals"][0]["key"], "branch_delete")
            self.assertTrue(
                any(
                    row["kind"] == "linear_issue.status_update"
                    and row["payload"].get("target_status") == "Done"
                    for row in store.list_pending_outbox()
                )
            )

            with patch("src.tandem_agents.core.integrations.github_mcp.execute_engine_tool") as tool_mock:
                tool_mock.side_effect = [
                    {"output": '{"merged": true, "sha": "def456"}'},
                    {"output": '{"deleted": true}'},
                ]
                cleanup = guarded_auto_merge(
                    cfg,
                    ready,
                    approvals={"merge": "approved", "branch_delete": "approved"},
                )
            self.assertEqual(cleanup["status"], "merged")
            self.assertTrue(cleanup["branch_deleted"])


if __name__ == "__main__":
    unittest.main()
