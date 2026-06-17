from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.tandem_agents.api import main as api_main
from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.api.main import (
    _active_run_claim_for_task,
    _active_scheduler_project_keys,
    _compact_event_payload,
    _linear_auth_redirect_origin,
    _operator_coordination_state,
    _operator_terminalize_reset_run,
    _project_runtime_env,
    _terminalize_expired_coordination_run,
    update_project_task_state,
    app,
)
from src.tandem_agents.core.coordination.coordination import CoordinationStore


class AcaApiWorkspaceGuideTest(unittest.TestCase):
    def _write_minimal_config(self, root: Path) -> None:
        (root / "tandem-data").mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(
            "\n".join(
                [
                    "ACA_COORDINATION_SQLITE_PATH=tandem-data/coordination.sqlite3",
                    "ACA_TASK_SOURCE_TYPE=manual",
                    "ACA_TASK_SOURCE_PROMPT=Do the thing",
                    "ACA_REPO_SLUG=frumu-ai/example",
                    "ACA_PROVIDER=openai",
                    "ACA_MODEL=gpt-5.5",
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
                    "tandem:",
                    "  base_url: http://127.0.0.1:39733",
                    "task_source:",
                    "  type: manual",
                    "  prompt: Do the thing",
                    "repository:",
                    "  slug: frumu-ai/example",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-5.5",
                    "output:",
                    "  root: runs",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_mcp_routes_are_registered_on_the_main_api(self) -> None:
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/server.json", paths)
        self.assertIn("/.well-known/mcp/server.json", paths)
        self.assertIn("/mcp", paths)

    def test_operator_state_translation_matches_coordination_states(self) -> None:
        self.assertEqual(_operator_coordination_state("Todo"), "queued")
        self.assertEqual(_operator_coordination_state("Backlog"), "queued")
        self.assertEqual(_operator_coordination_state("In Progress"), "active")
        self.assertEqual(_operator_coordination_state("In Review"), "review")
        self.assertEqual(_operator_coordination_state("Done"), "done")
        self.assertEqual(_operator_coordination_state("Blocked"), "blocked")

    def test_operator_terminalize_reset_run_marks_active_status_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_config(root)
            cfg = resolve_config(root)
            run_id = f"run-operator-reset-{root.name}"
            run_dir = cfg.output_root() / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {
                            "run_id": run_id,
                            "status": "running",
                            "updated_at_ms": 1,
                            "completed_at_ms": None,
                        },
                        "phase": {"name": "worker_execution", "detail": "running"},
                        "coordination": {"lease_id": "lease-1", "lease_status": "active"},
                        "blocker": {"active": False, "kind": None, "message": None},
                    }
                ),
                encoding="utf-8",
            )

            updated = _operator_terminalize_reset_run(
                cfg,
                run_id=run_id,
                task_id="TAN-170",
                target_status="Backlog",
                coordination_state="queued",
                lease={
                    "lease_id": "lease-1",
                    "task_key": "linear:team/project:TAN-170",
                    "worker_id": "worker-1",
                    "host_id": "host-a",
                    "status": "stale",
                    "heartbeat_at_ms": 11,
                    "expires_at_ms": 22,
                },
            )

            self.assertTrue(updated)
            status_payload = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status_payload["run"]["status"], "blocked")
            self.assertIsNotNone(status_payload["run"]["completed_at_ms"])
            self.assertEqual(status_payload["phase"]["role"], "operator")
            self.assertEqual(status_payload["coordination"]["lease_status"], "stale")
            self.assertEqual(status_payload["blocker"]["kind"], "operator_requeued")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(events[-1]["type"], "run.blocked")
            self.assertEqual(events[-1]["payload"]["target_status"], "Backlog")

    def test_operator_state_update_rejects_active_run_reset_without_force(self) -> None:
        cfg = SimpleNamespace(task_source=SimpleNamespace(type="linear"))
        with patch("src.tandem_agents.api.main._project_config", return_value=({}, cfg)), \
            patch(
                "src.tandem_agents.api.main._active_run_claim_for_task",
                return_value={"run_id": "run-1", "lease_id": "lease-1"},
            ), \
            patch("src.tandem_agents.api.main.linear_update_issue") as update_issue:
            with self.assertRaises(api_main.HTTPException) as raised:
                asyncio.run(
                    update_project_task_state(
                        "project",
                        "TAN-170",
                        {"state": "Backlog"},
                        token="secret-token",
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["active_run"]["run_id"], "run-1")
        update_issue.assert_not_called()

    def test_terminalize_expired_coordination_run_marks_zombie_status_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_config(root)
            cfg = resolve_config(root)
            run_id = f"run-expired-{root.name}"
            run_dir = cfg.output_root() / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {
                            "run_id": run_id,
                            "status": "running",
                            "updated_at_ms": 1,
                            "completed_at_ms": None,
                        },
                        "task": {"task_id": "TAN-170", "title": "Lease expiry test"},
                        "phase": {"name": "worker_execution", "detail": "running"},
                        "coordination": {
                            "lease_id": "lease-expired",
                            "lease_status": "active",
                        },
                        "blocker": {"active": False, "kind": None, "message": None},
                    }
                ),
                encoding="utf-8",
            )
            api_main.run_manager.create_run(run_id, "test")

            try:
                updated = _terminalize_expired_coordination_run(
                    cfg,
                    {
                        "run_id": run_id,
                        "lease_id": "lease-expired",
                        "task_key": "linear:team/project:TAN-170",
                        "worker_id": "worker-1",
                        "host_id": "host-a",
                        "status": "stale",
                        "release_reason": "expired",
                        "heartbeat_at_ms": 11,
                        "expires_at_ms": 22,
                    },
                )

                self.assertTrue(updated)
                status_payload = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
                self.assertEqual(status_payload["run"]["status"], "blocked")
                self.assertIsNotNone(status_payload["run"]["completed_at_ms"])
                self.assertEqual(status_payload["phase"]["name"], "coordination")
                self.assertEqual(status_payload["phase"]["role"], "coordinator")
                self.assertEqual(status_payload["coordination"]["lease_status"], "stale")
                self.assertEqual(status_payload["coordination"]["lease_release_reason"], "expired")
                self.assertEqual(status_payload["blocker"]["kind"], "coordination_lease_expired")
                self.assertIn("lease-expired", status_payload["blocker"]["message"])
                summary = (run_dir / "summary.md").read_text(encoding="utf-8")
                self.assertIn("Lease expiry test", summary)
                events = [
                    json.loads(line)
                    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertEqual(events[-1]["type"], "run.blocked")
                self.assertEqual(events[-1]["payload"]["kind"], "coordination_lease_expired")
                self.assertFalse(api_main.run_manager.runs[run_id].is_running)
            finally:
                with api_main.run_manager._lock:
                    api_main.run_manager.runs.pop(run_id, None)

    def test_active_run_claim_for_task_finds_orphaned_active_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_config(root)
            cfg = resolve_config(root)
            run_id = f"run-orphan-{root.name}"
            run_dir = cfg.output_root() / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {"run_id": run_id, "status": "running"},
                        "task": {
                            "task_id": "TAN-170",
                            "source": {"type": "linear", "identifier": "TAN-170"},
                        },
                        "coordination": {"lease_id": "lease-orphan"},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _active_run_claim_for_task(cfg, "TAN-170"),
                {"run_id": run_id, "lease_id": "lease-orphan"},
            )

    def test_coder_supervisor_reconcile_is_serialized(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        calls: list[str] = []

        def fake_reconcile(_cfg):
            calls.append("start")
            entered.set()
            release.wait(timeout=1)
            calls.append("end")
            return {"count": 0}

        def run_second() -> None:
            api_main._reconcile_active_coder_runs_serialized(object())
            second_done.set()

        with patch.object(api_main, "reconcile_active_coder_runs", side_effect=fake_reconcile):
            first = threading.Thread(
                target=api_main._reconcile_active_coder_runs_serialized,
                args=(object(),),
            )
            second = threading.Thread(target=run_second)
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            self.assertFalse(second_done.wait(timeout=0.02))
            self.assertEqual(calls, ["start"])
            release.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_done.is_set())
        self.assertEqual(calls, ["start", "end", "start", "end"])

    def test_approvals_status_query_filters_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_config(root)
            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
                "ACA_COORDINATION_SQLITE_PATH": str(root / "tandem-data" / "coordination.sqlite3"),
            }
            with patch.dict("os.environ", env, clear=False):
                cfg = resolve_config(root)
                store = CoordinationStore.from_config(cfg)
                store.ensure_schema()
                pending = store.enqueue_external_action_approval(
                    run_id="run-1",
                    task_id="TAN-110",
                    source_type="linear",
                    adapter="github_pr",
                    action_type="comment_pr",
                    target={"pr_number": 1},
                    payload={"body": "pending"},
                    risk_level="medium",
                )
                approved = store.enqueue_external_action_approval(
                    run_id="run-1",
                    task_id="TAN-110",
                    source_type="linear",
                    adapter="github_pr",
                    action_type="close_pr",
                    target={"pr_number": 2},
                    payload={},
                    risk_level="high",
                )
                store.decide_external_action_approval(
                    approved["approval_id"],
                    decision="approve",
                    actor="tester",
                    reason="ok",
                )
                with TestClient(app) as client:
                    response = client.get(
                        "/approvals",
                        params={"status": "pending"},
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()

            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["approvals"][0]["approval_id"], pending["approval_id"])

    def test_runs_list_uses_compact_events_and_omits_blackboard_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_minimal_config(root)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run": {
                            "status": "blocked",
                            "created_at_ms": 1,
                            "updated_at_ms": 2,
                            "error": "review required",
                        },
                        "task": {"title": "Compact me"},
                        "phase": {"name": "handoff"},
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "blackboard.yaml").write_text(
                "run_id: run-1\nlarge: should only appear on detail route\n",
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "seq": 1,
                        "type": "worker.completed",
                        "timestamp_ms": 2,
                        "run_id": "run-1",
                        "payload": {
                            "worker_id": "worker-1",
                            "returncode": 0,
                            "engine": {"messages": ["x" * 1000]},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
            }
            with patch.dict("os.environ", env, clear=False):
                with TestClient(app) as client:
                    list_response = client.get("/runs", headers={"Authorization": "Bearer secret-token"})
                    detail_response = client.get("/runs/run-1", headers={"Authorization": "Bearer secret-token"})

            self.assertEqual(list_response.status_code, 200, list_response.text)
            listed = list_response.json()["runs"][0]
            self.assertEqual(listed["blackboard"], {})
            self.assertEqual(listed["events"][0]["payload"], {"worker_id": "worker-1", "returncode": 0})

            self.assertEqual(detail_response.status_code, 200, detail_response.text)
            detailed = detail_response.json()
            self.assertEqual(detailed["blackboard"]["large"], "should only appear on detail route")
            self.assertIn("engine", detailed["events"][0]["payload"])

    def test_compact_event_payload_keeps_graph_and_partial_diff_diagnostics(self) -> None:
        payload = _compact_event_payload(
            {
                "repo_context": {
                    "source": "repo.context_bundle",
                    "fallback_used": False,
                    "artifact_path": "/runs/run-1/artifacts/repo_context_bundle.json",
                    "path_scope": "crates/tandem-meta-harness-eval",
                    "required_files": ["crates/tandem-meta-harness-eval/src/lib.rs"],
                    "index_status": "refreshed",
                    "secret_prompt": "omit me",
                },
                "partial_diff_artifacts": [
                    {
                        "worker_id": "worker-1",
                        "subtask_id": "subtask-1",
                        "patch_path": "/runs/run-1/artifacts/worker-1.patch",
                        "raw_diff": "omit me",
                    }
                ],
                "duration_ms": 123,
                "filters": {"statuses": "Backlog,Todo", "query": "", "token": "omit"},
                "engine": {"messages": ["omit"]},
            }
        )

        self.assertEqual(payload["repo_context"]["source"], "repo.context_bundle")
        self.assertFalse(payload["repo_context"]["fallback_used"])
        self.assertEqual(payload["repo_context"]["index_status"], "refreshed")
        self.assertEqual(payload["repo_context"]["path_scope"], "crates/tandem-meta-harness-eval")
        self.assertEqual(payload["repo_context"]["required_files"], ["crates/tandem-meta-harness-eval/src/lib.rs"])
        self.assertNotIn("secret_prompt", payload["repo_context"])
        self.assertEqual(payload["partial_diff_artifacts"][0]["worker_id"], "worker-1")
        self.assertNotIn("raw_diff", payload["partial_diff_artifacts"][0])
        self.assertEqual(payload["duration_ms"], 123)
        self.assertEqual(payload["filters"], {"statuses": "Backlog,Todo"})
        self.assertNotIn("engine", payload)

    def test_workspace_projects_and_guide_include_repo_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "secrets" / "github_token"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("secret-token\n", encoding="utf-8")

            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
            }

            with patch.dict("os.environ", env, clear=False):
                with TestClient(app) as client:
                    response = client.post(
                        "/workspace/projects",
                        params={
                            "slug": "alpha",
                            "repo_url": "https://github.com/acme/alpha.git",
                            "repo_path": "repos/alpha",
                            "worktree_root": "worktrees",
                            "default_branch": "main",
                            "remote_name": "origin",
                            "credential_file": "secrets/github_token",
                            "name": "Alpha",
                        },
                        json={"type": "manual", "prompt": "Hello"},
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)

                    projects = client.get("/projects", headers={"Authorization": "Bearer secret-token"})
                    self.assertEqual(projects.status_code, 200, projects.text)
                    payload = projects.json()
                    self.assertEqual(payload["alpha"]["repo"]["path"], "repos/alpha")
                    self.assertEqual(payload["alpha"]["repo"]["worktree_root"], "worktrees")
                    self.assertEqual(payload["alpha"]["repo"]["credential_file"], "secrets/github_token")

                    guide = client.get("/workspace/guide", headers={"Authorization": "Bearer secret-token"})
                    self.assertEqual(guide.status_code, 200, guide.text)
                    guide_payload = guide.json()
                    self.assertEqual(guide_payload["active_project"]["id"], "alpha")
                    self.assertEqual(guide_payload["active_project"]["repo"]["path"], "repos/alpha")
                    self.assertTrue(any("Call this guide first" in line for line in guide_payload["instructions"]))

    def test_workspace_active_project_round_trips_in_workspace_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
            }

            with patch.dict("os.environ", env, clear=False):
                with TestClient(app) as client:
                    for slug in ("alpha", "beta"):
                        response = client.post(
                            "/workspace/projects",
                            params={
                                "slug": slug,
                                "repo_path": f"repos/{slug}",
                                "name": slug.title(),
                            },
                            json={"type": "manual", "prompt": slug},
                            headers={"Authorization": "Bearer secret-token"},
                        )
                        self.assertEqual(response.status_code, 200, response.text)

                    set_active = client.post(
                        "/workspace/active/beta",
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(set_active.status_code, 200, set_active.text)

                    workspace = client.get("/workspace", headers={"Authorization": "Bearer secret-token"})
                    self.assertEqual(workspace.status_code, 200, workspace.text)
                    payload = workspace.json()
                    self.assertEqual(payload["workspace"]["active_project_id"], "beta")
                    self.assertEqual(payload["active_project_id"], "beta")
                    self.assertEqual(payload["active_project_slug"], "beta")

    def test_active_scheduler_project_keys_uses_workspace_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
            }

            with patch.dict("os.environ", env, clear=False):
                with TestClient(app) as client:
                    response = client.post(
                        "/workspace/projects",
                        params={
                            "slug": "linear-target",
                            "repo_path": "repos/tandem",
                            "name": "Linear Target",
                        },
                        json={
                            "type": "linear",
                            "team": "team-1",
                            "project": "project-1",
                        },
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    set_active = client.post(
                        "/workspace/active/linear-target",
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(set_active.status_code, 200, set_active.text)

                cfg = resolve_config(root)
                self.assertEqual(_active_scheduler_project_keys(root, cfg), {"linear:team-1/project-1"})

    def test_linear_auth_redirect_origin_reads_redirect_uri(self) -> None:
        url = (
            "https://mcp.linear.app/authorize?"
            "redirect_uri=https%3A%2F%2Ftests.frumu.ai%2Fapi%2Fengine%2Fmcp%2Flinear%2Fauth%2Fcallback"
        )

        self.assertEqual(_linear_auth_redirect_origin(url), "https://tests.frumu.ai")

    def test_project_runtime_env_uses_managed_checkout_path_for_remote_project(self) -> None:
        env = _project_runtime_env(
            Path("/tmp/aca"),
            {
                "id": "frumu-ai/tandem",
                "repo_url": "https://github.com/frumu-ai/tandem",
                "repo": {
                    "slug": "frumu-ai/tandem",
                    "path": "",
                    "default_branch": "main",
                    "remote_name": "origin",
                },
                "task_source": {
                    "type": "github_project",
                    "owner": "frumu-ai",
                    "repo": "tandem",
                    "project": "1",
                },
            },
        )

        self.assertEqual(env["ACA_REPO_SLUG"], "frumu-ai/tandem")
        self.assertEqual(env["ACA_REPO_URL"], "https://github.com/frumu-ai/tandem")
        self.assertEqual(env["ACA_REPO_PATH"], "workspace/repos/tandem")
        self.assertEqual(env["ACA_WORKTREE_ROOT"], "workspace/repos")
        self.assertEqual(env["ACA_TASK_SOURCE_REPO"], "tandem")

    def test_project_runtime_env_serializes_task_source_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _project_runtime_env(
                root,
                {
                    "id": "linear-runtime",
                    "repo_url": "https://github.com/frumu-ai/tandem-agents",
                    "repo": {"slug": "frumu-ai/tandem-agents"},
                    "task_source": {
                        "type": "linear",
                        "team": "Tandem",
                        "project": "Runtime",
                        "payload": {
                            "repo_routing": {
                                "require_explicit_repo_hint": True,
                            }
                        },
                    },
                },
            )

            self.assertEqual(
                json.loads(env["ACA_TASK_SOURCE_PAYLOAD"]),
                {"repo_routing": {"require_explicit_repo_hint": True}},
            )
            cfg = resolve_config(root, env=env)
            self.assertTrue(cfg.task_source.payload["repo_routing"]["require_explicit_repo_hint"])

    def test_project_repo_sync_initializes_local_workspace_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_repo = root / "repos" / "alpha"
            local_repo.mkdir(parents=True, exist_ok=True)
            (local_repo / "README.md").write_text("local workspace\n", encoding="utf-8")

            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
            }

            with patch.dict("os.environ", env, clear=False):
                with TestClient(app) as client:
                    create = client.post(
                        "/projects",
                        params={
                            "slug": "alpha",
                            "repo_path": "repos/alpha",
                            "name": "Alpha",
                        },
                        json={"type": "kanban_board", "path": "board.yaml"},
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(create.status_code, 200, create.text)

                    response = client.post(
                        "/projects/alpha/repo/sync",
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    self.assertTrue(payload["ok"])
                    self.assertEqual(Path(payload["repo"]["path"]).resolve(), local_repo.resolve())
                    self.assertFalse(payload["repo"]["dirty"])
                    self.assertTrue((local_repo / ".git").exists())


if __name__ == "__main__":
    unittest.main()
