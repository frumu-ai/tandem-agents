from __future__ import annotations

import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.engine.process_utils import run_command
from src.tandem_agents.core.repository.repository import (
    _git_clone_args_and_env,
    _github_pat,
    repository_binding_issues,
    resolve_repository,
    task_run_branch_name,
    worker_worktree_name,
)


class RepositoryNamingTest(unittest.TestCase):
    def test_task_run_branch_name_is_canonical(self) -> None:
        branch = task_run_branch_name(
            {"title": "Fix README", "task_id": "1234abcd"},
            "run5678",
            "frumu-ai/hello-tandem",
        )

        self.assertEqual(branch, "aca/frumu-ai-hello-tandem/fix-readme-1234abcd-run5678")

    def test_worker_worktree_name_includes_worker_and_subtask(self) -> None:
        self.assertEqual(worker_worktree_name("worker-a", "subtask-1"), "worker-a--subtask-1")
        self.assertEqual(worker_worktree_name("worker-a"), "worker-a")

    def test_repository_binding_issues_flag_non_git_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_dir = root / "repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository safety",
                        "repository:",
                        f"  path: {repo_dir}",
                        "  clone_url: https://github.com/acme/demo.git",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            issues = repository_binding_issues(cfg)

            self.assertTrue(any("not a git checkout" in issue for issue in issues))

    def test_resolve_repository_initializes_local_non_git_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_dir = root / "repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "README.md").write_text("local files\n", encoding="utf-8")
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: kanban_board",
                        "  path: board.yaml",
                        "repository:",
                        f"  path: {repo_dir}",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            self.assertEqual(repository_binding_issues(cfg), [])
            repo = resolve_repository(cfg)
            repo_again = resolve_repository(cfg)

            self.assertEqual(Path(repo["path"]).resolve(), repo_dir.resolve())
            self.assertEqual(Path(repo_again["path"]).resolve(), repo_dir.resolve())
            self.assertTrue((repo_dir / ".git").exists())
            self.assertFalse(repo["dirty"])
            self.assertFalse(repo_again["dirty"])
            self.assertEqual((repo_dir / "README.md").read_text(encoding="utf-8"), "local files\n")

    def test_resolve_repository_clones_into_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir(parents=True, exist_ok=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("source repo\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"])
            target = root / "checkout"
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository clone",
                        "repository:",
                        f"  path: {target}",
                        f"  clone_url: {source}",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            repo = resolve_repository(cfg)

            self.assertEqual(Path(repo["path"]).resolve(), target.resolve())
            self.assertTrue((target / ".git").exists())
            self.assertTrue((target / "README.md").exists())

    def test_resolve_repository_uses_token_file_for_private_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "secrets" / "github_token"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("secret-token\n", encoding="utf-8")
            repo_dir = root / "repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository clone",
                        "repository:",
                        f"  path: {repo_dir}",
                        f"  clone_url: https://github.com/acme/private-repo.git",
                        f"  credential_file: {token_file}",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            self.assertEqual(cfg.repository.credential_file, str(token_file))
            self.assertEqual(Path(cfg.repository_path() or "").resolve(), repo_dir.resolve())
            args, _ = _git_clone_args_and_env(cfg, "https://github.com/acme/private-repo.git", repo_dir)
            self.assertTrue(any("extraheader=AUTHORIZATION: basic" in arg for arg in args))

    def test_github_token_precedes_legacy_personal_access_token_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository clone",
                        "repository:",
                        "  path: repo",
                        "  clone_url: https://github.com/acme/private-repo.git",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(
                root,
                env={
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "stale-token",
                    "GITHUB_TOKEN": "current-token",
                },
            )

            self.assertEqual(_github_pat(cfg), "current-token")

    def test_resolve_repository_fast_forwards_existing_explicit_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "checkout"
            source.mkdir(parents=True, exist_ok=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("one\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "one"])
            run_command(["git", "clone", str(source), str(target)])
            (source / "README.md").write_text("two\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-am", "two"])
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository pull",
                        "repository:",
                        f"  path: {target}",
                        f"  clone_url: {source}",
                        "  default_branch: main",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            resolve_repository(cfg)

            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "two\n")

    def test_concurrent_resolve_repository_serializes_shared_checkout_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "checkout"
            source.mkdir(parents=True, exist_ok=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("one\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "one"])
            run_command(["git", "clone", str(source), str(target)])
            (source / "README.md").write_text("two\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-am", "two"])
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository pull",
                        "repository:",
                        f"  path: {target}",
                        f"  clone_url: {source}",
                        "  default_branch: main",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            with ThreadPoolExecutor(max_workers=6) as pool:
                repos = list(pool.map(lambda _: resolve_repository(cfg), range(12)))

            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "two\n")
            self.assertEqual({Path(repo["path"]).resolve() for repo in repos}, {target.resolve()})

    def test_resolve_repository_blocks_pull_when_checkout_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "checkout"
            source.mkdir(parents=True, exist_ok=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("one\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "one"])
            run_command(["git", "clone", str(source), str(target)])
            (target / "README.md").write_text("local edit\n", encoding="utf-8")
            (root / "agent.yaml").write_text(
                "\n".join(
                    [
                        "agent:",
                        "  name: ACA",
                        "tandem:",
                        "  base_url: http://127.0.0.1:39733",
                        "task_source:",
                        "  type: manual",
                        "  prompt: Repository pull",
                        "repository:",
                        f"  path: {target}",
                        f"  clone_url: {source}",
                        "provider:",
                        "  id: openai",
                        "  model: gpt-4.1-mini",
                        "output:",
                        "  root: runs",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)

            with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
                resolve_repository(cfg)


if __name__ == "__main__":
    unittest.main()
