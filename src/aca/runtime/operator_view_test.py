from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from src.aca.config.config_loader import resolve_config
from src.aca.core.coordination.coordination import CoordinationStore
from src.aca.runtime.operator_view import build_operator_summary
from src.aca.runtime.runstate import initial_blackboard, initial_status, save_blackboard, write_status
from src.aca.runtime.workspace_registry import project_binding_from_compat, record_run_reference, save_workspace


class OperatorViewTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "tandem-data").mkdir(parents=True, exist_ok=True)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_COORDINATION_SQLITE_PATH=tandem-data/coordination.sqlite3",
                    "ACA_TASK_SOURCE_TYPE=manual",
                    "ACA_TASK_SOURCE_PROMPT=Do the thing",
                    "ACA_REPO_SLUG=frumu-ai/example",
                    "ACA_PROVIDER=openai",
                    "ACA_MODEL=gpt-4.1-mini",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "agent.yaml").write_text(
            dedent(
                """
                agent:
                  name: ACA
                tandem:
                  base_url: http://127.0.0.1:39733
                task_source:
                  type: manual
                  prompt: Do the thing
                repository:
                  slug: frumu-ai/example
                provider:
                  id: openai
                  model: gpt-4.1-mini
                swarm:
                  enabled: false
                output:
                  root: runs
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def test_operator_summary_joins_task_lease_worker_run_and_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root)
            store = CoordinationStore.from_config(cfg)

            workspace = {
                "workspace": {
                    "id": "workspace-1",
                    "name": "ACA Workspace",
                    "created_at_ms": 1,
                    "updated_at_ms": 1,
                    "projects": [
                        project_binding_from_compat(
                            "alpha",
                            {
                                "repo_url": "https://github.com/frumu-ai/example.git",
                                "task_source": {"type": "manual", "prompt": "Do the thing"},
                            },
                        )
                    ],
                    "runs": [],
                    "active_project_id": "alpha",
                }
            }
            save_workspace(root, workspace)

            task = {
                "task_id": "task-1",
                "title": "Implement thing",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
            }
            store.register_task(task, repo=task["repo"])
            claim = store.claim_task(
                task,
                run_id="run-1",
                worker_id="worker-1",
                host_id="host-a",
                lease_ttl_seconds=60,
                branch_name="aca/example/run-1",
                repo=task["repo"],
            )
            task_key = claim["task"]["task_key"]
            lease_id = claim["lease"]["lease_id"]

            run_dir = cfg.output_root() / "run-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            status = initial_status(
                "run-1",
                {
                    "task_key": task_key,
                    "title": "Implement thing",
                    "repo": {"slug": "frumu-ai/example", "path": str(root / "repo")},
                },
                {"slug": "frumu-ai/example", "path": str(root / "repo"), "branch": "aca/example/run-1"},
                {"version": "engine"},
                {"id": "openai", "model": "gpt-4.1-mini"},
                {"enabled": False, "shared_model": False, "max_workers": 1},
                run_dir,
            )
            status["run"]["status"] = "running"
            status["phase"] = {"name": "coder_execution", "detail": None, "role": "worker", "updated_at_ms": 1}
            status["github_mcp"] = {
                "scope": "intake_finalize",
                "remote_sync": "status_comment",
                "connected": True,
                "last_action": "connected_for_finalize",
            }
            status["blocker"] = {"active": True, "kind": "review", "message": "Need human review", "owner_role": "reviewer"}
            write_status(run_dir / "status.json", status)

            blackboard = initial_blackboard(
                "run-1",
                {"title": "Implement thing"},
                {"slug": "frumu-ai/example", "path": str(root / "repo")},
                {"id": "openai", "model": "gpt-4.1-mini"},
                {"version": "engine"},
                {"enabled": False, "shared_model": False, "max_workers": 1},
            )
            blackboard["execution_backend"] = "coder"
            blackboard["coder_run"] = {"coder_run_id": "run-1", "status": "running", "phase": "coding"}
            blackboard["coder_supervision"] = {"tandem_status": "running", "tandem_phase": "coding"}
            blackboard["pull_request"] = "https://github.com/frumu-ai/example/pull/7"
            blackboard["review_policy"] = {
                "policy": "human_review",
                "human_review_required": True,
                "auto_merge_requested": False,
            }
            blackboard["blockers"] = [{"kind": "review", "message": "Need human review"}]
            save_blackboard(run_dir / "blackboard.yaml", blackboard)

            save_workspace(
                root,
                record_run_reference(
                    workspace,
                    run_id="run-1",
                    project_id="alpha",
                    project_key="manual:frumu-ai/example",
                    status="running",
                    execution_backend="coder",
                    admission_role="aca_scheduler",
                    execution_path="tandem_coder",
                    task_key=task_key,
                    task_title="Implement thing",
                ),
            )

            with store.connection() as conn:
                conn.execute("UPDATE workers SET last_seen_at_ms = 0 WHERE worker_id = ?", ("worker-1",))

            summary = build_operator_summary(cfg, coordination=store, limit=10)

            self.assertEqual(summary["coordination"]["summary"]["tasks"], 1)
            self.assertEqual(summary["coordination"]["summary"]["workers"], 1)
            self.assertEqual(summary["tasks"][0]["task_key"], task_key)
            self.assertEqual(summary["tasks"][0]["branch"], "aca/example/run-1")
            self.assertEqual(summary["tasks"][0]["pull_request"], "https://github.com/frumu-ai/example/pull/7")
            self.assertEqual(summary["tasks"][0]["execution_backend"], "coder")
            self.assertEqual(summary["tasks"][0]["admission_role"], "aca_scheduler")
            self.assertEqual(summary["tasks"][0]["execution_path"], "tandem_coder")
            self.assertEqual(summary["tasks"][0]["ownership_state"], "owned")
            self.assertEqual(summary["tasks"][0]["worker"]["worker_id"], "worker-1")
            self.assertEqual(summary["tasks"][0]["lease"]["lease_id"], lease_id)
            self.assertEqual(summary["tasks"][0]["blocked_reason"], "Need human review")
            self.assertTrue(summary["recovery"]["stale_workers"])
            self.assertEqual(summary["runs"][0]["pull_request"], "https://github.com/frumu-ai/example/pull/7")
            self.assertEqual(summary["runs"][0]["execution_backend"], "coder")
            self.assertEqual(summary["runs"][0]["github_mcp"]["remote_sync"], "status_comment")
            self.assertEqual(summary["coder_runs"][0]["coder_run_id"], "run-1")
            self.assertEqual(summary["runs"][0]["coder_supervision"]["tandem_status"], "running")


if __name__ == "__main__":
    unittest.main()
