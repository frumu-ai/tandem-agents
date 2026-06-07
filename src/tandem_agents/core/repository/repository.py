from __future__ import annotations

import base64
import os
import re
import shutil
import threading
from pathlib import Path

WORKTREE_LOCK = threading.Lock()
REPO_SYNC_LOCK = threading.Lock()
from typing import Any
from urllib.parse import urlparse

from src.tandem_agents.config.config import ResolvedConfig
from src.tandem_agents.core.engine.process_utils import run_command
from src.tandem_agents.utils.utils import slugify


def _github_pat(cfg: ResolvedConfig) -> str:
    def read_token_file(raw_path: str) -> str:
        path = str(raw_path or "").strip()
        if not path:
            return ""
        token_path = Path(path).expanduser()
        if not token_path.is_absolute():
            token_path = (cfg.root_dir / token_path).resolve()
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    return (
        read_token_file(cfg.repository.credential_file)
        or read_token_file(cfg.env.get("ACA_REPO_TOKEN_FILE") or "")
        or read_token_file(cfg.env.get("GITHUB_TOKEN_FILE") or "")
        or read_token_file(cfg.env.get("GITHUB_PERSONAL_ACCESS_TOKEN_FILE") or "")
        or (cfg.env.get("GITHUB_TOKEN") or "").strip()
        or (cfg.env.get("GITHUB_PERSONAL_ACCESS_TOKEN") or "").strip()
    )


def _is_github_clone_url(clone_url: str) -> bool:
    try:
        parsed = urlparse(clone_url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host == "github.com"


def _git_clone_args_and_env(cfg: ResolvedConfig, clone_url: str, target: Path) -> tuple[list[str], dict[str, str]]:
    env = dict(cfg.env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    args = ["git"]
    github_pat = _github_pat(cfg)
    if github_pat and _is_github_clone_url(clone_url):
        token = base64.b64encode(f"x-access-token:{github_pat}".encode("utf-8")).decode("ascii")
        args.extend(["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {token}"])
    args.extend(["clone", "--branch", cfg.repository.default_branch])
    args.extend([clone_url, str(target)])
    return args, env


def _git_auth_args(cfg: ResolvedConfig, clone_url: str) -> tuple[list[str], dict[str, str]]:
    env = dict(cfg.env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    args = ["git"]
    github_pat = _github_pat(cfg)
    if github_pat and _is_github_clone_url(clone_url):
        token = base64.b64encode(f"x-access-token:{github_pat}".encode("utf-8")).decode("ascii")
        args.extend(["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {token}"])
    return args, env


def _git_identity_args(cfg: ResolvedConfig) -> list[str]:
    name = (cfg.env.get("GIT_AUTHOR_NAME") or cfg.env.get("GIT_COMMITTER_NAME") or "ACA").strip()
    email = (
        cfg.env.get("GIT_AUTHOR_EMAIL")
        or cfg.env.get("GIT_COMMITTER_EMAIL")
        or "tandem-agents.invalid"
    ).strip()
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def _git_repo_args(repo_path: Path, *args: str, prefix: list[str] | None = None) -> list[str]:
    base = list(prefix or ["git"])
    return [
        *base,
        "-c",
        f"safe.directory={repo_path}",
        "-C",
        str(repo_path),
        *args,
    ]


def _remote_is_empty(cfg: ResolvedConfig, clone_url: str) -> bool:
    args, env = _git_auth_args(cfg, clone_url)
    result = run_command(args + ["ls-remote", clone_url], env=env)
    return result.returncode == 0 and not result.stdout.strip()


def _remote_url_for_existing_repo(cfg: ResolvedConfig, repo_path: Path) -> str:
    configured = str(cfg.repository.clone_url or "").strip()
    if configured:
        return configured
    remote_name = cfg.repository.remote_name or "origin"
    result = run_command(_git_repo_args(repo_path, "remote", "get-url", remote_name), env=cfg.env)
    return result.stdout.strip() if result.returncode == 0 else ""


def _configured_clone_url(cfg: ResolvedConfig) -> str:
    clone_url = str(cfg.repository.clone_url or "").strip()
    if clone_url:
        return clone_url
    slug = str(cfg.repository.slug or "").strip().strip("/")
    if "/" in slug:
        return f"https://github.com/{slug}.git"
    return ""


def _sync_existing_repository(cfg: ResolvedConfig, repo_path: Path) -> None:
    status = run_command(_git_repo_args(repo_path, "status", "--porcelain"), env=cfg.env)
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or status.stdout.strip())
    if status.stdout.strip():
        raise RuntimeError(
            f"Repository has uncommitted changes and ACA will not pull over them: {repo_path}"
        )

    remote_name = cfg.repository.remote_name or "origin"
    default_branch = cfg.repository.default_branch or "main"
    remote_url = _remote_url_for_existing_repo(cfg, repo_path)
    if not remote_url:
        return
    args, env = _git_auth_args(cfg, remote_url)

    fetch_result = run_command(
        _git_repo_args(repo_path, "fetch", "--prune", remote_name, default_branch, prefix=args),
        env=env,
    )
    if fetch_result.returncode != 0:
        raise RuntimeError(fetch_result.stderr.strip() or fetch_result.stdout.strip())

    checkout_result = run_command(_git_repo_args(repo_path, "checkout", default_branch), env=env)
    if checkout_result.returncode != 0:
        raise RuntimeError(checkout_result.stderr.strip() or checkout_result.stdout.strip())

    pull_result = run_command(
        _git_repo_args(repo_path, "pull", "--ff-only", remote_name, default_branch, prefix=args),
        env=env,
    )
    if pull_result.returncode != 0:
        raise RuntimeError(pull_result.stderr.strip() or pull_result.stdout.strip())


def _bootstrap_local_repository(cfg: ResolvedConfig, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    identity_args = _git_identity_args(cfg)
    init_result = run_command(
        [
            "git",
            *identity_args,
            "init",
            "--initial-branch",
            cfg.repository.default_branch,
            str(target),
        ],
        env={"GIT_TERMINAL_PROMPT": "0", **cfg.env},
    )
    if init_result.returncode != 0:
        raise RuntimeError(init_result.stderr.strip() or init_result.stdout.strip())

    add_result = run_command(_git_repo_args(target, "add", "-A"), env=cfg.env)
    if add_result.returncode != 0:
        raise RuntimeError(add_result.stderr.strip() or add_result.stdout.strip())

    commit_result = run_command(
        [
            "git",
            "-c",
            f"safe.directory={target}",
            "-C",
            str(target),
            *identity_args,
            "commit",
            "--allow-empty",
            "-m",
            "chore: initialize local workspace",
        ],
        env={"GIT_TERMINAL_PROMPT": "0", **cfg.env},
    )
    if commit_result.returncode != 0:
        raise RuntimeError(commit_result.stderr.strip() or commit_result.stdout.strip())
    return target.resolve()


def _bootstrap_empty_repository(cfg: ResolvedConfig, clone_url: str, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    identity_args = _git_identity_args(cfg)
    init_result = run_command(
        ["git", *identity_args, "init", "--initial-branch", cfg.repository.default_branch, str(target)],
        env={"GIT_TERMINAL_PROMPT": "0", **cfg.env},
    )
    if init_result.returncode != 0:
        raise RuntimeError(init_result.stderr.strip() or init_result.stdout.strip())
    remote_name = cfg.repository.remote_name or "origin"
    remote_result = run_command(_git_repo_args(target, "remote", "add", remote_name, clone_url), env=cfg.env)
    if remote_result.returncode != 0 and "already exists" not in (remote_result.stderr or ""):
        raise RuntimeError(remote_result.stderr.strip() or remote_result.stdout.strip())
    commit_result = run_command(
        [
            "git",
            "-c",
            f"safe.directory={target}",
            "-C",
            str(target),
            *identity_args,
            "commit",
            "--allow-empty",
            "-m",
            "chore: initialize repository",
        ],
        env={"GIT_TERMINAL_PROMPT": "0", **cfg.env},
    )
    if commit_result.returncode != 0:
        raise RuntimeError(commit_result.stderr.strip() or commit_result.stdout.strip())
    return target.resolve()


def _sanitize_repo_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "repo"


def _repo_target_name(cfg: ResolvedConfig) -> str:
    if cfg.repository.slug:
        return cfg.repository.slug.replace("/", "__")
    if cfg.repository.clone_url:
        parsed = urlparse(cfg.repository.clone_url)
        return _sanitize_repo_name(Path(parsed.path).stem or "repo")
    return "repo"


def _clone_url_to_slug(clone_url: str) -> str:
    text = str(clone_url or "").strip()
    if not text:
        return ""
    path = ""
    if text.startswith("git@github.com:"):
        path = text.split("git@github.com:", 1)[1]
    else:
        try:
            parsed = urlparse(text)
        except Exception:
            parsed = None
        if parsed and (parsed.hostname or "").lower() == "github.com":
            path = parsed.path or ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return ""


def repository_binding_issues(cfg: ResolvedConfig) -> list[str]:
    issues: list[str] = []
    repo_path = cfg.repository_path()
    clone_url = _configured_clone_url(cfg)
    slug = str(cfg.repository.slug or "").strip()
    credential_file = str(cfg.repository.credential_file or "").strip()
    if repo_path is not None:
        if repo_path.exists():
            if repo_path.is_file():
                issues.append(f"repository.path points to a file, not a directory: {repo_path}")
            else:
                git_dir = repo_path / ".git"
                if not git_dir.exists():
                    has_files = any(repo_path.iterdir())
                    if has_files and clone_url:
                        issues.append(
                            f"repository.path exists but is not a git checkout and is not an empty clone target: {repo_path}"
                        )
                elif slug:
                    remote_name = cfg.repository.remote_name or "origin"
                    remote_result = run_command(_git_repo_args(repo_path, "remote", "get-url", remote_name), env=cfg.env)
                    remote_slug = _clone_url_to_slug(remote_result.stdout.strip()) if remote_result.returncode == 0 else ""
                    if remote_slug and remote_slug != slug:
                        issues.append(
                            f"repository.path remote `{remote_slug}` does not match configured slug `{slug}`."
                        )
        elif not (clone_url or slug):
            issues.append(
                f"repository.path points to a missing location and no clone source is configured: {repo_path}"
            )
    if slug and clone_url:
        clone_slug = _clone_url_to_slug(clone_url)
        if clone_slug and clone_slug != slug:
            issues.append(
                f"repository.slug `{slug}` does not match repository.clone_url `{clone_url}`."
            )
    if credential_file:
        credential_path = Path(credential_file).expanduser()
        if not credential_path.is_absolute():
            credential_path = (cfg.root_dir / credential_path).resolve()
        if not credential_path.exists():
            issues.append(f"repository.credential_file does not exist: {credential_path}")
    return issues


def task_run_branch_name(task: dict[str, Any], run_id: str, repo_slug: str = "") -> str:
    task_title = str(task.get("title") or task.get("task_id") or "task").strip()
    task_id = str(task.get("task_id") or run_id or "run").strip()
    repo_part = slugify(repo_slug.replace("/", "-"), limit=28) if repo_slug else ""
    task_part = slugify(task_title, limit=32)
    task_id_part = slugify(task_id, limit=16)
    run_part = slugify(run_id, limit=16)
    tail = f"{task_part}-{task_id_part}-{run_part}"
    if repo_part:
        return f"aca/{repo_part}/{tail}"
    return f"aca/{tail}"


def worker_worktree_name(worker_id: str, subtask_id: str | None = None) -> str:
    worker_part = slugify(worker_id or "worker", limit=32)
    if subtask_id:
        subtask_part = slugify(subtask_id, limit=32)
        return f"{worker_part}--{subtask_part}"
    return worker_part


def _resolve_repository_unlocked(cfg: ResolvedConfig) -> dict[str, Any]:
    repo_path_hint = cfg.repository_path()
    if repo_path_hint:
        clone_url = _configured_clone_url(cfg)
        if repo_path_hint.exists():
            if (repo_path_hint / ".git").exists():
                repo_path = repo_path_hint.resolve()
                _sync_existing_repository(cfg, repo_path)
            elif repo_path_hint.is_dir() and not any(repo_path_hint.iterdir()):
                if clone_url:
                    args, env = _git_clone_args_and_env(cfg, clone_url, repo_path_hint)
                    result = run_command(args, env=env)
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                    repo_path = repo_path_hint.resolve()
                else:
                    repo_path = _bootstrap_local_repository(cfg, repo_path_hint)
            elif repo_path_hint.is_dir() and not clone_url:
                repo_path = _bootstrap_local_repository(cfg, repo_path_hint)
            else:
                raise RuntimeError(
                    f"Configured repository.path is not a git checkout and is not a safe clone target: {repo_path_hint}"
                )
        else:
            if clone_url:
                args, env = _git_clone_args_and_env(cfg, clone_url, repo_path_hint)
                result = run_command(args, env=env)
                if result.returncode != 0:
                    if _remote_is_empty(cfg, clone_url):
                        repo_path = _bootstrap_empty_repository(cfg, clone_url, repo_path_hint)
                    else:
                        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                else:
                    repo_path = repo_path_hint.resolve()
            else:
                repo_path = _bootstrap_local_repository(cfg, repo_path_hint)
    else:
        worktree_root = cfg.repository_worktree_root()
        worktree_root.mkdir(parents=True, exist_ok=True)
        target = worktree_root / _repo_target_name(cfg)
        if (target / ".git").exists():
            repo_path = target.resolve()
            _sync_existing_repository(cfg, repo_path)
        else:
            clone_url = _configured_clone_url(cfg)
            if not clone_url:
                raise RuntimeError("No repository binding available")
            args, env = _git_clone_args_and_env(cfg, clone_url, target)
            result = run_command(args, env=env)
            if result.returncode != 0:
                if _remote_is_empty(cfg, clone_url):
                    repo_path = _bootstrap_empty_repository(cfg, clone_url, target)
                else:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            else:
                repo_path = target.resolve()

    if not (repo_path / ".git").exists():
        raise RuntimeError(f"Resolved repository is not a git checkout: {repo_path}")
    return repository_status(repo_path, cfg.repository.remote_name, cfg.repository.default_branch)


def resolve_repository(cfg: ResolvedConfig) -> dict[str, Any]:
    with REPO_SYNC_LOCK:
        return _resolve_repository_unlocked(cfg)


def checkout_run_branch(cfg: ResolvedConfig, repo_path: Path, branch_name: str) -> str:
    """Creates and checkouts a new branch for the run."""
    # Ensure we are on the default branch and it's clean
    run_command(_git_repo_args(repo_path, "checkout", cfg.repository.default_branch), env=cfg.env)
    
    # Create and checkout new branch
    result = run_command(_git_repo_args(repo_path, "checkout", "-b", branch_name), env=cfg.env)
    if result.returncode != 0:
        # If branch exists, just checkout
        run_command(_git_repo_args(repo_path, "checkout", branch_name), env=cfg.env)
    
    return branch_name


def push_repository_changes(cfg: ResolvedConfig, repo_path: Path, branch_name: str) -> bool:
    """Pushes the current branch to the remote."""
    remote_name = cfg.repository.remote_name or "origin"
    result = run_command(_git_repo_args(repo_path, "push", "-u", remote_name, branch_name), env=cfg.env)
    return result.returncode == 0


def repository_status(repo_path: Path, remote_name: str = "origin", default_branch: str = "main") -> dict[str, Any]:
    branch = run_command(_git_repo_args(repo_path, "rev-parse", "--abbrev-ref", "HEAD"))
    commit = run_command(_git_repo_args(repo_path, "rev-parse", "HEAD"))
    status = run_command(_git_repo_args(repo_path, "status", "--porcelain"))
    remote = run_command(_git_repo_args(repo_path, "remote", "-v"))
    return {
        "path": str(repo_path.resolve()),
        "remote_name": remote_name,
        "default_branch": default_branch,
        "branch": branch.stdout.strip() or None,
        "commit": commit.stdout.strip() or None,
        "dirty": bool(status.stdout.strip()),
        "worktree_root": str(repo_path.parent.resolve()),
        "remote": remote.stdout.strip() or None,
    }


def create_worktree(repo_path: Path, worktree_path: Path) -> Path:
    with WORKTREE_LOCK:
        run_command(_git_repo_args(repo_path, "worktree", "prune"))
        if worktree_path.exists():
            remove_result = run_command(
                _git_repo_args(repo_path, "worktree", "remove", "--force", str(worktree_path))
            )
            if remove_result.returncode != 0 and worktree_path.exists():
                shutil.rmtree(worktree_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(
            _git_repo_args(
                repo_path,
                "worktree",
                "add",
                "--detach",
                "--force",
                str(worktree_path),
                "HEAD",
            )
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return worktree_path.resolve()


def _host_path_for_git_metadata(path: Path) -> Path:
    """Map container absolute git metadata paths back to the ACA host checkout.

    ACA runs inside a container at /workspace/tandem-agents, while the host-side
    API process may inspect the same run tree at cfg.root_dir. Git worktree
    .git files created in the container can therefore contain gitdir paths that
    are invalid from the host process.
    """
    root_dir = Path(os.environ.get("ACA_ROOT") or ".").resolve()
    text = str(path)
    prefix = "/workspace/tandem-agents/"
    if text.startswith(prefix):
        return root_dir / text[len(prefix):]
    if text == "/workspace/tandem-agents":
        return root_dir
    return path


def _git_dir_for_worktree(repo_path: Path) -> Path | None:
    git_file = repo_path / ".git"
    try:
        text = git_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    raw_git_dir = text.split(":", 1)[1].strip()
    if not raw_git_dir:
        return None
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = (repo_path / git_dir).resolve()
    return _host_path_for_git_metadata(git_dir)


def _git_command_for_worktree(repo_path: Path, *args: str) -> list[str]:
    git_dir = _git_dir_for_worktree(repo_path)
    if git_dir is None or not git_dir.exists():
        return _git_repo_args(repo_path, *args)
    return ["git", "-c", f"safe.directory={repo_path}", f"--git-dir={git_dir}", f"--work-tree={repo_path}", *args]


def _is_internal_worktree_artifact(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").strip("/")
    return normalized == ".aca" or normalized.startswith(".aca/")


def _status_path(raw_line: str) -> str:
    path_text = raw_line[3:].strip()
    if "->" in path_text:
        path_text = path_text.split("->", 1)[1].strip()
    return path_text


def git_diff_stat(repo_path: Path) -> str:
    result = run_command(_git_command_for_worktree(repo_path, "status", "--short", "--untracked-files=all"))
    lines = [
        raw_line
        for raw_line in result.stdout.splitlines()
        if raw_line.strip() and not _is_internal_worktree_artifact(_status_path(raw_line))
    ]
    return "\n".join(lines).strip()


def git_working_diff(repo_path: Path, *, max_chars: int = 20000, max_file_chars: int = 4000) -> str:
    """Return a best-effort unified diff of uncommitted working-tree changes.

    Includes modified/deleted tracked files (via ``git diff HEAD``) and the
    contents of new untracked files, so reviewers and testers can see what
    actually changed instead of only a status summary.

    This is read-only: it never mutates the index or working tree. The output is
    truncated per file (``max_file_chars``) and overall (``max_chars``) so it
    stays within prompt budgets.
    """
    sections: list[str] = []
    tracked = run_command(_git_command_for_worktree(repo_path, "diff", "HEAD"))
    tracked_text = tracked.stdout.strip()
    if tracked_text:
        sections.append(tracked_text)
    try:
        changes = list_worktree_changes(repo_path)
    except Exception:
        changes = []
    for change in changes:
        if not change["status"].strip().startswith("?"):
            continue
        rel_path = change["path"]
        file_path = repo_path / rel_path
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content) > max_file_chars:
            content = content[:max_file_chars] + "\n... (file truncated)\n"
        sections.append(f"new file: {rel_path}\n{content}")
    diff_text = "\n\n".join(section for section in sections if section).strip()
    if len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars] + "\n... (diff truncated)\n"
    return diff_text


def list_worktree_changes(worktree_path: Path) -> list[dict[str, str]]:
    result = run_command(
        _git_command_for_worktree(worktree_path, "status", "--porcelain", "--untracked-files=all")
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    changes: list[dict[str, str]] = []
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        status = raw_line[:2]
        path_text = _status_path(raw_line)
        if not path_text:
            continue
        if _is_internal_worktree_artifact(path_text):
            continue
        changes.append({"status": status, "path": path_text})
    return changes


def sync_worktree_changes(worktree_path: Path, repo_path: Path) -> list[str]:
    copied: list[str] = []
    with REPO_SYNC_LOCK:
        for change in list_worktree_changes(worktree_path):
            rel_path = change["path"]
            source = worktree_path / rel_path
            target = repo_path / rel_path
            status = change["status"]
            if "D" in status:
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                copied.append(rel_path)
                continue
            if not source.exists():
                continue
            if source.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(rel_path)
    return copied


def commit_repository_changes(cfg: ResolvedConfig, repo_path: Path, message: str) -> dict[str, Any] | None:
    if not git_diff_stat(repo_path).strip():
        return None
    env = {"GIT_TERMINAL_PROMPT": "0", **cfg.env}
    add_result = run_command(_git_repo_args(repo_path, "add", "-A"), env=env)
    if add_result.returncode != 0:
        raise RuntimeError(add_result.stderr.strip() or add_result.stdout.strip())
    commit_result = run_command(
        _git_repo_args(repo_path, *_git_identity_args(cfg), "commit", "-m", message),
        env=env,
    )
    if commit_result.returncode != 0:
        stderr = (commit_result.stderr or "").strip()
        stdout = (commit_result.stdout or "").strip()
        combined = f"{stdout}\n{stderr}".strip()
        if "nothing to commit" in combined.lower():
            return None
        raise RuntimeError(stderr or stdout)
    head_result = run_command(_git_repo_args(repo_path, "rev-parse", "HEAD"), env=env)
    if head_result.returncode != 0:
        raise RuntimeError(head_result.stderr.strip() or head_result.stdout.strip())
    return {
        "commit": head_result.stdout.strip(),
        "message": message,
    }
