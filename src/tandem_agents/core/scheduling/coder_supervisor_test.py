from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.scheduling import coder_supervisor
from src.tandem_agents.runtime.runstate import initial_blackboard, initial_status, load_blackboard, load_status, save_blackboard, write_status


def _config(root: Path, *, review_policy: str = "human_review"):
    (root / "tandem-data").mkdir(parents=True, exist_ok=True)
    (root / "agent.yaml").write_text(
        dedent(
            f"""
            agent:
              name: ACA
            tandem:
              base_url: http://127.0.0.1:39733
            task_source:
              type: manual
              prompt: supervise coder
            repository:
              slug: acme/demo
            provider:
              id: openai
              model: gpt-5.5
            github_mcp:
              scope: none
              remote_sync: off
            linear_mcp:
              enabled: true
              scope: intake_finalize
              remote_sync: rich
            review:
              policy: {review_policy}
              auto_merge_strategy: squash
              auto_merge_allowed_strategies: squash
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


def _seed_active_coder_run(cfg, store: CoordinationStore, *, run_id: str = "run-1", source: dict | None = None) -> None:
    task = {
        "task_id": "task-1",
        "title": "Fix cache invalidation",
        "source": source or {"type": "manual", "prompt": "supervise coder"},
        "repo": {"slug": "acme/demo", "path": str(cfg.root_dir / "repo")},
    }
    repo = {"slug": "acme/demo", "path": str(cfg.root_dir / "repo"), "branch": "aca/run-1"}
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
        {"id": "openai", "model": "gpt-5.5"},
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
    blackboard = initial_blackboard(run_id, task, repo, {"id": "openai", "model": "gpt-5.5"}, {"version": "engine"}, {})
    blackboard["execution_backend"] = "coder"
    blackboard["coder_run"] = {"coder_run_id": run_id, "status": "running", "phase": "coding"}
    save_blackboard(run_dir / "blackboard.yaml", blackboard)


def _seed_completed_run_with_pr(cfg, store: CoordinationStore, *, run_id: str = "run-pr", source: dict | None = None) -> None:
    _seed_active_coder_run(cfg, store, run_id=run_id, source=source)
    run_dir = cfg.output_root() / run_id
    status = load_status(run_dir / "status.json")
    status["run"]["status"] = "completed"
    status["phase"] = {"name": "handoff", "detail": "task completed", "role": "manager", "updated_at_ms": 1}
    status["pull_request"] = "https://github.com/acme/demo/pull/7"
    status["pull_request_lifecycle"] = {
        "url": "https://github.com/acme/demo/pull/7",
        "number": 7,
        "head_branch": "aca/run-pr",
        "base_branch": "main",
        "base_repo": "acme/demo",
        "lifecycle_state": "waiting-for-review",
        "terminal": False,
    }
    write_status(run_dir / "status.json", status)
    blackboard = load_blackboard(run_dir / "blackboard.yaml")
    blackboard["pull_request"] = "https://github.com/acme/demo/pull/7"
    blackboard["pull_request_lifecycle"] = dict(status["pull_request_lifecycle"])
    save_blackboard(run_dir / "blackboard.yaml", blackboard)
    run = store.get_run(run_id) or {}
    metadata = dict(run.get("metadata") or {})
    metadata["pull_request"] = "https://github.com/acme/demo/pull/7"
    metadata["pull_request_lifecycle"] = dict(status["pull_request_lifecycle"])
    store.update_run(run_id, status="completed", phase="handoff", metadata=metadata, completed=True)


class CoderSupervisorTest(unittest.TestCase):
    def test_non_terminal_run_stays_running_and_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_active_coder_run(cfg, store)

            with patch.object(
                coder_supervisor,
                "sdk_coder_get_run",
                return_value={
                    "coder_run": {"coder_run_id": "run-1", "status": "running", "phase": "coding"},
                    "run": {"status": "running", "phase": "coding"},
                },
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-1", coordination=store)

            self.assertFalse(result["terminal"])
            status = load_status(cfg.output_root() / "run-1" / "status.json")
            blackboard = load_blackboard(cfg.output_root() / "run-1" / "blackboard.yaml")
            self.assertEqual(status["run"]["status"], "running")
            self.assertFalse(status["blocker"]["active"])
            self.assertEqual(blackboard["coder_supervision"]["tandem_status"], "running")
            self.assertEqual((store.get_run("run-1") or {})["status"], "running")

    def test_completed_run_finalizes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_active_coder_run(cfg, store)

            coder_result = {
                "coder_run": {"coder_run_id": "run-1", "status": "completed", "phase": "handoff"},
                "run": {"status": "completed", "phase": "handoff"},
                "status": "completed",
                "phase": "handoff",
                "artifacts": [],
            }
            first = coder_supervisor.apply_coder_result(cfg, store, run_id="run-1", coder_result=coder_result)
            second = coder_supervisor.apply_coder_result(cfg, store, run_id="run-1", coder_result=coder_result)

            self.assertTrue(first["terminal"])
            self.assertTrue(second["terminal"])
            status = load_status(cfg.output_root() / "run-1" / "status.json")
            self.assertEqual(status["run"]["status"], "completed")
            self.assertEqual((store.get_run("run-1") or {})["status"], "completed")

    def test_linear_completed_coder_run_enqueues_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_active_coder_run(
                cfg,
                store,
                source={
                    "type": "linear",
                    "issue_id": "lin-122",
                    "identifier": "TAN-122",
                    "team": "Tandem",
                },
            )

            coder_result = {
                "coder_run": {"coder_run_id": "run-1", "status": "completed", "phase": "handoff"},
                "run": {"status": "completed", "phase": "handoff"},
                "status": "completed",
                "phase": "handoff",
                "artifacts": [],
            }
            result = coder_supervisor.apply_coder_result(cfg, store, run_id="run-1", coder_result=coder_result)

            self.assertTrue(result["terminal"])
            rows = store.list_pending_outbox()
            self.assertEqual([row["kind"] for row in rows], ["linear_issue.status_update"])
            payload = rows[0]["payload"]
            self.assertEqual(payload["target_status"], "In Review")
            self.assertEqual(payload["labels"], [])
            self.assertEqual(payload["task"]["source"]["identifier"], "TAN-122")

    def test_linear_blocked_coder_run_enqueues_blocked_status_and_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_active_coder_run(
                cfg,
                store,
                source={
                    "type": "linear",
                    "issue_id": "lin-122",
                    "identifier": "TAN-122",
                    "team": "Tandem",
                },
            )

            coder_result = {
                "coder_run": {"coder_run_id": "run-1", "status": "blocked", "phase": "coding"},
                "run": {"status": "blocked", "phase": "coding", "last_error": "tests failed"},
                "status": "blocked",
                "phase": "coding",
                "last_error": "tests failed",
                "artifacts": [],
            }
            result = coder_supervisor.apply_coder_result(cfg, store, run_id="run-1", coder_result=coder_result)

            self.assertEqual(result["status"], "blocked")
            rows = store.list_pending_outbox()
            self.assertEqual([row["kind"] for row in rows], ["linear_issue.status_update", "linear_issue.comment"])
            self.assertEqual(rows[0]["payload"]["target_status"], "Blocked")
            self.assertEqual(rows[0]["payload"]["labels"], ["aca-blocked"])
            self.assertIn("tests failed", rows[1]["payload"]["body"])
            self.assertIn("Next expected action", rows[1]["payload"]["body"])

    def test_poll_error_does_not_block_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_active_coder_run(cfg, store)

            with patch.object(coder_supervisor, "sdk_coder_get_run", side_effect=RuntimeError("temporary outage")):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-1", coordination=store)

            self.assertEqual(result["status"], "running")
            status = load_status(cfg.output_root() / "run-1" / "status.json")
            self.assertEqual(status["run"]["status"], "running")
            self.assertFalse(status["blocker"]["active"])
            self.assertIn("temporary outage", load_blackboard(cfg.output_root() / "run-1" / "blackboard.yaml")["coder_supervision"]["last_error"])

    def test_cancel_reconciles_through_tandem_cancel_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_active_coder_run(cfg, store)

            with (
                patch.object(coder_supervisor, "sdk_coder_cancel_run", return_value={"ok": True}) as cancel_run,
                patch.object(
                    coder_supervisor,
                    "sdk_coder_get_run",
                    return_value={
                        "coder_run": {"coder_run_id": "run-1", "status": "cancelled", "phase": "cancelled"},
                        "run": {"status": "cancelled", "phase": "cancelled", "last_error": "operator stop"},
                    },
                ),
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-1", coordination=store, cancel_reason="stop it")

            self.assertTrue(result["terminal"])
            cancel_run.assert_called_once()
            status = load_status(cfg.output_root() / "run-1" / "status.json")
            self.assertEqual(status["run"]["status"], "cancelled")

    def test_completed_run_refreshes_pull_request_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(cfg, store)

            refreshed = {
                "url": "https://github.com/acme/demo/pull/7",
                "number": 7,
                "head_branch": "aca/run-pr",
                "base_branch": "main",
                "base_repo": "acme/demo",
                "state": "open",
                "draft": False,
                "merged": False,
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            with patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed) as refresh:
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            refresh.assert_called_once()
            self.assertEqual(result["status"], "ready-to-merge")
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            blackboard = load_blackboard(cfg.output_root() / "run-pr" / "blackboard.yaml")
            run_meta = (store.get_run("run-pr") or {}).get("metadata") or {}
            self.assertEqual(status["pull_request_lifecycle"]["lifecycle_state"], "ready-to-merge")
            self.assertEqual(blackboard["pull_request_lifecycle"]["lifecycle_state"], "ready-to-merge")
            self.assertEqual(run_meta["pull_request_lifecycle"]["lifecycle_state"], "ready-to-merge")

    def test_needs_repair_starts_pr_repair_pass_and_comments_linear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(
                cfg,
                store,
                source={
                    "type": "linear",
                    "issue_id": "lin-1",
                    "identifier": "TAN-120",
                    "team": "Tandem",
                },
            )

            refreshed = {
                "url": "https://github.com/acme/demo/pull/7",
                "number": 7,
                "head_branch": "aca/run-pr",
                "base_branch": "main",
                "base_repo": "acme/demo",
                "state": "open",
                "review_state": "changes_requested",
                "checks_state": "success",
                "lifecycle_state": "needs-repair",
                "terminal": False,
            }
            repair_context = {
                "actionable": True,
                "pull_request": refreshed,
                "feedback_items": [
                    {
                        "kind": "review_comment",
                        "body": "Fix this boundary case.",
                        "path": "src/app.py",
                        "line": 12,
                    }
                ],
                "truncated": False,
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "collect_pull_request_repair_context", return_value=repair_context),
                patch.object(coder_supervisor, "sdk_coder_create_run", return_value={"ok": True}) as create_run,
                patch.object(coder_supervisor, "sdk_coder_execute_all", return_value={"run": {"status": "completed"}}) as execute_all,
                patch.object(coder_supervisor, "linear_add_comment", return_value=None) as linear_comment,
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            self.assertEqual(result["status"], "needs-repair")
            self.assertEqual(result["repair"]["status"], "completed")
            create_run.assert_called_once()
            payload = create_run.call_args.args[1]
            self.assertEqual(payload["workflow_mode"], "pr_repair")
            self.assertEqual(payload["github_ref"]["head_branch"], "aca/run-pr")
            self.assertIn("Fix this boundary case", payload["objective"])
            execute_all.assert_called_once()
            self.assertEqual(linear_comment.call_count, 2)
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            blackboard = load_blackboard(cfg.output_root() / "run-pr" / "blackboard.yaml")
            self.assertEqual(status["pull_request_repair"]["status"], "completed")
            self.assertEqual(blackboard["pull_request_repair"]["context"]["feedback_items"][0]["path"], "src/app.py")

    def test_ready_to_merge_auto_merge_persists_merge_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp), review_policy="auto_merge")
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(cfg, store)

            refreshed = {
                "url": "https://github.com/acme/demo/pull/7",
                "number": 7,
                "head_branch": "aca/run-pr",
                "base_branch": "main",
                "base_repo": "acme/demo",
                "state": "open",
                "draft": False,
                "merged": False,
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            merge = {
                "status": "merged",
                "merged": True,
                "branch_deleted": True,
                "strategy": "squash",
                "pull_request": refreshed,
                "merge_result": {"merged": True},
                "delete_result": {"deleted": True},
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "guarded_auto_merge", return_value=merge) as auto_merge,
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            auto_merge.assert_called_once_with(cfg, refreshed)
            self.assertEqual(result["status"], "merged")
            self.assertTrue(result["terminal"])
            self.assertEqual(result["merge"]["status"], "merged")
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            blackboard = load_blackboard(cfg.output_root() / "run-pr" / "blackboard.yaml")
            run_meta = (store.get_run("run-pr") or {}).get("metadata") or {}
            self.assertEqual(status["pull_request_lifecycle"]["lifecycle_state"], "merged")
            self.assertTrue(blackboard["pull_request_lifecycle"]["branch_deleted"])
            self.assertEqual(run_meta["pull_request_merge"]["strategy"], "squash")

    def test_linear_ready_to_merge_auto_merge_enqueues_done_status_and_merge_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp), review_policy="auto_merge")
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(
                cfg,
                store,
                source={
                    "type": "linear",
                    "issue_id": "lin-122",
                    "identifier": "TAN-122",
                    "team": "Tandem",
                },
            )

            refreshed = {
                "url": "https://github.com/acme/demo/pull/7",
                "number": 7,
                "head_branch": "aca/run-pr",
                "base_branch": "main",
                "base_repo": "acme/demo",
                "state": "open",
                "draft": False,
                "merged": False,
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "ready-to-merge",
                "terminal": False,
            }
            merge = {
                "status": "merged",
                "merged": True,
                "branch_deleted": True,
                "strategy": "squash",
                "pull_request": refreshed,
                "merge_result": {"merged": True},
                "delete_result": {"deleted": True},
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "guarded_auto_merge", return_value=merge),
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            self.assertEqual(result["status"], "merged")
            rows = store.list_pending_outbox()
            self.assertEqual([row["kind"] for row in rows], ["linear_issue.status_update", "linear_issue.comment"])
            self.assertEqual(rows[0]["payload"]["target_status"], "Done")
            self.assertEqual(rows[0]["payload"]["labels"], ["aca-done"])
            self.assertEqual(rows[0]["payload"]["merge"]["strategy"], "squash")
            self.assertIn("https://github.com/acme/demo/pull/7", rows[1]["payload"]["body"])
            self.assertIn("Remote branch deleted: `yes`", rows[1]["payload"]["body"])


if __name__ == "__main__":
    unittest.main()
