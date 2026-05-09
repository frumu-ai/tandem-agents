from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.runtime.workspace_registry import (
    configured_project_binding,
    record_run_reference,
    load_workspace,
    project_binding_from_compat,
    save_workspace,
    set_active_project,
    workspace_file,
    workspace_summary,
    workspace_view,
)


def _fake_cfg(root: Path):
    return SimpleNamespace(
        repository=SimpleNamespace(
            slug="acme/demo",
            default_branch="main",
            path="",
            remote_name="origin",
            worktree_root="",
            credential_file="",
            clone_url="https://github.com/acme/demo.git",
        ),
        task_source=SimpleNamespace(
            type="github_project",
            owner="acme",
            repo="demo",
            project="1",
            item="",
            url="",
            path="",
            prompt="",
            source_name="",
            card_id="",
        ),
        repository_path=lambda: root / "repos" / "demo",
        task_source_path=lambda: None,
    )


class WorkspaceRegistryTest(unittest.TestCase):
    def test_round_trip_writes_workspace_and_project_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = {
                "workspace": {
                    "id": "workspace-1",
                    "name": "Tandem Agents Workspace",
                    "created_at_ms": 1,
                    "updated_at_ms": 1,
                    "projects": [
                        project_binding_from_compat(
                            "alpha",
                            {
                                "repo_url": "https://github.com/acme/demo.git",
                                "task_source": {"type": "github_project", "owner": "acme", "repo": "demo", "project": "1"},
                            },
                        )
                    ],
                    "runs": [],
                    "active_project_id": "alpha",
                }
            }
            save_workspace(root, workspace)

            self.assertTrue(workspace_file(root).exists())
            self.assertTrue((root / ".tandem-agents" / "projects").exists())

            loaded = load_workspace(root)
            self.assertEqual(loaded["workspace"]["active_project_id"], "alpha")
            self.assertEqual(len(loaded["workspace"]["projects"]), 1)
            self.assertEqual(loaded["workspace"]["projects"][0]["id"], "alpha")

    def test_legacy_projects_file_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "config" / "projects.yaml"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(
                """
alpha:
  slug: acme/demo
  repo_url: https://github.com/acme/demo.git
  task_source:
    type: github_project
    owner: acme
    repo: demo
    project: "1"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            loaded = load_workspace(root)
            self.assertEqual(loaded["workspace"]["projects"][0]["id"], "alpha")
            self.assertEqual(loaded["workspace"]["projects"][0]["repo"]["slug"], "acme/demo")

    def test_workspace_view_injects_configured_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _fake_cfg(root)
            view = workspace_view(root, cfg)
            self.assertEqual(view["summary"]["project_count"], 1)
            self.assertEqual(view["workspace"]["active_project_id"], "acme/demo")
            self.assertEqual(view["projects"][0]["id"], "acme/demo")
            self.assertEqual(view["projects"][0]["repo"]["path"], str(root / "repos" / "demo"))

    def test_project_binding_preserves_named_repo_paths_and_credentials(self):
        binding = project_binding_from_compat(
            "alpha",
            {
                "name": "Alpha",
                "repo": {
                    "slug": "acme/alpha",
                    "path": "repos/alpha",
                    "worktree_root": "worktrees",
                    "credential_file": "secrets/github_token",
                    "clone_url": "https://github.com/acme/alpha.git",
                    "default_branch": "main",
                    "remote_name": "origin",
                },
                "task_source": {"type": "manual", "prompt": "Hello"},
            },
        )

        self.assertEqual(binding["repo"]["path"], "repos/alpha")
        self.assertEqual(binding["repo"]["worktree_root"], "worktrees")
        self.assertEqual(binding["repo"]["credential_file"], "secrets/github_token")

    def test_set_active_project_requires_known_project(self):
        workspace = {
            "workspace": {
                "id": "workspace-1",
                "name": "Tandem Agents Workspace",
                "created_at_ms": 1,
                "updated_at_ms": 1,
                "projects": [
                    project_binding_from_compat(
                        "alpha",
                        {
                            "repo_url": "https://github.com/acme/demo.git",
                            "task_source": {"type": "github_project", "owner": "acme", "repo": "demo", "project": "1"},
                        },
                    )
                ],
                "runs": [],
                "active_project_id": None,
            }
        }
        updated = set_active_project(workspace, "alpha")
        self.assertEqual(updated["workspace"]["active_project_id"], "alpha")
        with self.assertRaises(ValueError):
            set_active_project(workspace, "missing")

    def test_record_run_reference_tracks_execution_backend(self):
        workspace = {
            "workspace": {
                "id": "workspace-1",
                "name": "Tandem Agents Workspace",
                "created_at_ms": 1,
                "updated_at_ms": 1,
                "projects": [],
                "runs": [],
                "active_project_id": None,
            }
        }
        updated = record_run_reference(
            workspace,
            run_id="run-123",
            project_id="alpha",
            project_key="github_project:acme/demo",
            status="starting",
            execution_backend="coder",
            admission_role="aca_scheduler",
            execution_path="tandem_coder",
            task_key="task-1",
            task_title="Do the thing",
        )
        run_ref = updated["workspace"]["runs"][0]
        self.assertEqual(run_ref["run_id"], "run-123")
        self.assertEqual(run_ref["project_id"], "alpha")
        self.assertEqual(run_ref["project_key"], "github_project:acme/demo")
        self.assertEqual(run_ref["execution_backend"], "coder")
        self.assertEqual(run_ref["admission_role"], "aca_scheduler")
        self.assertEqual(run_ref["execution_path"], "tandem_coder")
        self.assertEqual(run_ref["task_key"], "task-1")
        self.assertEqual(run_ref["task_title"], "Do the thing")


if __name__ == "__main__":
    unittest.main()
