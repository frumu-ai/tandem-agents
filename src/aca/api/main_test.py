from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.aca.api.main import _project_runtime_env, app


class AcaApiWorkspaceGuideTest(unittest.TestCase):
    def test_mcp_routes_are_registered_on_the_main_api(self) -> None:
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/server.json", paths)
        self.assertIn("/.well-known/mcp/server.json", paths)
        self.assertIn("/mcp", paths)

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
        self.assertEqual(env["ACA_REPO_PATH"], "/workspace/repos/tandem")
        self.assertEqual(env["ACA_TASK_SOURCE_REPO"], "tandem")

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
