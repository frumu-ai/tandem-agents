from __future__ import annotations

import unittest
from unittest import mock
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.engine.process_utils import run_command
import os

from src.tandem_agents.config.config_loader import resolve_config as _resolve_config
from src.tandem_agents.core.repository.repository import (
    _git_clone_args_and_env,
    _git_repo_args,
    _github_pat,
    checkout_run_branch,
    create_worktree,
    current_repository_branch,
    fetch_pr_refs,
    git_diff_stat,
    git_working_diff,
    list_worktree_changes,
    pr_head_ref,
    push_repository_changes,
    repository_binding_issues,
    resolve_repository,
    task_run_branch_name,
    worker_worktree_name,
)


def _config_for_repo(root: Path, repo_path: Path):
    (root / "agent.yaml").write_text(
        "\n".join(
            [
                "agent:",
                "  name: ACA",
                "tandem:",
                "  base_url: http://127.0.0.1:39733",
                "task_source:",
                "  type: manual",
                "  prompt: x",
                "repository:",
                f"  path: {repo_path}",
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
    return _resolve_config(root, env=dict(os.environ))


class RepositoryNamingTest(unittest.TestCase):
    def test_git_repo_args_marks_managed_checkout_safe(self) -> None:
        repo_path = Path("/workspace/tandem-agents/workspace/repos/tandem")

        args = _git_repo_args(repo_path, "status", "--porcelain")

        self.assertEqual(
            args,
            [
                "git",
                "-c",
                "safe.directory=/workspace/tandem-agents/workspace/repos/tandem",
                "-C",
                "/workspace/tandem-agents/workspace/repos/tandem",
                "status",
                "--porcelain",
            ],
        )

    def test_git_diff_stat_maps_container_worktree_gitdir_to_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "workspace" / "repos" / "demo"
            source.mkdir(parents=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("before\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"])
            worktree = root / "runs" / "run-1" / "worktrees" / "worker-1"
            run_command(["git", "-C", str(source), "worktree", "add", "--detach", str(worktree), "HEAD"])
            git_file = worktree / ".git"
            host_gitdir = git_file.read_text(encoding="utf-8").split(":", 1)[1].strip()
            container_gitdir = str(Path(host_gitdir)).replace(str(root), "/workspace/tandem-agents")
            git_file.write_text(f"gitdir: {container_gitdir}\n", encoding="utf-8")
            (worktree / "README.md").write_text("after\n", encoding="utf-8")

            with mock.patch.dict("os.environ", {"ACA_ROOT": str(root)}):
                self.assertIn("README.md", git_diff_stat(worktree))

    def test_create_worktree_gitdir_is_visible_to_engine_and_aca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aca_root = root / "aca"
            host_root = root / "host"
            aca_root.mkdir()
            host_root.symlink_to(aca_root, target_is_directory=True)
            source = aca_root / "workspace" / "repos" / "demo"
            source.mkdir(parents=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("before\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"])
            worktree = aca_root / "runs" / "run-1" / "worktrees" / "worker-1"

            with mock.patch.dict(
                "os.environ",
                {
                    "ACA_ROOT": str(aca_root),
                    "ACA_ENGINE_HOST_ROOT": str(host_root),
                },
            ):
                create_worktree(source, worktree)
                git_file_text = (worktree / ".git").read_text(encoding="utf-8")
                self.assertIn(str(host_root), git_file_text)
                (worktree / "README.md").write_text("after\n", encoding="utf-8")
                self.assertIn("README.md", git_diff_stat(worktree))

            host_worktree = host_root / "runs" / "run-1" / "worktrees" / "worker-1"
            host_status = run_command(["git", "-C", str(host_worktree), "status", "--short"])
            self.assertEqual(host_status.returncode, 0, host_status.stderr)
            self.assertIn("README.md", host_status.stdout)

    def test_git_diff_stat_ignores_aca_internal_context_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            run_command(["git", "init", "--initial-branch=main", str(repo)])
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"])
            (repo / ".aca").mkdir()
            (repo / ".aca" / "pr_candidate_context.json").write_text("{}\n", encoding="utf-8")

            self.assertEqual(git_diff_stat(repo), "")
            self.assertEqual(list_worktree_changes(repo), [])

    def test_git_working_diff_includes_modified_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "repo"
            source.mkdir(parents=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("before\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"])

            # Modify a tracked file and add a brand-new untracked file.
            (source / "README.md").write_text("after\n", encoding="utf-8")
            (source / "new_module.py").write_text("print('hello')\n", encoding="utf-8")

            diff = git_working_diff(source)

            self.assertIn("README.md", diff)
            self.assertIn("after", diff)
            self.assertIn("new file: new_module.py", diff)
            self.assertIn("print('hello')", diff)

    def test_git_working_diff_truncates_to_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "repo"
            source.mkdir(parents=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "big.txt").write_text("x" * 10000, encoding="utf-8")

            diff = git_working_diff(source, max_chars=500, max_file_chars=200)

            self.assertLessEqual(len(diff), 600)
            self.assertIn("truncated", diff)

    def test_git_working_diff_empty_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "repo"
            source.mkdir(parents=True)
            run_command(["git", "init", "--initial-branch=main", str(source)])
            (source / "README.md").write_text("hello\n", encoding="utf-8")
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(source), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"])

            self.assertEqual(git_working_diff(source), "")

    def test_pr_head_ref_is_namespaced(self) -> None:
        self.assertEqual(pr_head_ref(7), "refs/aca/pr-7")

    def test_fetch_pr_refs_fetches_pull_head_into_local_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ident = ["-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid"]
            bare = root / "remote.git"
            run_command(["git", "init", "--bare", "--initial-branch=main", str(bare)])
            work = root / "work"
            run_command(["git", "clone", str(bare), str(work)])
            (work / "README.md").write_text("base\n", encoding="utf-8")
            run_command(["git", "-C", str(work), *ident, "add", "README.md"])
            run_command(["git", "-C", str(work), *ident, "commit", "-m", "base"])
            run_command(["git", "-C", str(work), "push", "origin", "main"])
            # Publish a PR-like commit as refs/pull/1/head on the remote.
            run_command(["git", "-C", str(work), "checkout", "-b", "feature"])
            (work / "feature.txt").write_text("from pr\n", encoding="utf-8")
            run_command(["git", "-C", str(work), *ident, "add", "feature.txt"])
            run_command(["git", "-C", str(work), *ident, "commit", "-m", "pr work"])
            run_command(["git", "-C", str(work), "push", "origin", "feature:refs/pull/1/head"])
            # Fresh consumer checkout (as ACA's repo); refs/pull/* are not cloned by default.
            checkout = root / "checkout"
            run_command(["git", "clone", str(bare), str(checkout)])

            cfg = _config_for_repo(root, checkout)
            results = fetch_pr_refs(cfg, checkout, [1])

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["ok"], results[0])
            self.assertEqual(results[0]["ref"], "refs/aca/pr-1")
            show = run_command(["git", "-C", str(checkout), "show", "--stat", "refs/aca/pr-1"])
            self.assertIn("feature.txt", show.stdout)

    def test_fetch_pr_refs_reports_missing_pr_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ident = ["-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid"]
            bare = root / "remote.git"
            run_command(["git", "init", "--bare", "--initial-branch=main", str(bare)])
            work = root / "work"
            run_command(["git", "clone", str(bare), str(work)])
            (work / "README.md").write_text("base\n", encoding="utf-8")
            run_command(["git", "-C", str(work), *ident, "add", "README.md"])
            run_command(["git", "-C", str(work), *ident, "commit", "-m", "base"])
            run_command(["git", "-C", str(work), "push", "origin", "main"])
            checkout = root / "checkout"
            run_command(["git", "clone", str(bare), str(checkout)])

            cfg = _config_for_repo(root, checkout)
            results = fetch_pr_refs(cfg, checkout, [999])

            self.assertEqual(len(results), 1)
            self.assertFalse(results[0]["ok"])
            self.assertTrue(results[0]["error"])

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

    def test_checkout_run_branch_fails_when_default_checkout_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "checkout"
            repo.mkdir()
            run_command(["git", "init", "--initial-branch=main", str(repo)])
            (repo / "README.md").write_text("one\n", encoding="utf-8")
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "one"])
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            cfg = _config_for_repo(root, repo)

            with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
                checkout_run_branch(cfg, repo, "aca/test-run")

            self.assertEqual(current_repository_branch(repo, cfg=cfg), "main")

    def test_push_repository_changes_refuses_wrong_current_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "checkout"
            repo.mkdir()
            run_command(["git", "init", "--initial-branch=main", str(repo)])
            (repo / "README.md").write_text("one\n", encoding="utf-8")
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "add", "README.md"])
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "one"])
            cfg = _config_for_repo(root, repo)

            self.assertFalse(push_repository_changes(cfg, repo, "aca/test-run"))


if __name__ == "__main__":
    unittest.main()
