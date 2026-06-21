from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.execution.run_recovery import (
    cleanup_terminal_orphaned_engine_sessions,
    cleanup_terminal_orphaned_engine_sessions_for_run,
    recover_restart_orphaned_run,
)


class RunRecoveryTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "tandem:",
                    "  base_url: http://127.0.0.1:39733",
                    "task_source:",
                    "  type: linear",
                    "  team: team-1",
                    "  project: project-1",
                    "repository:",
                    "  slug: frumu-ai/tandem-agents",
                    "  default_branch: main",
                    "provider:",
                    "  id: openai-codex",
                    "  model: gpt-5.5",
                    "output:",
                    "  root: runs",
                    "coordination:",
                    "  sqlite_path: tandem-data/coordination.sqlite3",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def _write_run(self, root: Path, repo: Path) -> Path:
        run_id = "run-20260621T010101Z-recover"
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "logs").mkdir()
        (run_dir / "artifacts").mkdir()
        (run_dir / "worktrees").mkdir()
        (run_dir / "diffs").mkdir()
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "ACA_SMOKE_HARNESS.md").write_text("# Smoke Harness\n", encoding="utf-8")
        (repo / "docs" / "README.md").write_text("- [Smoke Harness](ACA_SMOKE_HARNESS.md)\n", encoding="utf-8")
        task = {
            "task_id": "TAN-347",
            "title": "Document smoke harness",
            "description": "Verification:\n\n```bash\npython3 -m unittest src.tandem_agents.aca_harness.calculator_test\n```",
            "acceptance_criteria": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            "source": {
                "type": "linear",
                "team": "team-1",
                "project": "project-1",
                "item": "TAN-347",
                "board_path": str(run_dir / "board.yaml"),
            },
            "repo": {
                "path": str(repo),
                "slug": "frumu-ai/tandem-agents",
                "default_branch": "main",
                "remote_name": "origin",
                "branch": "aca/test-recovery",
            },
        }
        status = {
            "run": {"run_id": run_id, "status": "running", "created_at_ms": 1, "updated_at_ms": 2},
            "task": task,
            "repo": task["repo"],
            "engine": {"version": "0.6.1"},
            "provider": {"id": "openai-codex", "model": "gpt-5.5"},
            "swarm": {"max_workers": 1},
            "phase": {"name": "review", "detail": "manager integration"},
            "blocker": {"active": False, "kind": None, "message": None, "owner_role": None},
            "metrics": {"planned_workers": 1, "completed_workers": 1, "failed_workers": 0},
            "coordination": {
                "lease_id": "lease-1",
                "task_key": "linear:team-1/project-1:TAN-347",
                "worker_id": "worker-1",
                "host_id": "host-1",
            },
        }
        blackboard = {
            "run_id": run_id,
            "task": task,
            "repo": task["repo"],
            "manager_plan": {"summary": "Document the smoke harness.", "subtasks": []},
            "subtasks": [
                {
                    "id": "docs",
                    "title": "Document smoke harness",
                    "files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                    "target_files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                }
            ],
            "workers": [
                {
                    "worker_id": "worker-1",
                    "subtask_id": "docs",
                    "title": "Document smoke harness",
                    "status": "completed",
                    "returncode": 0,
                    "changed_files": ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
                    "output_excerpt": "Added smoke harness docs.",
                }
            ],
        }
        board = {
            "board": {"id": "aca", "name": "ACA", "columns": ["in_progress", "review"]},
            "cards": [{"id": "TAN-347", "title": "Document smoke harness", "lane": "in_progress"}],
        }
        (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
        (run_dir / "blackboard.yaml").write_text(yaml.safe_dump(blackboard), encoding="utf-8")
        (run_dir / "board.yaml").write_text(yaml.safe_dump(board), encoding="utf-8")
        (run_dir / "events.jsonl").write_text(
            json.dumps({"type": "worker.completed", "run_id": run_id, "payload": {"returncode": 0}}) + "\n",
            encoding="utf-8",
        )
        return run_dir

    def test_recover_restart_orphaned_run_finalizes_completed_worker_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            repo = root / "repo"
            run_dir = self._write_run(root, repo)
            captured = {}

            def fake_finalize(ctx):
                captured["ctx"] = ctx
                return {"status": {"run": {"status": "completed"}, "pull_request": "https://github.test/pr/1"}}

            with (
                patch(
                    "src.tandem_agents.core.execution.run_recovery._run_engine_command_checks",
                    return_value=[
                        {
                            "command": "python3 -m unittest src.tandem_agents.aca_harness.calculator_test",
                            "returncode": 0,
                            "status": "pass",
                        }
                    ],
                ),
                patch("src.tandem_agents.core.execution.run_recovery.finalize_completed_run", side_effect=fake_finalize),
            ):
                result = recover_restart_orphaned_run(cfg, run_dir)

        self.assertTrue(result["recovered"])
        ctx = captured["ctx"]
        self.assertEqual(ctx.worker_results[0]["changed_files"], ["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"])
        self.assertEqual(ctx.repo_validation["command_checks"][0]["status"], "pass")
        self.assertEqual(ctx.status["verification"]["outcome"], "pass")

    def test_recover_restart_orphaned_run_ignores_run_without_completed_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            repo = root / "repo"
            run_dir = self._write_run(root, repo)
            blackboard = yaml.safe_load((run_dir / "blackboard.yaml").read_text(encoding="utf-8"))
            blackboard["workers"][0]["status"] = "failed"
            (run_dir / "blackboard.yaml").write_text(yaml.safe_dump(blackboard), encoding="utf-8")

            result = recover_restart_orphaned_run(cfg, run_dir)

        self.assertFalse(result["recovered"])
        self.assertEqual(result["reason"], "worker_not_completed")

    def test_cleanup_terminal_orphaned_engine_session_deletes_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            run_dir = root / "runs" / "sched-20260621T010101Z-cleanup"
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {"run_id": run_dir.name, "status": "blocked"},
                        "task": {"task_id": "TAN-170"},
                        "repo": {"path": str(root / "repo")},
                        "blocker": {"detail": "worker=worker-1; session_id=session-1; engine_run_id=run-1"},
                    }
                ),
                encoding="utf-8",
            )
            marker = run_dir / "active_worker_engine_sessions.json"
            marker.write_text(
                json.dumps({"worker-1": {"session_id": "session-1", "run_id": "run-1"}}),
                encoding="utf-8",
            )

            with patch(
                "src.tandem_agents.core.execution.run_recovery.delete_tandem_session",
                return_value=None,
            ) as delete_session:
                result = cleanup_terminal_orphaned_engine_sessions_for_run(cfg, run_dir)

            delete_session.assert_called_once_with(cfg, "session-1")
            self.assertEqual(result["sessions"][0]["ok"], True)
            self.assertFalse(marker.exists())
            event_types = [
                json.loads(line)["type"]
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(event_types, ["run.orphan_engine_session_cancelled"])

    def test_cleanup_terminal_orphaned_engine_session_preserves_marker_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            run_dir = root / "runs" / "sched-20260621T010101Z-cleanup"
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {"run_id": run_dir.name, "status": "blocked"},
                        "task": {"task_id": "TAN-170"},
                        "repo": {"path": str(root / "repo")},
                    }
                ),
                encoding="utf-8",
            )
            marker = run_dir / "active_worker_engine_sessions.json"
            marker.write_text(
                json.dumps({"worker-1": {"session_id": "session-1", "run_id": "run-1"}}),
                encoding="utf-8",
            )

            with patch(
                "src.tandem_agents.core.execution.run_recovery.delete_tandem_session",
                side_effect=RuntimeError("delete failed"),
            ):
                result = cleanup_terminal_orphaned_engine_sessions_for_run(cfg, run_dir)

            self.assertEqual(result["sessions"][0]["ok"], False)
            active = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(active["worker-1"]["session_id"], "session-1")
            self.assertIn("delete failed", active["worker-1"]["cleanup_error"])
            self.assertIn("cleanup_failed_at_ms", active["worker-1"])

    def test_cleanup_terminal_orphaned_engine_sessions_scans_scheduler_run_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            run_dir = root / "runs" / "sched-20260621T010101Z-cleanup"
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {"run_id": run_dir.name, "status": "blocked"},
                        "task": {"task_id": "TAN-170"},
                        "repo": {"path": str(root / "repo")},
                        "blocker": {"detail": "worker=worker-1; session_id=session-1"},
                    }
                ),
                encoding="utf-8",
            )

            with patch("src.tandem_agents.core.execution.run_recovery.delete_tandem_session"):
                result = cleanup_terminal_orphaned_engine_sessions(cfg)

            self.assertEqual(result[0]["run_id"], run_dir.name)
            self.assertEqual(result[0]["sessions"][0]["session_id"], "session-1")


if __name__ == "__main__":
    unittest.main()
