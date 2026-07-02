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
              merge_requires_approval: false
              branch_delete_requires_approval: false
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
    def test_active_coder_run_listing_skips_inactive_blackboards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            run_dir = cfg.output_root() / "run-inactive"
            run_dir.mkdir(parents=True, exist_ok=True)
            task = {
                "task_id": "task-inactive",
                "title": "Inactive",
                "source": {"type": "manual", "prompt": "done"},
            }
            repo = {"slug": "acme/demo", "path": str(cfg.root_dir / "repo")}
            status = initial_status(
                "run-inactive",
                task,
                repo,
                {"version": "engine"},
                {"id": "openai", "model": "gpt-5.5"},
                {"enabled": False, "shared_model": False, "max_workers": 1},
                run_dir,
            )
            status["run"]["status"] = "completed"
            status["phase"] = {"name": "handoff", "detail": "done", "role": "manager", "updated_at_ms": 1}
            write_status(run_dir / "status.json", status)
            (run_dir / "blackboard.yaml").write_text(
                "notes:\n"
                "  - This completed run has no coder or pull request lifecycle markers.\n"
                f"  - {'x' * 20000}\n",
                encoding="utf-8",
            )
            _seed_completed_run_with_pr(cfg, store)

            with patch.object(coder_supervisor, "load_blackboard", wraps=coder_supervisor.load_blackboard) as load_blackboard_mock:
                active = coder_supervisor.list_active_coder_runs(cfg)

            self.assertEqual([item["run_id"] for item in active], ["run-pr"])
            loaded_blackboards = {Path(call.args[0]) for call in load_blackboard_mock.call_args_list}
            self.assertNotIn(run_dir / "blackboard.yaml", loaded_blackboards)
            self.assertIn(cfg.output_root() / "run-pr" / "blackboard.yaml", loaded_blackboards)

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

    def test_pr_lifecycle_refresh_failure_remains_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(cfg, store)

            with patch.object(coder_supervisor, "refresh_pull_request_lifecycle", side_effect=RuntimeError("temporary GitHub MCP outage")):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            self.assertFalse(result["terminal"])
            self.assertEqual(result["status"], "waiting-for-review")
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            blackboard = load_blackboard(cfg.output_root() / "run-pr" / "blackboard.yaml")
            self.assertFalse(status["pull_request_lifecycle"]["terminal"])
            self.assertEqual(status["pull_request_lifecycle"]["lifecycle_state"], "waiting-for-review")
            self.assertEqual(blackboard["pull_request_lifecycle"]["lifecycle_state"], "waiting-for-review")

    def test_pr_lifecycle_refresh_recovers_retryable_blocked_readback_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(cfg, store)
            run_dir = cfg.output_root() / "run-pr"
            status = load_status(run_dir / "status.json")
            status["pull_request_lifecycle"]["lifecycle_state"] = "blocked"
            status["pull_request_lifecycle"]["terminal"] = True
            status["pull_request_lifecycle"]["error"] = "Could not read GitHub pull request acme/demo#7 through GitHub MCP."
            write_status(run_dir / "status.json", status)
            blackboard = load_blackboard(run_dir / "blackboard.yaml")
            blackboard["pull_request_lifecycle"] = dict(status["pull_request_lifecycle"])
            save_blackboard(run_dir / "blackboard.yaml", blackboard)

            refreshed = {
                "url": "https://github.com/acme/demo/pull/7",
                "number": 7,
                "head_branch": "aca/run-pr",
                "base_branch": "main",
                "base_repo": "acme/demo",
                "state": "open",
                "draft": False,
                "merged": False,
                "review_state": "review_required",
                "checks_state": "unknown",
                "lifecycle_state": "waiting-for-review",
                "terminal": False,
            }
            with patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed) as refresh:
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            refresh.assert_called_once()
            self.assertEqual(result["status"], "waiting-for-review")
            status = load_status(run_dir / "status.json")
            self.assertFalse(status["pull_request_lifecycle"]["terminal"])
            self.assertNotIn("error", status["pull_request_lifecycle"])

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
            # "dispatched", not "completed": success is only confirmed on the
            # next lifecycle refresh (TAN2-2 no longer claims unverified success).
            self.assertEqual(result["repair"]["status"], "dispatched")
            self.assertEqual(result["repair"]["breaker"]["attempts"], 1)
            create_run.assert_called_once()
            payload = create_run.call_args.args[1]
            self.assertEqual(payload["workflow_mode"], "pr_repair")
            self.assertEqual(payload["github_ref"]["head_branch"], "aca/run-pr")
            self.assertIn("Fix this boundary case", payload["objective"])
            execute_all.assert_called_once()
            # Only the first-attempt "starting" comment — no per-pass finish
            # comment, so a stuck PR no longer spams the issue each tick.
            self.assertEqual(linear_comment.call_count, 1)
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            blackboard = load_blackboard(cfg.output_root() / "run-pr" / "blackboard.yaml")
            self.assertEqual(status["pull_request_repair"]["status"], "dispatched")
            self.assertEqual(blackboard["pull_request_repair"]["context"]["feedback_items"][0]["path"], "src/app.py")

    def test_needs_rebase_updates_branch_without_coder_run(self) -> None:
        # A behind-base PR should get its branch updated (cheap) and NOT spawn
        # a coder run (TAN2-3).
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
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "needs-rebase",
                "terminal": False,
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "update_pull_request_branch", return_value={"updated": True}) as update_branch,
                patch.object(coder_supervisor, "sdk_coder_create_run", return_value={"ok": True}) as create_run,
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            update_branch.assert_called_once()
            create_run.assert_not_called()
            self.assertEqual(result["rebase"]["updated"], True)
            self.assertEqual(result["status"], "needs-rebase")

    def test_conflicted_routes_through_repair_pass(self) -> None:
        # A conflicted PR goes through the repair machinery (agent attempts
        # resolution under the circuit breaker) (TAN2-3).
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(
                cfg, store,
                source={"type": "linear", "issue_id": "lin-1", "identifier": "TAN-120", "team": "Tandem"},
            )
            refreshed = {
                "url": "https://github.com/acme/demo/pull/7",
                "number": 7,
                "head_branch": "aca/run-pr",
                "base_branch": "main",
                "base_repo": "acme/demo",
                "state": "open",
                "review_state": "approved",
                "checks_state": "success",
                "lifecycle_state": "conflicted",
                "terminal": False,
            }
            repair_context = {
                "actionable": True,
                "pull_request": refreshed,
                "feedback_items": [{"kind": "review_comment", "body": "resolve conflicts", "path": "a.py", "line": 1}],
                "truncated": False,
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "collect_pull_request_repair_context", return_value=repair_context),
                patch.object(coder_supervisor, "sdk_coder_create_run", return_value={"ok": True}) as create_run,
                patch.object(coder_supervisor, "sdk_coder_execute_all", return_value={}),
                patch.object(coder_supervisor, "linear_add_comment", return_value=None),
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            create_run.assert_called_once()
            self.assertEqual(result["repair"]["status"], "dispatched")

    def test_needs_repair_does_not_redispatch_once_escalated(self) -> None:
        # Once the circuit breaker has escalated a PR, a subsequent reconcile
        # tick that still sees "needs-repair" must NOT spend tokens on another
        # coder run — this is the core unbounded-spend fix (TAN2-2).
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(
                cfg,
                store,
                source={"type": "linear", "issue_id": "lin-1", "identifier": "TAN-120", "team": "Tandem"},
            )
            # Pre-seed an already-escalated breaker state in run metadata.
            run = store.get_run("run-pr") or {}
            metadata = dict(run.get("metadata") or {})
            metadata["pull_request_repair_state"] = {
                "attempts": 5,
                "last_attempt_ms": 1,
                "last_signature": "old",
                "escalated": True,
                "reason": "max_attempts",
            }
            store.update_run("run-pr", metadata=metadata)

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
                "feedback_items": [{"kind": "review_comment", "body": "still broken", "path": "a.py", "line": 1}],
                "truncated": False,
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "collect_pull_request_repair_context", return_value=repair_context),
                patch.object(coder_supervisor, "sdk_coder_create_run", return_value={"ok": True}) as create_run,
                patch.object(coder_supervisor, "sdk_coder_execute_all", return_value={}) as execute_all,
                patch.object(coder_supervisor, "linear_add_comment", return_value=None) as linear_comment,
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            create_run.assert_not_called()
            execute_all.assert_not_called()
            linear_comment.assert_not_called()  # already escalated → stay silent
            self.assertEqual(result["repair"]["status"], "escalated")
            # Lifecycle is pinned to needs-human so the operator sees the handoff.
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            self.assertEqual(status["pull_request_lifecycle"]["lifecycle_state"], "needs-human")

    def test_needs_repair_escalates_when_issue_budget_exhausted(self) -> None:
        # A PR still in needs-repair whose issue has blown its per-issue budget
        # must escalate instead of dispatching another coder run (TAN2-1),
        # regardless of how many repair attempts remain.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            # Force a tiny execution budget so one prior pass exhausts it.
            object.__setattr__(cfg.budget, "max_coder_executions", 1)
            store = CoordinationStore.from_config(cfg)
            _seed_completed_run_with_pr(
                cfg,
                store,
                source={"type": "linear", "issue_id": "lin-1", "identifier": "TAN-120", "team": "Tandem"},
            )
            run = store.get_run("run-pr") or {}
            metadata = dict(run.get("metadata") or {})
            metadata["issue_spend"] = {"total_tokens": 0, "cost_usd": 0.0, "coder_executions": 1}
            store.update_run("run-pr", metadata=metadata)

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
                "feedback_items": [{"kind": "review_comment", "body": "fix", "path": "a.py", "line": 1}],
                "truncated": False,
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "collect_pull_request_repair_context", return_value=repair_context),
                patch.object(coder_supervisor, "sdk_coder_create_run", return_value={"ok": True}) as create_run,
                patch.object(coder_supervisor, "sdk_coder_execute_all", return_value={}) as execute_all,
                patch.object(coder_supervisor, "linear_add_comment", return_value=None) as linear_comment,
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            create_run.assert_not_called()
            execute_all.assert_not_called()
            self.assertEqual(result["repair"]["status"], "escalated")
            self.assertIn("budget_exhausted", result["repair"]["summary"])
            # The escalation comment names the budget reason.
            self.assertTrue(linear_comment.called)
            self.assertIn("budget_exhausted", linear_comment.call_args.args[2])

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

            auto_merge.assert_called_once_with(cfg, refreshed, approvals={})
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

    def test_ready_to_merge_waits_for_merge_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp), review_policy="auto_merge")
            cfg.review.merge_requires_approval = True
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
                "status": "pending_approval",
                "merged": False,
                "branch_deleted": False,
                "strategy": "squash",
                "pull_request": refreshed,
                "pending_approvals": [{"action": "merge_pull_request", "key": "merge"}],
                "denials": [],
            }
            with (
                patch.object(coder_supervisor, "refresh_pull_request_lifecycle", return_value=refreshed),
                patch.object(coder_supervisor, "guarded_auto_merge", return_value=merge),
            ):
                result = coder_supervisor.reconcile_coder_run(cfg, "run-pr", coordination=store)

            self.assertEqual(result["status"], "ready-to-merge")
            self.assertEqual(result["merge"]["status"], "pending_approval")
            status = load_status(cfg.output_root() / "run-pr" / "status.json")
            self.assertEqual(status["pull_request_merge"]["pending_approvals"][0]["key"], "merge")


class RepairCircuitBreakerTest(unittest.TestCase):
    COOLDOWN = 60_000

    def _decide(self, state, signature, now, *, max_attempts=3):
        return coder_supervisor._repair_gate_decision(
            state,
            signature,
            now,
            max_attempts=max_attempts,
            cooldown_base_ms=self.COOLDOWN,
        )

    def test_first_attempt_proceeds_and_records_state(self) -> None:
        decision, state, _ = self._decide({}, "sig-a", 1_000)
        self.assertEqual(decision, "proceed")
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["last_attempt_ms"], 1_000)
        self.assertEqual(state["last_signature"], "sig-a")

    def test_defers_within_cooldown_window(self) -> None:
        prior = {"attempts": 1, "last_attempt_ms": 1_000, "last_signature": "sig-a"}
        # New feedback, but only 30s later — under the 60s base cooldown.
        decision, state, reason = self._decide(prior, "sig-b", 1_000 + 30_000)
        self.assertEqual(decision, "defer")
        self.assertEqual(reason, "cooldown")
        self.assertEqual(state["attempts"], 1)  # not incremented

    def test_proceeds_after_cooldown_with_new_feedback(self) -> None:
        prior = {"attempts": 1, "last_attempt_ms": 1_000, "last_signature": "sig-a"}
        decision, state, _ = self._decide(prior, "sig-b", 1_000 + 61_000)
        self.assertEqual(decision, "proceed")
        self.assertEqual(state["attempts"], 2)

    def test_escalates_on_unchanged_feedback_after_cooldown(self) -> None:
        prior = {"attempts": 1, "last_attempt_ms": 1_000, "last_signature": "sig-a"}
        # Same signature after the pass = the last repair moved nothing.
        decision, state, reason = self._decide(prior, "sig-a", 1_000 + 61_000)
        self.assertEqual(decision, "escalate")
        self.assertEqual(reason, "no_new_feedback")
        self.assertTrue(state["escalated"])

    def test_escalates_when_max_attempts_reached(self) -> None:
        prior = {"attempts": 3, "last_attempt_ms": 1_000, "last_signature": "sig-a"}
        decision, state, reason = self._decide(prior, "sig-z", 10_000_000, max_attempts=3)
        self.assertEqual(decision, "escalate")
        self.assertEqual(reason, "max_attempts")
        self.assertTrue(state["escalated"])

    def test_skips_once_already_escalated(self) -> None:
        prior = {"attempts": 3, "escalated": True, "reason": "max_attempts", "last_attempt_ms": 1_000}
        decision, _, reason = self._decide(prior, "sig-z", 10_000_000)
        self.assertEqual(decision, "skip")
        self.assertEqual(reason, "max_attempts")

    def test_cooldown_grows_exponentially(self) -> None:
        # attempts=2 → cooldown = base * 2**1 = 120s; 90s later still defers.
        prior = {"attempts": 2, "last_attempt_ms": 0, "last_signature": "sig-a"}
        decision, _, _ = self._decide(prior, "sig-b", 90_000, max_attempts=5)
        self.assertEqual(decision, "defer")
        # 121s later it proceeds.
        decision2, _, _ = self._decide(prior, "sig-b", 121_000, max_attempts=5)
        self.assertEqual(decision2, "proceed")

    def test_signature_stable_and_sensitive(self) -> None:
        ctx_a = {
            "pull_request": {"head_branch": "feat"},
            "feedback_items": [
                {"kind": "review_comment", "url": "u1", "body": "fix this"},
                {"kind": "failed_check", "url": "c1", "body": "ci red"},
            ],
        }
        # Same content, different order → same signature.
        ctx_a_reordered = {
            "pull_request": {"head_branch": "feat"},
            "feedback_items": list(reversed(ctx_a["feedback_items"])),
        }
        self.assertEqual(
            coder_supervisor._repair_signature(ctx_a),
            coder_supervisor._repair_signature(ctx_a_reordered),
        )
        # Changed body → different signature.
        ctx_b = {
            "pull_request": {"head_branch": "feat"},
            "feedback_items": [
                {"kind": "review_comment", "url": "u1", "body": "fix this differently"},
                {"kind": "failed_check", "url": "c1", "body": "ci red"},
            ],
        }
        self.assertNotEqual(
            coder_supervisor._repair_signature(ctx_a),
            coder_supervisor._repair_signature(ctx_b),
        )


if __name__ == "__main__":
    unittest.main()
