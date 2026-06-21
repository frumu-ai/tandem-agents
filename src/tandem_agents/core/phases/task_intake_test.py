from __future__ import annotations

import tempfile
from contextlib import ExitStack
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.phases.task_intake import run_task_intake


class _ClaimBlockedCoordination:
    def __init__(self) -> None:
        self.heartbeat_calls: list[tuple[tuple, dict]] = []
        self.registered_worker: dict | None = None

    def register_task(self, task, *, repo=None, status="queued"):
        return {"task_key": "manual:manual:do-the-thing:TAN-170", "state": status}

    def register_worker(self, **kwargs):
        self.registered_worker = dict(kwargs)

    def claim_task(self, *_args, **_kwargs):
        return {
            "claimed": False,
            "reason": "active_lease_exists",
            "task": {"task_key": "manual:manual:do-the-thing:TAN-170"},
            "active_lease": {"lease_id": "lease-old"},
        }

    def heartbeat_lease(self, *args, **kwargs):
        self.heartbeat_calls.append((args, kwargs))
        return {"lease_id": "lease-old", "status": "active"}


class _ClaimSuccessCoordination:
    def __init__(self) -> None:
        self.registered_worker: dict | None = None
        self.updated_runs: list[dict] = []
        self.marked_active: list[tuple[tuple, dict]] = []
        self.heartbeat_calls: list[tuple[tuple, dict]] = []

    def register_task(self, task, *, repo=None, status="queued"):
        return {"task_key": "manual:manual:do-the-thing:TAN-170", "state": status}

    def register_worker(self, **kwargs):
        self.registered_worker = dict(kwargs)

    def claim_task(self, *_args, **_kwargs):
        return {
            "claimed": True,
            "lease": {"lease_id": "lease-new", "expires_at_ms": 123456789},
        }

    def update_run(self, run_id, **kwargs):
        self.updated_runs.append({"run_id": run_id, **kwargs})

    def heartbeat_lease(self, *args, **kwargs):
        self.heartbeat_calls.append((args, kwargs))
        return {"lease_id": args[0] if args else "lease-new", "status": "active"}

    def mark_task_active(self, *args, **kwargs):
        self.marked_active.append((args, kwargs))


class TaskIntakeTest(unittest.TestCase):
    def test_claim_blocked_does_not_extend_existing_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            repo_path = root / "repo"
            repo_path.mkdir()
            layout = {
                "events": run_dir / "events.jsonl",
                "board": run_dir / "board.yaml",
                "blackboard": run_dir / "blackboard.yaml",
                "status": run_dir / "status.json",
            }
            task = {
                "task_id": "TAN-170",
                "title": "Do the thing",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "task_contract": {"local_goal": "Do the thing"},
                "contract_completeness": {"ok": True},
                "dependency_status": {"blocked": False},
                "verification_commands": [],
                "target_files": [],
                "acceptance_criteria": [],
            }
            cfg = SimpleNamespace(
                task_source=SimpleNamespace(type="manual", team="", project="", statuses="", labels="", query="", item="", url=""),
                swarm=SimpleNamespace(enabled=False, shared_model=False, max_workers=1),
                repository=SimpleNamespace(slug="frumu-ai/example", remote_name="origin", default_branch="main"),
                coordination=SimpleNamespace(lease_ttl_seconds=300),
                github_mcp=SimpleNamespace(scope="none", remote_sync="off"),
                linear_mcp=SimpleNamespace(scope="none", remote_sync="off"),
            )
            coordination = _ClaimBlockedCoordination()
            ctx = RunContext(
                cfg=cfg,
                run_id="run-1",
                run_dir=run_dir,
                layout=layout,
                repo={"path": str(repo_path), "slug": "frumu-ai/example"},
                coordination=coordination,
                engine={"provider": "test"},
            )

            run_repo_path = run_dir / "repo" / "aca-run-1"
            with (
                patch("src.tandem_agents.core.integrations.github_mcp.github_mcp_scope", return_value="none"),
                patch("src.tandem_agents.core.integrations.github_mcp.github_remote_sync_mode", return_value="off"),
                patch("src.tandem_agents.runtime.task_sources.normalize_task", return_value=(task, {"cards": []}, layout["board"])),
                patch("src.tandem_agents.core.engine.engine.task_run_branch_name", return_value="aca/run-1"),
                patch("src.tandem_agents.core.engine.engine.task_run_worktree_name", return_value="aca-run-1"),
                patch("src.tandem_agents.core.engine.engine.checkout_run_worktree", return_value=run_repo_path) as checkout_worktree,
                patch("src.tandem_agents.core.repository.repository.repository_status", return_value={"path": str(run_repo_path), "slug": "frumu-ai/example"}),
                patch("src.tandem_agents.core.execution.runner_core._task_claim_identity", return_value={"worker_id": "worker-new", "host_id": "host-new", "role": "coordinator", "source_type": "manual"}),
                patch("src.tandem_agents.core.execution.runner_core._record_review_policy"),
                patch("src.tandem_agents.core.execution.runner_core._append_blackboard_note"),
                patch("src.tandem_agents.runtime.runstate.initial_blackboard", return_value={}),
                patch("src.tandem_agents.runtime.run_output.write_blackboard_snapshot"),
                patch("src.tandem_agents.runtime.run_output.write_board_snapshot"),
                patch("src.tandem_agents.core.execution.run_lifecycle.build_provider_config_dict", return_value={}),
                patch("src.tandem_agents.core.execution.run_lifecycle.block_run", return_value={"blocked": True}),
            ):
                result = run_task_intake(ctx)

            self.assertEqual(result, {"blocked": True})
            checkout_worktree.assert_called_once_with(cfg, repo_path, run_repo_path, "aca/run-1")
            self.assertEqual(ctx.repo_path, run_repo_path)
            self.assertEqual(ctx.repo["slug"], "frumu-ai/example")
            self.assertEqual(ctx.repo["source_path"], str(repo_path))
            self.assertEqual(ctx.task["repo"]["path"], str(run_repo_path))
            self.assertEqual(ctx.task["repo"]["source_path"], str(repo_path))
            self.assertEqual(ctx.blackboard["repo"]["path"], str(run_repo_path))
            self.assertEqual(coordination.heartbeat_calls, [])
            self.assertEqual(coordination.registered_worker["worker_id"], "worker-new")


    def test_board_claim_preserves_run_worktree_repo_metadata_on_replaced_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            repo_path = root / "repo"
            repo_path.mkdir()
            layout = {
                "events": run_dir / "events.jsonl",
                "board": run_dir / "board.yaml",
                "blackboard": run_dir / "blackboard.yaml",
                "status": run_dir / "status.json",
            }
            task = {
                "task_id": "TAN-170",
                "title": "Do the thing",
                "source": {"type": "manual", "prompt": "Do the thing"},
                "task_contract": {"local_goal": "Do the thing"},
                "contract_completeness": {"ok": True},
                "dependency_status": {"blocked": False},
                "verification_commands": [],
                "target_files": [],
                "acceptance_criteria": [],
                "execution_kind": "code_edit",
            }
            board = {"cards": [{"id": "TAN-170", "lane": "todo"}]}
            card_task = {
                "task_id": "TAN-170",
                "title": "Do the thing from board",
                "source": {"type": "manual"},
                "task_contract": {"local_goal": "Do the thing from board"},
                "contract_completeness": {"ok": True},
                "dependency_status": {"blocked": False},
                "verification_commands": [],
                "target_files": [],
                "acceptance_criteria": [],
            }
            cfg = SimpleNamespace(
                task_source=SimpleNamespace(type="manual", team="", project="", statuses="", labels="", query="", item="", url=""),
                swarm=SimpleNamespace(enabled=False, shared_model=False, max_workers=1),
                repository=SimpleNamespace(slug="frumu-ai/example", remote_name="origin", default_branch="main"),
                coordination=SimpleNamespace(lease_ttl_seconds=300),
                github_mcp=SimpleNamespace(scope="none", remote_sync="off"),
                linear_mcp=SimpleNamespace(scope="none", remote_sync="off"),
            )
            coordination = _ClaimSuccessCoordination()
            ctx = RunContext(
                cfg=cfg,
                run_id="run-1",
                run_dir=run_dir,
                layout=layout,
                repo={"path": str(repo_path), "slug": "frumu-ai/example", "clone_url": "https://example/repo.git"},
                coordination=coordination,
                engine={"provider": "test"},
            )
            run_repo_path = run_dir / "repo" / "aca-run-1"

            patches = [
                patch("src.tandem_agents.core.integrations.github_mcp.github_mcp_scope", return_value="none"),
                patch("src.tandem_agents.core.integrations.github_mcp.github_remote_sync_mode", return_value="off"),
                patch("src.tandem_agents.runtime.task_sources.normalize_task", return_value=(task, board, layout["board"])),
                patch("src.tandem_agents.core.engine.engine.task_run_branch_name", return_value="aca/run-1"),
                patch("src.tandem_agents.core.engine.engine.task_run_worktree_name", return_value="aca-run-1"),
                patch("src.tandem_agents.core.engine.engine.checkout_run_worktree", return_value=run_repo_path),
                patch("src.tandem_agents.core.repository.repository.repository_status", return_value={"path": str(run_repo_path), "slug": "frumu-ai/example"}),
                patch("src.tandem_agents.core.execution.runner_core._task_claim_identity", return_value={"worker_id": "worker-new", "host_id": "host-new", "role": "coordinator", "source_type": "manual"}),
                patch("src.tandem_agents.core.repository.board.select_card", return_value=board["cards"][0]),
                patch("src.tandem_agents.core.repository.board.claim_card"),
                patch("src.tandem_agents.core.repository.board.card_to_task", return_value=card_task),
                patch("src.tandem_agents.core.repository.board.save_board"),
                patch("src.tandem_agents.core.execution.run_lifecycle.build_provider_config_dict", return_value={}),
                patch("src.tandem_agents.core.execution.run_lifecycle.build_swarm_config_dict", return_value={}),
                patch("src.tandem_agents.core.engine.coder_backend.coder_backend_mode", return_value="local"),
                patch("src.tandem_agents.core.execution.runner_core._record_review_policy"),
                patch("src.tandem_agents.core.execution.runner_core._append_blackboard_note"),
                patch("src.tandem_agents.runtime.runstate.initial_blackboard", return_value={}),
                patch("src.tandem_agents.runtime.run_output.write_blackboard_snapshot"),
                patch("src.tandem_agents.runtime.run_output.write_board_snapshot"),
            ]
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                result = run_task_intake(ctx)

            self.assertIsNone(result)
            self.assertEqual(ctx.task["title"], "Do the thing from board")
            self.assertEqual(ctx.task["repo"]["path"], str(run_repo_path))
            self.assertEqual(ctx.task["repo"]["source_path"], str(repo_path))
            self.assertEqual(ctx.task["repo"]["branch"], "aca/run-1")
            self.assertEqual(ctx.status["task"]["repo"]["path"], str(run_repo_path))
            self.assertEqual(ctx.blackboard["task"]["repo"]["path"], str(run_repo_path))
            self.assertEqual(ctx.blackboard["repo"]["path"], str(run_repo_path))
            self.assertTrue(coordination.marked_active)


if __name__ == "__main__":
    unittest.main()
