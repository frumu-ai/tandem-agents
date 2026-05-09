from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from src.aca.config.config_loader import resolve_config
from src.aca.core.coordination.coordination import CoordinationStore
from src.aca.core.scheduling import coder_supervisor
from src.aca.runtime.runstate import initial_blackboard, initial_status, load_blackboard, load_status, save_blackboard, write_status


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


def _seed_active_coder_run(cfg, store: CoordinationStore, *, run_id: str = "run-1") -> None:
    task = {
        "task_id": "task-1",
        "title": "Fix cache invalidation",
        "source": {"type": "manual", "prompt": "supervise coder"},
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


if __name__ == "__main__":
    unittest.main()
