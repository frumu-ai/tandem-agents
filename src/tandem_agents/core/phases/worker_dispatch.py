"""phases/worker_dispatch.py -- Local worker pool execution and result collection.

This module owns the worker dispatch phase:
1. Register workers with the coordination store
2. Spin up a heartbeat thread to keep leases alive during execution
3. Execute the local ThreadPoolExecutor worker pool via ``_execute_local_worker_pool``
4. Collect results and apply tolerated-failure logic
5. Clean up stale worker registrations on completion

All worker state is written into the RunContext. No return value — the
caller continues with ctx.worker_results after this returns.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any

from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.engine.engine import delete_tandem_session
from src.tandem_agents.core.engine.tandem_client_sdk import sdk_session_messages
from src.tandem_agents.core.repository.repository import sync_worktree_changes, worker_worktree_name
from src.tandem_agents.runtime.runstate import append_event
from src.tandem_agents.utils.utils import atomic_write_json

logger = logging.getLogger("aca.phases.worker_dispatch")


def _mark_cancelled_worker_attempt(ctx: RunContext, worker_id: str, execution_id: str) -> None:
    worker_id = str(worker_id or "").strip()
    execution_id = str(execution_id or "").strip()
    if not worker_id or not execution_id:
        return
    path = ctx.run_dir / "cancelled_worker_attempts.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    values = loaded.get(worker_id)
    if isinstance(values, str):
        attempts = [values]
    elif isinstance(values, list):
        attempts = [str(value).strip() for value in values if str(value).strip()]
    else:
        attempts = []
    if execution_id not in attempts:
        attempts.append(execution_id)
    loaded[worker_id] = attempts[-20:]
    atomic_write_json(path, loaded)


def _clear_active_worker_attempt_marker(ctx: RunContext, worker_id: str) -> None:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return
    path = ctx.run_dir / "active_worker_attempts.json"
    if not path.exists():
        return
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(loaded, dict) or worker_id not in loaded:
        return
    _mark_cancelled_worker_attempt(ctx, worker_id, str(loaded.get(worker_id) or "").strip())
    loaded.pop(worker_id, None)
    if loaded:
        atomic_write_json(path, loaded)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _active_worker_engine_sessions_path(ctx: RunContext) -> Path:
    return ctx.run_dir / "active_worker_engine_sessions.json"


def _load_active_worker_engine_sessions(ctx: RunContext) -> dict[str, dict[str, Any]]:
    path = _active_worker_engine_sessions_path(ctx)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    sessions: dict[str, dict[str, Any]] = {}
    for raw_worker_id, raw_info in loaded.items():
        worker_id = str(raw_worker_id or "").strip()
        if not worker_id or not isinstance(raw_info, dict):
            continue
        session_id = str(raw_info.get("session_id") or "").strip()
        if not session_id:
            continue
        sessions[worker_id] = dict(raw_info)
        sessions[worker_id]["session_id"] = session_id
    return sessions


def _pop_active_worker_engine_session(ctx: RunContext, worker_id: str) -> dict[str, Any]:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return {}
    path = _active_worker_engine_sessions_path(ctx)
    if not path.exists():
        return {}
    sessions = _load_active_worker_engine_sessions(ctx)
    info = dict(sessions.pop(worker_id, {}) or {})
    if sessions:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, sessions)
        except OSError:
            logger.debug("Could not update active worker engine session marker %s", path, exc_info=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Could not remove active worker engine session marker %s", path, exc_info=True)
    return info


def _mark_active_worker_engine_session_cleanup_failed(
    ctx: RunContext,
    worker_id: str,
    info: dict[str, Any],
    error: str,
) -> None:
    worker_id = str(worker_id or "").strip()
    session_id = str(info.get("session_id") or "").strip()
    if not worker_id or not session_id:
        return
    path = _active_worker_engine_sessions_path(ctx)
    sessions = _load_active_worker_engine_sessions(ctx)
    current = dict(sessions.get(worker_id) or info)
    current["session_id"] = session_id
    current["cleanup_failed_at_ms"] = int(time.time() * 1000)
    current["cleanup_error"] = str(error or "session_delete_failed")[:500]
    sessions[worker_id] = current
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, sessions)
    except OSError:
        logger.debug("Could not record worker engine session cleanup failure in %s", path, exc_info=True)


def _cancel_active_worker_engine_session(ctx: RunContext, worker_id: str, reason: str) -> None:
    _clear_active_worker_attempt_marker(ctx, worker_id)
    info = dict(_load_active_worker_engine_sessions(ctx).get(str(worker_id or "").strip()) or {})
    session_id = str(info.get("session_id") or "").strip()
    if not session_id:
        return
    run_id = str(info.get("run_id") or "").strip()
    reason = str(reason or "worker_cancelled").strip() or "worker_cancelled"
    append_event(
        ctx.layout["events"],
        "worker.engine_cancel_requested",
        ctx.run_id,
        {
            "worker_id": worker_id,
            "session_id": session_id,
            "engine_run_id": run_id,
            "reason": reason,
        },
        task_id=ctx.task.get("task_id"),
        role="worker",
        repo={"path": ctx.repo.get("path")},
    )

    def _delete() -> None:
        try:
            delete_tandem_session(ctx.cfg, session_id)
            _pop_active_worker_engine_session(ctx, worker_id)
            append_event(
                ctx.layout["events"],
                "worker.engine_cancelled",
                ctx.run_id,
                {
                    "worker_id": worker_id,
                    "session_id": session_id,
                    "engine_run_id": run_id,
                    "reason": reason,
                },
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
        except Exception as exc:
            _mark_active_worker_engine_session_cleanup_failed(ctx, worker_id, info, str(exc))
            append_event(
                ctx.layout["events"],
                "worker.engine_cancel_failed",
                ctx.run_id,
                {
                    "worker_id": worker_id,
                    "session_id": session_id,
                    "engine_run_id": run_id,
                    "reason": reason,
                    "error": str(exc)[:500],
                },
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )

    thread = threading.Thread(
        target=_delete,
        name=f"aca-cancel-engine-session-{worker_id}",
        daemon=True,
    )
    thread.start()


_TERMINAL_WORKER_BLOCKER_KINDS = {
    "approval_failed",
    "github_context_unavailable",
    "unsupported_task",
    "worker_corrupt_diff",
    "worker_off_track",
    "worker_runaway_diff",
    "worker_unproductive_diff",
    "worker_no_progress",
    "worker_incomplete_diff",
    "worker_reported_blocker",
    "worker_no_diff",
}

_UNPRODUCTIVE_DIFF_MARKERS = (
    "TODO(worker-blocker)",
    "panic!(\"blocked:",
    "panic!('blocked:",
    "blocked: production-path regression coverage",
    "production-path regression coverage was not added or verified",
)


def _diff_apply_check(worktree: Path, diff_text: str) -> tuple[bool, str]:
    if not str(diff_text or "").strip():
        return False, "empty diff"
    with tempfile.TemporaryDirectory(prefix="aca-progress-diff-index-") as temp_dir:
        index_path = str(Path(temp_dir) / "index")
        env = {**os.environ, "GIT_INDEX_FILE": index_path}
        read_tree = subprocess.run(
            ["git", "-C", str(worktree), "read-tree", "HEAD"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if read_tree.returncode != 0:
            detail = (read_tree.stderr or read_tree.stdout or "").strip()
            return False, detail or f"git read-tree failed with exit {read_tree.returncode}"
        check = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--cached", "--check", "--whitespace=nowarn"],
            input=diff_text,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode == 0:
            return True, ""
        detail = (check.stderr or check.stdout or "").strip()
        return False, detail or f"git apply --check failed with exit {check.returncode}"


def _diff_applies_to_head(worktree: Path, diff_text: str) -> bool:
    ok, _detail = _diff_apply_check(worktree, diff_text)
    return ok


def _abort_result_subtask_id(subtask: dict[str, Any] | None, worktree: Path | None = None) -> str:
    subtask_id = str((subtask or {}).get("id") or "").strip()
    if subtask_id:
        return subtask_id
    name = str(getattr(worktree, "name", "") or "").strip()
    if "--" not in name:
        return ""
    subtask_id = name.split("--", 1)[1]
    if "--exec-" in subtask_id:
        subtask_id = subtask_id.split("--exec-", 1)[0]
    return subtask_id.strip()


def _subtask_retry_metadata(subtask: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(subtask, dict):
        return {}

    def _paths(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        paths: list[str] = []
        for raw_path in value:
            rel_path = str(raw_path or "").strip()
            if rel_path and rel_path not in paths:
                paths.append(rel_path)
        return paths

    metadata: dict[str, Any] = {}
    files = _paths(subtask.get("files"))
    target_files = _paths(subtask.get("target_files"))
    if files:
        metadata["subtask_files"] = files
    if target_files:
        metadata["subtask_target_files"] = target_files
    return metadata


def _subtask_declared_change_files(subtask: dict[str, Any] | None) -> list[str]:
    if not isinstance(subtask, dict):
        return []
    paths: list[str] = []
    for field in ("target_files", "files"):
        value = subtask.get(field) or []
        if not isinstance(value, list):
            value = [value]
        for raw_path in value:
            path = _normalize_repo_path(raw_path)
            if path and path not in paths:
                paths.append(path)
    return paths


def _changed_files_scoped_to_subtask(
    changed_files: list[str],
    subtask: dict[str, Any] | None,
) -> list[str]:
    normalized = [_normalize_repo_path(path) for path in changed_files]
    normalized = [path for path in normalized if path]
    declared = set(_subtask_declared_change_files(subtask))
    if not declared:
        return normalized
    return [path for path in normalized if path in declared]


def _filter_diff_text_to_files(diff_text: str, changed_files: list[str]) -> str:
    allowed = {_normalize_repo_path(path) for path in changed_files if _normalize_repo_path(path)}
    if not allowed:
        return ""
    sections: list[str] = []
    current_lines: list[str] = []
    include_current = False

    def flush() -> None:
        nonlocal current_lines
        if include_current and current_lines:
            sections.append("".join(current_lines))
        current_lines = []

    for line in str(diff_text or "").splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            parts = line.split()
            path = ""
            if len(parts) >= 4:
                path = _normalize_repo_path(parts[3][2:] if parts[3].startswith("b/") else parts[3])
            include_current = path in allowed
            current_lines = [line] if include_current else []
            continue
        if include_current:
            current_lines.append(line)
    flush()
    return "".join(sections)


def _filter_result_partial_diff_artifact(
    result: dict[str, Any],
    changed_files: list[str],
) -> str:
    """Rewrite a preserved worker patch so repair carries only fresh files."""

    partial_diff_artifact = str(result.get("partial_diff_artifact") or "").strip()
    if not partial_diff_artifact and isinstance(result.get("artifacts"), dict):
        partial_diff_artifact = str(result["artifacts"].get("partial_diff") or "").strip()
    if not partial_diff_artifact:
        return ""
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
        result["artifacts"] = artifacts
    fresh_files = [_normalize_repo_path(path) for path in changed_files]
    fresh_files = [path for path in fresh_files if path]
    if not fresh_files:
        artifacts["original_partial_diff"] = partial_diff_artifact
        result["partial_diff_artifact"] = ""
        artifacts.pop("partial_diff", None)
        return ""
    source_path = Path(partial_diff_artifact)
    try:
        raw_text = source_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    filtered_diff = _filter_diff_text_to_files(raw_text, fresh_files)
    if not filtered_diff.strip():
        return ""
    target_path = source_path.with_name(f"{source_path.stem}.fresh-only{source_path.suffix}")
    status_rows = "\n".join(f"- {path}" for path in fresh_files)
    target_path.write_text(
        "# Partial worker diff filtered after overlay baseline accounting\n"
        "# Reason: terminal artifact included inherited overlay files from earlier workers\n\n"
        f"## changed files\n\n{status_rows}\n\n"
        f"## git diff --binary\n\n{filtered_diff}",
        encoding="utf-8",
    )
    artifacts["original_partial_diff"] = partial_diff_artifact
    artifacts["partial_diff"] = str(target_path)
    result["partial_diff_artifact"] = str(target_path)
    return str(target_path)


def _result_has_non_reusable_guarded_partial_diff(result: dict[str, Any]) -> bool:
    failure_reason = str(result.get("failure_reason") or "").strip()
    blocker_kind = str(result.get("blocker_kind") or "").strip()
    output_excerpt = str(result.get("output_excerpt") or result.get("stdout") or "").lower()
    return (
        failure_reason in {"WORKER_RUNAWAY_DIFF", "WORKER_DESTRUCTIVE_DIFF"}
        or blocker_kind == "worker_runaway_diff"
        or "runaway guard" in output_excerpt
        or "diff exceeded aca runaway" in output_excerpt
        or "destructive rewrite guard" in output_excerpt
    )


def _sync_verifiable_worker_diff(
    ctx: RunContext,
    *,
    worker_id: str,
    subtask_id: str,
    worktree: Path,
    changed_files: list[str],
) -> tuple[bool, list[str], list[str], str]:
    """Sync a source+test guard diff into the run checkout before review."""
    try:
        synced = sync_worktree_changes(worktree, ctx.repo_path)
    except Exception as exc:
        return False, [], [], str(exc)
    synced_files = [str(path) for path in synced if str(path).strip()]
    if not synced_files:
        return False, [], [], "no files were synced from the verifiable worker diff"
    merged = list(dict.fromkeys([*changed_files, *synced_files]))
    append_event(
        ctx.layout["events"],
        "worker.verifiable_diff_synced",
        ctx.run_id,
        {
            "worker_id": worker_id,
            "subtask_id": subtask_id,
            "changed_files": merged,
            "synced_files": synced_files,
        },
        task_id=ctx.task.get("task_id"),
        role="worker",
        repo={"path": ctx.repo.get("path")},
    )
    return True, merged, synced_files, ""


def _changed_python_syntax_errors(worktree: Path, changed_files: list[str]) -> list[str]:
    python_files = [
        str(path).replace("\\", "/")
        for path in changed_files
        if str(path or "").replace("\\", "/").endswith(".py")
        and (worktree / str(path).replace("\\", "/")).is_file()
    ]
    if not python_files:
        return []
    script = (
        "import ast, pathlib, sys\n"
        "errors = []\n"
        "for raw in sys.argv[1:]:\n"
        "    path = pathlib.Path(raw)\n"
        "    try:\n"
        "        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))\n"
        "    except SyntaxError as exc:\n"
        "        errors.append(f'{raw}:{exc.lineno}:{exc.offset}: {exc.msg}')\n"
        "    except Exception as exc:\n"
        "        errors.append(f'{raw}: {type(exc).__name__}: {exc}')\n"
        "if errors:\n"
        "    print('\\n'.join(errors))\n"
        "    raise SystemExit(1)\n"
    )
    result = subprocess.run(
        ["python3", "-c", script, *python_files],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return []
    output = (result.stdout or result.stderr or "").strip()
    return [line.strip() for line in output.splitlines() if line.strip()] or [
        f"python syntax check failed with exit {result.returncode}"
    ]


def _changed_python_test_modules(worktree: Path, changed_files: list[str]) -> list[str]:
    modules: list[str] = []
    seen: set[str] = set()
    for raw_path in changed_files:
        rel_path = str(raw_path or "").strip().replace("\\", "/")
        if not rel_path.endswith(".py") or not _is_test_path(rel_path):
            continue
        if rel_path.endswith("/__init__.py") or rel_path == "__init__.py":
            continue
        if not (worktree / rel_path).is_file():
            continue
        module = rel_path[:-3].replace("/", ".")
        if module and module not in seen:
            seen.add(module)
            modules.append(module)
    return modules


_FOCUSED_PYTHON_TEST_ENV_DENYLIST = {
    "ACA_REPO_PATH",
    "AUTOCODER_REPO_PATH",
    "ACA_REPO_SLUG",
    "AUTOCODER_REPO_SLUG",
    "ACA_REPO_URL",
    "AUTOCODER_REPO_URL",
    "ACA_WORKTREE_ROOT",
    "AUTOCODER_WORKTREE_ROOT",
    "ACA_REPO_ALLOWED_HOSTS",
    "ACA_REPO_TOKEN_FILE",
    "TANDEM_CONTROL_PANEL_CONFIG_FILE",
}


def _focused_python_test_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _FOCUSED_PYTHON_TEST_ENV_DENYLIST:
        env.pop(key, None)
    return env


def _changed_python_tests_result(worktree: Path, changed_files: list[str]) -> dict[str, Any] | None:
    modules = _changed_python_test_modules(worktree, changed_files)
    if not modules:
        return None
    command = ["python3", "-m", "unittest", *modules]
    try:
        result = subprocess.run(
            command,
            cwd=worktree,
            env=_focused_python_test_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in (exc.stdout, exc.stderr) if isinstance(part, str)).strip()
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "output": output or "changed Python tests timed out after 90s",
            "timed_out": True,
        }
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return {
        "ok": result.returncode == 0,
        "command": command,
        "returncode": result.returncode,
        "output": output,
        "timed_out": False,
    }


def _worktree_changed_files_diff(
    worktree: Path,
    changed_files: list[str],
    *,
    include_staged: bool = False,
) -> str:
    paths = [str(path or "").strip() for path in changed_files if str(path or "").strip()]
    command = ["git", "-C", str(worktree), "diff", "--binary"]
    if include_staged:
        command.append("HEAD")
    if paths:
        command.extend(["--", *paths])
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    return result.stdout if result.returncode == 0 else ""


def _reviewable_failed_diff_rejection(
    worktree: Path,
    subtask: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any] | None:
    required_test_files = _subtask_required_test_files(subtask)
    diff_text = _worktree_changed_files_diff(worktree, changed_files, include_staged=True)
    if diff_text and not _diff_has_substantive_required_test_addition(diff_text, required_test_files):
        return {
            "reason": "weak_test_diff",
            "message": "changed test files did not add a test method or assertion",
        }
    missing_symbols = _diff_required_tests_missing_added_public_symbols(diff_text, required_test_files)
    if missing_symbols:
        return {
            "reason": "misaligned_test_diff",
            "message": (
                "required test changes do not exercise newly introduced production symbol(s): "
                + ", ".join(missing_symbols[:8])
            ),
            "missing_symbols": missing_symbols,
        }
    test_result = _changed_python_tests_result(worktree, changed_files)
    if test_result is not None and not bool(test_result.get("ok")):
        return {
            "reason": "focused_tests_failed",
            "message": str(test_result.get("output") or "").strip()[:1000],
            "command": test_result.get("command"),
            "returncode": test_result.get("returncode"),
            "timed_out": bool(test_result.get("timed_out")),
        }
    return None


def _is_test_path(path: str) -> bool:
    lowered = str(path or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    return (
        lowered.startswith("tests/")
        or "/tests/" in f"/{lowered}"
        or lowered.endswith((
            "_test.rs",
            "_tests.rs",
            "_test.py",
            ".test.ts",
            ".test.tsx",
            ".spec.ts",
            ".spec.tsx",
        ))
    )


def _text_mentions_test_work(value: Any) -> bool:
    text = str(value or "").lower()
    return any(word in text for word in ("test", "tests", "coverage", "regression"))


def _subtask_required_test_files(subtask: dict[str, Any]) -> list[str]:
    files = subtask.get("files") or subtask.get("target_files") or []
    if not isinstance(files, list):
        files = [files]
    return [str(path).strip() for path in files if _is_test_path(str(path))]


def _changed_files_satisfy_required_test_files(
    changed_files: list[str],
    required_test_files: list[str],
) -> bool:
    changed = {str(path or "").strip().replace("\\", "/") for path in changed_files}
    required = {str(path or "").strip().replace("\\", "/") for path in required_test_files}
    if required:
        return bool(changed & required)
    return any(_is_test_path(path) for path in changed)


def _diff_has_substantive_required_test_addition(
    diff_text: str,
    required_test_files: list[str],
) -> bool:
    required = {str(path or "").strip().replace("\\", "/") for path in required_test_files if str(path or "").strip()}
    current_file = ""
    for line in str(diff_text or "").splitlines():
        if line.startswith("diff --git "):
            current_file = ""
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_file = parts[3][2:]
            continue
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        normalized_file = current_file.replace("\\", "/")
        if required:
            if normalized_file not in required:
                continue
        elif not _is_test_path(normalized_file):
            continue
        added = line[1:].strip()
        lowered = added.lower()
        if re.match(r"(async\s+)?def\s+test[_a-z0-9]*\s*\(", added):
            return True
        if "self.assert" in added or "assert " in lowered or lowered.startswith("assert"):
            return True
        if "pytest.raises" in added or "unittest.mock" in added and "assert" in lowered:
            return True
    return False


def _diff_required_tests_missing_added_public_symbols(
    diff_text: str,
    required_test_files: list[str],
) -> list[str]:
    required = {str(path or "").strip().replace("\\", "/") for path in required_test_files if str(path or "").strip()}
    current_file = ""
    production_symbols: list[str] = []
    production_seen: set[str] = set()
    added_test_body_lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if line.startswith("diff --git "):
            current_file = ""
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_file = parts[3][2:]
            continue
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        normalized_file = current_file.replace("\\", "/")
        added = line[1:]
        if normalized_file.endswith(".py") and not _is_test_path(normalized_file):
            match = re.match(r"(?:async\s+def|def|class)\s+([A-Za-z][A-Za-z0-9_]*)\b", added)
            if match:
                symbol = match.group(1)
                if not symbol.startswith("_") and symbol not in production_seen:
                    production_seen.add(symbol)
                    production_symbols.append(symbol)
            continue
        if required:
            if normalized_file not in required:
                continue
        elif not _is_test_path(normalized_file):
            continue
        stripped = added.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("import ", "from ")):
            continue
        added_test_body_lines.append(stripped)
    if not production_symbols:
        return []
    test_body = "\n".join(added_test_body_lines)
    if any(re.search(rf"\b{re.escape(symbol)}\b", test_body) for symbol in production_symbols):
        return []
    return production_symbols


def _subtask_requires_test_changes(subtask: dict[str, Any]) -> bool:
    if not _subtask_required_test_files(subtask):
        return False
    text_parts: list[Any] = [
        subtask.get("title"),
        subtask.get("goal"),
        subtask.get("scope_note"),
    ]
    for field in ("acceptance_criteria", "deliverables"):
        value = subtask.get(field)
        if isinstance(value, (list, tuple, set)):
            text_parts.extend(value)
        elif value:
            text_parts.append(value)
    return any(_text_mentions_test_work(part) for part in text_parts)


def _subtask_requires_paired_source_test_diff(subtask: dict[str, Any]) -> bool:
    if subtask.get("repair_requires_paired_source_test") or subtask.get("repair_requires_paired_source_test_diff"):
        return True
    if str(subtask.get("repair_mode") or "").strip() in {
        "complementary_rejected_partial_diff",
        "weak_source_test_diff",
    }:
        return True
    production_followups = [
        str(path or "").strip()
        for path in subtask.get("repair_requires_production_followup") or []
        if str(path or "").strip() and not _is_test_path(str(path or "").strip())
    ]
    test_followups = [
        str(path or "").strip()
        for path in subtask.get("repair_requires_test_followup") or []
        if str(path or "").strip() and _is_test_path(str(path or "").strip())
    ] or _subtask_required_test_files(subtask)
    if production_followups and test_followups:
        return True
    return bool(_subtask_requires_test_changes(subtask) and _subtask_primary_source_files(subtask))


def _worker_paired_repair_one_sided_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_PAIRED_REPAIR_ONE_SIDED_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_PAIRED_REPAIR_ONE_SIDED_ABORT_SECONDS=%r", raw)
    return 45.0


def _worker_paired_source_test_no_change_abort_seconds(ctx: RunContext) -> float:
    raw = str(
        (getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS") or ""
    ).strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_PAIRED_SOURCE_TEST_NO_CHANGE_ABORT_SECONDS=%r", raw)
    return 150.0


def _paired_source_test_no_change_floor_seconds(
    ctx: RunContext,
    subtask: dict[str, Any],
    worker_id: str,
) -> float:
    if not _subtask_requires_paired_source_test_diff(subtask):
        return 0.0
    try:
        from src.tandem_agents.core.execution.worker import (
            _engine_empty_stream_timeout_seconds,
            _scaled_prompt_sync_timeout_seconds,
            _worker_terminalize_timeout_seconds,
            _worker_timeout_multiplier,
        )

        empty_stream_timeout = _engine_empty_stream_timeout_seconds(ctx.cfg)
        prompt_sync_timeout = _scaled_prompt_sync_timeout_seconds(
            ctx.cfg,
            worker_id or "worker-1",
            bool(subtask.get("write_required", True)),
            _worker_timeout_multiplier(subtask),
        )
        terminalize_grace = _worker_terminalize_timeout_seconds(ctx.cfg)
        return max(0.0, (empty_stream_timeout * 2.0) + prompt_sync_timeout + terminalize_grace)
    except Exception:
        logger.debug("Failed to derive paired source/test no-change recovery floor", exc_info=True)
        return 0.0


def _worker_testless_diff_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS=%r", raw)
    return 120.0


def _effective_worker_testless_diff_abort_seconds(
    ctx: RunContext,
    subtask: dict[str, Any],
    worker_id: str,
) -> float:
    del worker_id
    base_seconds = _worker_testless_diff_abort_seconds(ctx)
    if _subtask_requires_paired_source_test_diff(subtask):
        return min(base_seconds, _worker_paired_repair_one_sided_abort_seconds(ctx))
    return base_seconds


def _worker_test_only_diff_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_TEST_ONLY_DIFF_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_TEST_ONLY_DIFF_ABORT_SECONDS=%r", raw)
    return 180.0


def _effective_worker_test_only_diff_abort_seconds(
    ctx: RunContext,
    subtask: dict[str, Any],
    worker_id: str,
) -> float:
    del worker_id
    base_seconds = _worker_test_only_diff_abort_seconds(ctx)
    if _subtask_requires_paired_source_test_diff(subtask):
        return min(base_seconds, _worker_paired_repair_one_sided_abort_seconds(ctx))
    return base_seconds


def _one_sided_guard_elapsed_seconds(
    first_seen_by_worker: dict[str, dict[str, float]],
    worker_id: str,
    guard_key: str,
    active: bool,
    now: float,
) -> float:
    worker_id = str(worker_id or "").strip()
    guard_key = str(guard_key or "").strip()
    if not worker_id or not guard_key:
        return 0.0
    if not active:
        worker_state = first_seen_by_worker.get(worker_id)
        if worker_state is not None:
            worker_state.pop(guard_key, None)
            if not worker_state:
                first_seen_by_worker.pop(worker_id, None)
        return 0.0
    worker_state = first_seen_by_worker.setdefault(worker_id, {})
    first_seen = float(worker_state.get(guard_key) or now)
    worker_state[guard_key] = first_seen
    return max(0.0, float(now) - first_seen)


def _worker_progress_snapshot_sleep_seconds(
    ctx: RunContext,
    subtasks: dict[str, dict[str, Any]],
) -> float:
    coordination = getattr(ctx.cfg, "coordination", None)
    default_sleep = max(1.0, float(getattr(coordination, "heartbeat_interval_seconds", 1) or 1) / 2.0)
    guard_budgets: list[float] = []
    for worker_id, subtask in subtasks.items():
        subtask = dict(subtask or {})
        if not _subtask_requires_paired_source_test_diff(subtask):
            continue
        for seconds in (
            _effective_worker_testless_diff_abort_seconds(ctx, subtask, worker_id),
            _effective_worker_test_only_diff_abort_seconds(ctx, subtask, worker_id),
        ):
            if seconds > 0:
                guard_budgets.append(seconds)
    if not guard_budgets:
        return default_sleep
    return max(1.0, min(default_sleep, min(guard_budgets) / 3.0))


def _worker_comment_only_diff_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_COMMENT_ONLY_DIFF_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_COMMENT_ONLY_DIFF_ABORT_SECONDS=%r", raw)
    return 180.0


def _worker_verifiable_diff_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_VERIFIABLE_DIFF_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_VERIFIABLE_DIFF_ABORT_SECONDS=%r", raw)
    return 120.0


def _worker_repair_no_change_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_REPAIR_NO_CHANGE_ABORT_SECONDS=%r", raw)
    return 180.0


def _effective_worker_repair_no_change_abort_seconds(
    ctx: RunContext,
    subtask: dict[str, Any],
    worker_id: str,
) -> float:
    configured = _worker_repair_no_change_abort_seconds(ctx)
    if configured <= 0:
        return configured
    effective = max(configured, _effective_worker_no_change_abort_seconds(ctx, subtask, worker_id))
    paired_budget = _worker_paired_source_test_no_change_abort_seconds(ctx)
    if paired_budget > 0 and _subtask_requires_paired_source_test_diff(subtask):
        paired_floor = _paired_source_test_no_change_floor_seconds(ctx, subtask, worker_id)
        return max(paired_floor, min(effective, max(paired_budget, paired_floor)))
    return effective


def _worker_no_change_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_NO_CHANGE_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_NO_CHANGE_ABORT_SECONDS=%r", raw)
    return 240.0


def _effective_worker_no_change_abort_seconds(
    ctx: RunContext,
    subtask: dict[str, Any],
    worker_id: str,
) -> float:
    configured = _worker_no_change_abort_seconds(ctx)
    if configured <= 0:
        return configured
    try:
        from src.tandem_agents.core.execution.worker import (
            _scaled_async_no_text_timeout_seconds,
            _scaled_prompt_sync_timeout_seconds,
            _subtask_prefers_prompt_sync_first,
            _use_prompt_sync_first,
            _worker_terminalize_timeout_seconds,
            _worker_timeout_multiplier,
        )

        write_required = bool(subtask.get("write_required", True))
        timeout_multiplier = _worker_timeout_multiplier(subtask)
        prompt_sync_first = _use_prompt_sync_first(
            ctx.cfg,
            _subtask_prefers_prompt_sync_first(subtask) if write_required else None,
        )
        if prompt_sync_first:
            engine_budget = _scaled_prompt_sync_timeout_seconds(
                ctx.cfg,
                worker_id or "worker-1",
                write_required,
                timeout_multiplier,
            )
        else:
            engine_budget = _scaled_async_no_text_timeout_seconds(
                ctx.cfg,
                worker_id or "worker-1",
                write_required,
                timeout_multiplier,
            )
        grace_seconds = _worker_terminalize_timeout_seconds(ctx.cfg)
        effective = max(configured, engine_budget + grace_seconds)
        paired_budget = _worker_paired_source_test_no_change_abort_seconds(ctx)
        if paired_budget > 0 and _subtask_requires_paired_source_test_diff(subtask):
            paired_floor = _paired_source_test_no_change_floor_seconds(ctx, subtask, worker_id)
            return max(paired_floor, min(effective, max(paired_budget, paired_floor)))
        return effective
    except Exception:
        logger.debug("Failed to derive effective worker no-change abort budget", exc_info=True)
        paired_budget = _worker_paired_source_test_no_change_abort_seconds(ctx)
        if paired_budget > 0 and _subtask_requires_paired_source_test_diff(subtask):
            paired_floor = _paired_source_test_no_change_floor_seconds(ctx, subtask, worker_id)
            return max(paired_floor, min(configured, max(paired_budget, paired_floor)))
        return configured


def _worker_no_diff_tool_loop_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_NO_DIFF_TOOL_LOOP_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_NO_DIFF_TOOL_LOOP_ABORT_SECONDS=%r", raw)
    return 90.0


def _session_messages_with_timeout(ctx: RunContext, session_id: str, *, timeout_seconds: float = 2.0) -> Any:
    result: dict[str, Any] = {}

    def _load() -> None:
        try:
            result["messages"] = sdk_session_messages(ctx.cfg, session_id)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_load, name="aca-worker-session-messages", daemon=True)
    thread.start()
    thread.join(max(0.1, timeout_seconds))
    if thread.is_alive():
        return None
    if result.get("error"):
        logger.debug("Failed to inspect worker session messages for tool-loop guard: %s", result["error"])
        return None
    return result.get("messages")


def _tool_loop_summary_from_messages(messages: Any) -> dict[str, Any] | None:
    if not isinstance(messages, list):
        return None
    tool_parts: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts") or message.get("content") or []
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "tool":
                tool_parts.append(part)
    if not tool_parts:
        return None
    invalid_patch_count = 0
    edit_count = 0
    noop_edit_count = 0
    edit_paths: set[str] = set()
    patch_paths: set[str] = set()
    for part in tool_parts:
        args = part.get("args") if isinstance(part.get("args"), dict) else {}
        tool = str(part.get("tool") or "").strip()
        result = str(part.get("result") or "")
        if tool == "apply_patch":
            patch_text = str(args.get("patchText") or "")
            match = re.search(r"\*\*\* Update File:\s*([^\n\r]+)", patch_text)
            if match:
                patch_paths.add(match.group(1).strip())
            if "No valid patches in input" in result:
                invalid_patch_count += 1
        elif tool == "edit":
            edit_count += 1
            path = str(args.get("path") or "").strip()
            if path:
                edit_paths.add(path)
            if str(args.get("old") or "") == str(args.get("new") or ""):
                noop_edit_count += 1
    if invalid_patch_count >= 3:
        return {
            "tool_parts": len(tool_parts),
            "invalid_patch_count": invalid_patch_count,
            "edit_count": edit_count,
            "noop_edit_count": noop_edit_count,
            "paths": sorted(edit_paths | patch_paths),
            "reason": "worker repeatedly submitted invalid apply_patch calls without leaving a filesystem diff",
        }
    if noop_edit_count >= 3 and len(tool_parts) >= 5:
        return {
            "tool_parts": len(tool_parts),
            "invalid_patch_count": invalid_patch_count,
            "edit_count": edit_count,
            "noop_edit_count": noop_edit_count,
            "paths": sorted(edit_paths | patch_paths),
            "reason": "worker repeatedly made no-op edit calls without leaving a filesystem diff",
        }
    if invalid_patch_count + noop_edit_count >= 3 and edit_count >= 3 and len(tool_parts) >= 8:
        return {
            "tool_parts": len(tool_parts),
            "invalid_patch_count": invalid_patch_count,
            "edit_count": edit_count,
            "noop_edit_count": noop_edit_count,
            "paths": sorted(edit_paths | patch_paths),
            "reason": "worker churned through failed patch and no-op edit calls without leaving a filesystem diff",
        }
    return None


def _messages_have_assistant_or_tool_activity(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = str(info.get("role") or message.get("role") or "").strip().lower()
        if role and role not in {"user", "system"}:
            return True
        parts = message.get("parts") or message.get("content") or []
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip().lower()
            tool_name = str(part.get("tool") or part.get("name") or "").strip()
            has_tool_payload = bool(
                tool_name
                or part.get("args")
                or part.get("input")
                or part.get("result")
                or part.get("output")
                or part.get("id")
            )
            if part_type in {"tool", "tool_call", "function_call"} and has_tool_payload:
                return True
            if tool_name:
                return True
    return False


def _active_worker_no_diff_tool_loop(ctx: RunContext, worker_id: str) -> dict[str, Any] | None:
    session = _load_active_worker_engine_sessions(ctx).get(worker_id) or {}
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        return None
    messages = _session_messages_with_timeout(ctx, session_id)
    summary = _tool_loop_summary_from_messages(messages)
    if not summary:
        return None
    summary["session_id"] = session_id
    return summary


def _active_worker_engine_silence_summary(ctx: RunContext, worker_id: str) -> dict[str, Any] | None:
    session = _load_active_worker_engine_sessions(ctx).get(worker_id) or {}
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        return None
    messages = _session_messages_with_timeout(ctx, session_id)
    if not isinstance(messages, list):
        return None
    if _messages_have_assistant_or_tool_activity(messages):
        return None
    return {
        "session_id": session_id,
        "run_id": str(session.get("run_id") or "").strip(),
        "message_count": len(messages),
        "reason": "active worker engine session had no assistant or tool activity",
    }


def _subtask_is_repair_no_change_guard_candidate(subtask: dict[str, Any]) -> bool:
    if not bool(subtask.get("write_required", True)):
        return False
    return bool(
        subtask.get("deterministic_partial_diff_repair")
        or subtask.get("deterministic_testless_repair")
        or subtask.get("discarded_partial_diff_patch")
        or subtask.get("carry_forward_patch")
        or subtask.get("carry_forward_patches")
    )


def _subtask_is_no_change_guard_candidate(subtask: dict[str, Any]) -> bool:
    if not bool(subtask.get("write_required", True)):
        return False
    if _subtask_is_repair_no_change_guard_candidate(subtask):
        return False
    return True


def _latest_worker_retry_write_required(
    ctx: RunContext,
    worker_id: str,
    subtask: dict[str, Any],
) -> bool | None:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    events_path = Path(ctx.layout.get("events") or "")
    if not events_path.exists():
        return None
    execution_id = str(subtask.get("_worker_execution_id") or "").strip()
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("worker_id") or "").strip() != worker_id:
            continue
        event_type = str(event.get("type") or "").strip()
        event_execution_id = str(payload.get("execution_id") or "").strip()
        if execution_id and event_execution_id and event_execution_id != execution_id:
            continue
        if event_type == "worker.retry_started":
            value = payload.get("write_required")
            if isinstance(value, bool):
                return value
            return None
        if event_type == "worker.started" and (not execution_id or event_execution_id == execution_id):
            return None
    return None


def _latest_worker_start_baseline(
    ctx: RunContext,
    worker_id: str,
    subtask: dict[str, Any],
) -> dict[str, Any] | None:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    events_path = Path(ctx.layout.get("events") or "")
    if not events_path.exists():
        return None
    execution_id = str(subtask.get("_worker_execution_id") or "").strip()
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("worker_id") or "").strip() != worker_id:
            continue
        event_type = str(event.get("type") or "").strip()
        event_execution_id = str(payload.get("execution_id") or "").strip()
        if execution_id and event_execution_id and event_execution_id != execution_id:
            continue
        if event_type != "worker.started":
            continue
        baseline_changed_files = [
            str(path or "").strip().replace("\\", "/")
            for path in payload.get("baseline_changed_files") or []
            if str(path or "").strip()
        ]
        baseline_file_states = payload.get("baseline_file_states")
        if not isinstance(baseline_file_states, dict):
            baseline_file_states = {}
        normalized_states = {
            str(path or "").strip().replace("\\", "/"): dict(state)
            for path, state in baseline_file_states.items()
            if str(path or "").strip() and isinstance(state, dict)
        }
        return {
            "baseline_changed_files": list(dict.fromkeys(baseline_changed_files)),
            "baseline_file_states": normalized_states,
            "baseline_state_digest": str(payload.get("baseline_state_digest") or "").strip(),
        }
    return None


def _worktree_has_any_changes(worktree: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        logger.debug("Failed to inspect worker worktree status for no-change guard: %s", result.stderr)
        return True
    return bool((result.stdout or "").strip())


def _baseline_file_state(worktree: Path, rel_path: str) -> dict[str, Any]:
    path = worktree / rel_path
    try:
        if path.is_symlink():
            return {"kind": "symlink", "target": os.readlink(path)}
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {"kind": "file", "sha256": digest.hexdigest()}
        if path.exists():
            return {"kind": "other"}
    except OSError as exc:
        return {"kind": "error", "detail": str(exc)[:200]}
    return {"kind": "absent"}


def _fresh_changed_files_since_baseline(
    worktree: Path,
    changed_files: list[str],
    baseline: dict[str, Any] | None,
) -> list[str]:
    if not baseline:
        return list(dict.fromkeys(path for path in changed_files if str(path or "").strip()))
    baseline_states = baseline.get("baseline_file_states")
    if not isinstance(baseline_states, dict):
        return list(dict.fromkeys(path for path in changed_files if str(path or "").strip()))
    fresh: list[str] = []
    for raw_path in changed_files:
        rel_path = str(raw_path or "").strip().replace("\\", "/")
        if not rel_path:
            continue
        old_state = baseline_states.get(rel_path)
        if not isinstance(old_state, dict):
            fresh.append(rel_path)
            continue
        if _baseline_file_state(worktree, rel_path) != old_state:
            fresh.append(rel_path)
    return list(dict.fromkeys(fresh))


def _subtask_uses_carried_partial_diff(subtask: dict[str, Any]) -> bool:
    return bool(subtask.get("carry_forward_patch") or subtask.get("carry_forward_patches"))


def _carried_baseline_changed_files(
    subtask: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> list[str]:
    if not _subtask_uses_carried_partial_diff(subtask) or not isinstance(baseline, dict):
        return []
    return list(
        dict.fromkeys(
            path
            for raw_path in baseline.get("baseline_changed_files") or []
            if (path := _normalize_repo_path(raw_path))
        )
    )


def _changed_files_include_source_and_test(changed_files: list[str]) -> bool:
    normalized = list(
        dict.fromkeys(path for raw_path in changed_files if (path := _normalize_repo_path(raw_path)))
    )
    return bool(normalized) and any(_is_test_path(path) for path in normalized) and any(
        not _is_test_path(path) for path in normalized
    )


def _carried_partial_diff_paths(subtask: dict[str, Any]) -> list[str]:
    raw_patches = subtask.get("carry_forward_patches")
    patches: list[str] = []
    if isinstance(raw_patches, list):
        patches.extend(str(path or "").strip() for path in raw_patches if str(path or "").strip())
    raw_patch = str(subtask.get("carry_forward_patch") or "").strip()
    if raw_patch:
        patches.append(raw_patch)
    return list(dict.fromkeys(patches))


def _attach_carried_partial_diff_to_repair_no_change_result(
    result: dict[str, Any],
    subtask: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> None:
    if str(result.get("failure_reason") or "").strip() != "WORKER_REPAIR_NO_CHANGE":
        return
    patches = _carried_partial_diff_paths(subtask)
    if not patches:
        return
    changed_files = _carried_baseline_changed_files(subtask, baseline) or list(
        dict.fromkeys(
            path
            for raw_path in (
                subtask.get("repair_changed_files")
                or subtask.get("target_files")
                or subtask.get("files")
                or []
            )
            if (path := _normalize_repo_path(raw_path))
        )
    )
    if not changed_files:
        return
    result.setdefault("artifacts", {})["partial_diff"] = patches[-1]
    result["partial_diff_artifact"] = patches[-1]
    result["changed_files"] = changed_files
    result["preserved_partial_diff_no_change"] = True
    result["engine_blocker_kind"] = "worker_no_progress"
    preserved_failure_reason = str(subtask.get("repair_source_failure_reason") or "").strip()
    if not preserved_failure_reason and subtask.get("verification_commands") and _changed_files_include_source_and_test(changed_files):
        preserved_failure_reason = "WORKER_VERIFIABLE_DIFF_TEST_FAILED"
    if preserved_failure_reason:
        result["preserved_failure_reason"] = preserved_failure_reason
    repair_excerpt = str(subtask.get("repair_worker_output_excerpt") or "").strip()
    if repair_excerpt:
        result["verification_output_excerpt"] = repair_excerpt[:2000]
        result["output_excerpt"] = (
            str(result.get("output_excerpt") or "").rstrip()
            + "\n\nPreserved partial diff failure:\n"
            + repair_excerpt[:1200].strip()
        ).strip()
    verification_commands = [
        str(command or "").strip()
        for command in (subtask.get("verification_commands") or [])
        if str(command or "").strip()
    ]
    if verification_commands:
        result["verification_command"] = verification_commands[0].split()


def _validation_changed_files_with_carried_baseline(
    subtask: dict[str, Any],
    changed_files: list[str],
    baseline: dict[str, Any] | None,
) -> list[str]:
    fresh_files = list(
        dict.fromkeys(path for raw_path in changed_files if (path := _normalize_repo_path(raw_path)))
    )
    carried_files = _carried_baseline_changed_files(subtask, baseline)
    if not fresh_files:
        if (
            subtask.get("repair_verification_first")
            and subtask.get("deterministic_partial_diff_repair")
            and _changed_files_include_source_and_test(carried_files)
        ):
            return carried_files
        return []
    explicit_test_followups = [
        path
        for raw_path in subtask.get("repair_requires_test_followup") or []
        if (path := _normalize_repo_path(raw_path)) and _is_test_path(path)
    ]
    explicit_paired_repair = bool(
        subtask.get("repair_requires_paired_source_test") or subtask.get("repair_requires_paired_source_test_diff")
    )
    carried_repair_mode = str(subtask.get("repair_mode") or "").strip()
    composed_carried_repair = (
        (
            bool(subtask.get("carry_forward_patches"))
            or carried_repair_mode
            in {
                "complementary_guarded_partial_diff",
                "weak_source_test_diff",
            }
        )
        and _subtask_uses_carried_partial_diff(subtask)
        and _changed_files_include_source_and_test(carried_files)
    )
    if (
        subtask.get("repair_verification_first")
        and subtask.get("deterministic_partial_diff_repair")
        and _changed_files_include_source_and_test(carried_files)
    ):
        return list(dict.fromkeys([*carried_files, *fresh_files]))
    if composed_carried_repair and (
        subtask.get("repair_changed_files")
        or explicit_paired_repair
        or subtask.get("repair_requires_test_followup")
        or subtask.get("repair_requires_production_followup")
        or subtask.get("repair_source_worker_id")
    ):
        return list(dict.fromkeys([*carried_files, *fresh_files]))
    if (explicit_test_followups or explicit_paired_repair) and any(_is_test_path(path) for path in carried_files):
        fresh_has_required_test = _changed_files_satisfy_required_test_files(
            fresh_files,
            explicit_test_followups or _subtask_required_test_files(subtask),
        )
        if not fresh_has_required_test:
            return fresh_files
    if not any(not _is_test_path(path) for path in carried_files):
        return fresh_files
    return list(dict.fromkeys([*carried_files, *fresh_files]))


def _worktree_has_subtask_changes(
    worktree: Path,
    subtask: dict[str, Any] | None,
    baseline: dict[str, Any] | None = None,
) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        logger.debug("Failed to inspect worker worktree status for no-change guard: %s", result.stderr)
        return True
    changed_files: list[str] = []
    for raw_line in (result.stdout or "").splitlines():
        path = raw_line[3:].strip() if len(raw_line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            changed_files.append(path)
    scoped = _changed_files_scoped_to_subtask(changed_files, subtask)
    return bool(_fresh_changed_files_since_baseline(worktree, scoped, baseline))


def _diff_add_delete_counts(diff_text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _diff_is_destructive_rewrite(diff_text: str, *, max_deletions: int) -> bool:
    additions, deletions = _diff_add_delete_counts(diff_text)
    return deletions >= max_deletions and deletions > max(20, additions * 2)


def _subtask_primary_source_files(subtask: dict[str, Any]) -> list[str]:
    for path in _subtask_declared_change_files(subtask):
        if not _is_test_path(path):
            return [path]
    return []


def _changed_files_satisfy_primary_source_target(
    changed_files: list[str],
    subtask: dict[str, Any],
) -> bool:
    primary_sources = _subtask_primary_source_files(subtask)
    if not primary_sources:
        return True
    changed = {_normalize_repo_path(path) for path in changed_files if _normalize_repo_path(path)}
    return any(path in changed for path in primary_sources)


def _subtask_has_verifiable_source_and_test_diff(
    subtask: dict[str, Any],
    changed_files: list[str],
) -> bool:
    if not _subtask_requires_test_changes(subtask):
        return False
    if not _changed_files_satisfy_required_test_files(changed_files, _subtask_required_test_files(subtask)):
        return False
    if not _changed_files_satisfy_primary_source_target(changed_files, subtask):
        return False
    return any(not _is_test_path(path) for path in changed_files)


def _subtask_requires_production_followup_for_test_only_diff(subtask: dict[str, Any]) -> bool:
    explicit_followups = [
        str(path or "").strip()
        for path in subtask.get("repair_requires_production_followup") or []
        if str(path or "").strip()
    ]
    if any(not _is_test_path(path) for path in explicit_followups):
        return True
    declared_files = [
        str(path or "").strip()
        for field in ("target_files", "files")
        for path in (subtask.get(field) or [])
        if str(path or "").strip()
    ]
    production_files = [path for path in declared_files if not _is_test_path(path)]
    if not production_files:
        return False
    text_parts: list[Any] = [
        subtask.get("title"),
        subtask.get("goal"),
        subtask.get("scope_note"),
    ]
    for field in ("acceptance_criteria", "deliverables"):
        value = subtask.get(field)
        if isinstance(value, (list, tuple, set)):
            text_parts.extend(value)
        elif value:
            text_parts.append(value)
    text = "\n".join(str(part or "").lower() for part in text_parts)
    if (
        "test-only slice" in text
        or "test only slice" in text
        or "do not edit production" in text
        or "do not edit production files" in text
    ):
        return False
    return any(
        marker in text
        for marker in (
            "production",
            "implementation",
            "behavior",
            "source",
            "wire",
            "wiring",
            "loader",
            "regression",
            "fix",
        )
    )


def _failed_result_has_reviewable_source_and_test_diff(
    result: dict[str, Any],
    subtask: dict[str, Any],
) -> bool:
    if int(result.get("returncode") or 0) == 0:
        return False
    failure_reason = str(result.get("failure_reason") or "").strip()
    if failure_reason == "WORKER_VERIFIABLE_DIFF_UNTERMINATED":
        try:
            verification_returncode = int(result.get("verification_returncode"))
        except (TypeError, ValueError):
            return False
        if verification_returncode != 0 or bool(result.get("verification_timed_out")):
            return False
    elif failure_reason in {
        "WORKER_SYNTAX_INVALID_DIFF",
        "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
        "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
    }:
        return False
    patch_path = str(result.get("partial_diff_artifact") or "").strip()
    if not patch_path and isinstance(result.get("artifacts"), dict):
        patch_path = str(result["artifacts"].get("partial_diff") or "").strip()
    if not patch_path:
        return False
    changed_files = [str(path or "").strip() for path in result.get("changed_files") or [] if str(path or "").strip()]
    return _subtask_has_verifiable_source_and_test_diff(subtask, changed_files)


def _positive_contract_identifier_tokens(subtask: dict[str, Any]) -> list[str]:
    ignored_tokens = {
        "as_dict",
        "task_key",
        "project_key",
        "repo_key",
        "scope_mode",
        "scope_paths",
    }

    def _is_contract_field_token(token: str) -> bool:
        if token in ignored_tokens:
            return False
        return (
            token.startswith("max_")
            or token.startswith("min_")
            or token.startswith("aca_")
            or token.endswith("_backpressure")
            or token.endswith("_reached")
            or token.endswith("_cents")
            or token.endswith("_seconds")
            or token.endswith("_limit")
        )

    values: list[str] = []
    for field in ("acceptance_criteria", "deliverables"):
        value = subtask.get(field)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item or "") for item in value)
        elif value:
            values.append(str(value))
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        lowered = value.lower()
        if "do not add" in lowered or "out of scope" in lowered:
            continue
        for token in re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", value):
            token = token.lower()
            if not _is_contract_field_token(token):
                continue
            if token not in seen:
                seen.add(token)
                tokens.append(token)
    return tokens


def _failed_result_has_reviewable_production_diff(
    result: dict[str, Any],
    subtask: dict[str, Any],
    worktree: Path,
) -> bool:
    if int(result.get("returncode") or 0) == 0:
        return False
    patch_path = str(result.get("partial_diff_artifact") or "").strip()
    if not patch_path and isinstance(result.get("artifacts"), dict):
        patch_path = str(result["artifacts"].get("partial_diff") or "").strip()
    if not patch_path:
        return False
    result_text = "\n".join(
        str(result.get(field) or "")
        for field in ("stdout", "stderr", "output_excerpt", "recovery_action", "failure_reason")
    )
    if result_text.strip():
        from src.tandem_agents.core.execution.worker import _terminalized_note_reports_blockers

        if _terminalized_note_reports_blockers(result_text):
            return False
    if _subtask_requires_test_changes(subtask):
        return False
    changed_files = [str(path or "").strip().replace("\\", "/") for path in result.get("changed_files") or [] if str(path or "").strip()]
    if not changed_files or any(_is_test_path(path) for path in changed_files):
        return False
    declared = [
        str(path or "").strip().replace("\\", "/")
        for field in ("target_files", "files")
        for path in (subtask.get(field) or [])
        if str(path or "").strip()
    ]
    if declared and not set(changed_files).issubset(set(declared)):
        return False
    if not _changed_files_satisfy_primary_source_target(changed_files, subtask):
        return False
    if _changed_python_syntax_errors(worktree, changed_files):
        return False
    diff_text = _worktree_changed_files_diff(worktree, changed_files)
    if not diff_text:
        return False
    if _diff_is_comment_only(diff_text) or _diff_has_unproductive_marker(diff_text):
        return False
    tokens = _positive_contract_identifier_tokens(subtask)
    if not tokens:
        return False
    diff_text_lower = diff_text.lower()
    return all(token in diff_text_lower for token in tokens)


def _subtask_has_required_test_only_diff(
    subtask: dict[str, Any],
    changed_files: list[str],
) -> bool:
    if not _subtask_requires_test_changes(subtask):
        return False
    if not _subtask_requires_production_followup_for_test_only_diff(subtask):
        return False
    if not changed_files:
        return False
    if not all(_is_test_path(path) for path in changed_files):
        return False
    return _changed_files_satisfy_required_test_files(
        changed_files,
        _subtask_required_test_files(subtask),
    )


def _added_diff_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if not line.startswith("+") or line.startswith(("+++", "+++ ")):
            continue
        lines.append(line[1:])
    return lines


def _removed_diff_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if not line.startswith("-") or line.startswith(("---", "--- ")):
            continue
        lines.append(line[1:])
    return lines


def _diff_has_unproductive_marker(diff_text: str) -> bool:
    return any(marker in str(diff_text or "") for marker in _UNPRODUCTIVE_DIFF_MARKERS)


def _diff_is_comment_only(diff_text: str) -> bool:
    added = [line.strip() for line in _added_diff_lines(diff_text) if line.strip()]
    if not added:
        return False
    comment_prefixes = ("//", "#", "/*", "*", "*/", "//!", "///")
    return all(line.startswith(comment_prefixes) for line in added)


def _normalize_repo_path(path: Any) -> str:
    return str(path or "").strip().replace("\\", "/").strip("/")


def _diff_sections_by_file(diff_text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_path = ""
    for line in str(diff_text or "").splitlines():
        if line.startswith("diff --git "):
            current_path = ""
            parts = line.split()
            if len(parts) >= 4:
                current_path = _normalize_repo_path(parts[3][2:] if parts[3].startswith("b/") else parts[3])
                sections.setdefault(current_path, [])
            continue
        if line.startswith("+++ b/"):
            current_path = _normalize_repo_path(line.removeprefix("+++ b/"))
            sections.setdefault(current_path, [])
        if current_path:
            sections.setdefault(current_path, []).append(line)
    return {path: "\n".join(lines) for path, lines in sections.items()}


def _line_is_comment_or_trivia(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped in {"{", "}", "};", "(", ")", "[", "]", ","}:
        return True
    return stripped.startswith(("//", "#", "/*", "*", "*/", "//!", "///"))


def _diff_changed_files_missing_substantive_production_followup(
    diff_text: str,
    changed_files: list[str],
    subtask: dict[str, Any],
) -> list[str]:
    followups = [
        _normalize_repo_path(path)
        for path in subtask.get("repair_requires_production_followup") or []
        if _normalize_repo_path(path)
    ]
    if not followups:
        return []
    changed = {_normalize_repo_path(path) for path in changed_files if _normalize_repo_path(path)}
    sections = _diff_sections_by_file(diff_text)
    missing: list[str] = []
    for path in followups:
        if path not in changed:
            missing.append(path)
            continue
        section = sections.get(path, "")
        added_or_removed = [
            line[1:]
            for line in section.splitlines()
            if (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
        ]
        if not any(not _line_is_comment_or_trivia(line) for line in added_or_removed):
            missing.append(path)
    return missing


def _diff_has_tautological_boolean_assertion(diff_text: str) -> bool:
    code_lines = [
        line.strip()
        for line in _added_diff_lines(diff_text)
        if line.strip() and not line.strip().startswith(("//", "#", "/*", "*", "*/"))
    ]
    if not code_lines:
        return False
    declared_true: set[str] = set()
    non_tautological: list[str] = []
    for line in code_lines:
        match = re.fullmatch(r"(?:let\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*true\s*;", line)
        if match:
            declared_true.add(match.group(1))
            continue
        match = re.fullmatch(r"assert!\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;", line)
        if match and match.group(1) in declared_true:
            continue
        if re.fullmatch(r"assert!\(\s*true\s*\)\s*;", line):
            continue
        non_tautological.append(line)
    return bool(declared_true or code_lines) and not non_tautological


def _diff_is_string_only_change(diff_text: str) -> bool:
    added = [line.strip() for line in _added_diff_lines(diff_text) if line.strip()]
    removed = [line.strip() for line in _removed_diff_lines(diff_text) if line.strip()]
    if not added or not removed:
        return False
    string_line = re.compile(r'^[A-Za-z0-9_"\':,\s.\-{}()\[\]]*".*"[A-Za-z0-9_"\':,\s.\-{}()\[\]]*$')
    if not all(string_line.match(line) for line in added + removed):
        return False
    normalize = lambda line: re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    return sorted(normalize(line) for line in added) == sorted(normalize(line) for line in removed)


def _diff_is_local_string_oracle_test(diff_text: str) -> bool:
    code_lines = [
        line.strip()
        for line in _added_diff_lines(diff_text)
        if line.strip() and not line.strip().startswith(("//", "#", "/*", "*", "*/", "//!", "///"))
    ]
    if not code_lines:
        return False
    local_strings: set[str] = set()
    meaningful_asserts = 0
    for line in code_lines:
        if line in {"{", "}", "};"}:
            continue
        if line.startswith("#[") or re.match(r"(?:async\s+)?fn\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", line):
            continue
        match = re.fullmatch(r"let\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"(?:[^\"\\]|\\.)*\"\s*;", line)
        if match:
            local_strings.add(match.group(1))
            continue
        match = re.fullmatch(
            r"assert!\(\s*([A-Za-z_][A-Za-z0-9_]*)\.contains\(\s*\"(?:[^\"\\]|\\.)*\"\s*\)\s*\)\s*;",
            line,
        )
        if match and match.group(1) in local_strings:
            meaningful_asserts += 1
            continue
        match = re.fullmatch(
            r"assert_ne!\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;",
            line,
        )
        if match and match.group(1) in local_strings and match.group(2) in local_strings:
            meaningful_asserts += 1
            continue
        return False
    return bool(local_strings) and meaningful_asserts >= 2


def _diff_has_placeholder_noop_test(diff_text: str) -> bool:
    added = [
        line.strip().lower()
        for line in _added_diff_lines(diff_text)
        if line.strip() and not line.strip().startswith(("+++", "#["))
    ]
    if not added:
        return False
    placeholder_terms = (
        "placeholder",
        "must be replaced",
        "replace with",
        "before completion",
        "before merging",
        "not implemented",
    )
    has_placeholder_language = any(any(term in line for term in placeholder_terms) for line in added)
    has_noop_assertion = any(re.fullmatch(r"assert!\(\s*true\s*\)\s*;", line) for line in added)
    return has_noop_assertion and has_placeholder_language


def _diff_missing_production_function_calls(worktree: Path, diff_text: str, changed_files: list[str]) -> list[str]:
    if not changed_files or not all(_is_test_path(path) for path in changed_files):
        return []
    added_lines = _added_diff_lines(diff_text)
    defined_in_diff: set[str] = set()
    candidates: set[str] = set()
    call_pattern = re.compile(r"(?<![\w.!])([A-Za-z_][A-Za-z0-9_]*)\s*(?=\()")
    helper_markers = (
        "github",
        "project",
        "projects",
        "readiness",
        "intake",
        "schema",
        "drift",
        "divergence",
        "diagnostic",
    )
    ignored_calls = {
        "Some",
        "None",
        "Ok",
        "Err",
        "String",
        "Vec",
        "HashMap",
        "HashSet",
        "BTreeMap",
        "BTreeSet",
        "Option",
        "Result",
    }
    for raw_line in added_lines:
        line = raw_line.strip()
        if not line or line.startswith(("//", "#", "/*", "*", "*/", "//!", "///")):
            continue
        definition = re.match(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if definition:
            defined_in_diff.add(definition.group(1))
            continue
        for match in call_pattern.finditer(line):
            name = match.group(1)
            if name in ignored_calls or name in defined_in_diff:
                continue
            lowered = name.lower()
            if "_" not in name:
                continue
            if any(marker in lowered for marker in helper_markers):
                candidates.add(name)
    if not candidates:
        return []
    changed_set = {str(path or "").strip().replace("\\", "/").strip("/") for path in changed_files}
    missing: list[str] = []
    for name in sorted(candidates):
        try:
            proc = subprocess.run(
                ["git", "grep", "-n", "--fixed-strings", "--", name],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode not in (0, 1):
            continue
        found_production_reference = False
        for line in proc.stdout.splitlines():
            path = line.split(":", 1)[0].strip().replace("\\", "/")
            if not path or path in changed_set:
                continue
            if not _is_test_path(path):
                found_production_reference = True
                break
        if not found_production_reference:
            missing.append(name)
    return missing


def _worker_no_progress_timeout_seconds(ctx: RunContext, subtasks: list[dict[str, Any]] | None = None) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_NO_PROGRESS_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_NO_PROGRESS_TIMEOUT_SECONDS=%r", raw)
    from src.tandem_agents.core.execution.worker import (
        _scaled_async_prompt_timeout_seconds,
        _scaled_prompt_sync_timeout_seconds,
        _worker_terminalize_timeout_seconds,
        _worker_timeout_multiplier,
    )

    prompt_budget = 0.0
    pending = subtasks or [{"write_required": True}]
    for index, subtask in enumerate(pending, start=1):
        write_required = bool(subtask.get("write_required", True))
        timeout_multiplier = _worker_timeout_multiplier(subtask)
        subtask_budget = _scaled_prompt_sync_timeout_seconds(
            ctx.cfg,
            f"worker-{index}",
            write_required,
            timeout_multiplier,
        )
        if write_required:
            # Write-required workers use prompt_sync first, then may retry once
            # through async streaming before returning an engine timeout result.
            subtask_budget += _scaled_async_prompt_timeout_seconds(
                ctx.cfg,
                f"worker-{index}",
                write_required,
                timeout_multiplier,
            )
        prompt_budget = max(prompt_budget, subtask_budget)
    terminalize_budget = _worker_terminalize_timeout_seconds(ctx.cfg)
    return max(1.0, prompt_budget + terminalize_budget + 30.0)


def dispatch_workers(ctx: RunContext) -> None:
    """Execute the pending subtask worker pool and collect results.

    If ``ctx.pending_subtasks`` is empty this is a no-op (results already
    accumulated by ``pre_screen_subtasks`` for the pre-satisfied path).

    Mutates:
        ctx.worker_results     -- extended with results from pending subtasks
        ctx.repo_validation    -- refreshed after worker sync
        ctx.blackboard, ctx.status
    """
    if not ctx.pending_subtasks:
        logger.debug(
            "No pending subtasks; skipping worker dispatch (run_id=%s)", ctx.run_id
        )
        _post_dispatch_validation(ctx)
        return

    from src.tandem_agents.core.engine.engine import effective_tandem_provider
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot, write_status

    worker_provider, worker_model = ctx.cfg.provider_for_role("worker")
    worker_capabilities = {
        "mode": "local-worker-pool",
        "provider": worker_provider,
        "model": worker_model,
        "repository": ctx.repo.get("slug") or ctx.cfg.repository.slug,
        "worktree_mode": "single-host",
    }
    # Local worker rows are observability records for this run. The manager
    # owns the task lease, so child workers must not be able to stale it.
    worker_lease_id: str | None = None

    # Transition status
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="worker_execution",
        phase_role="worker",
        run_status="running",
    )
    _rc._touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="worker_execution",
        ctx=ctx,
    )
    max_parallel_workers = max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1)
    queued_worker_slices = len(ctx.pending_subtasks)
    append_event(
        ctx.layout["events"],
        "swarm.spawned",
        ctx.run_id,
        {
            "planned_workers": len(ctx.planned_subtasks),
            "max_parallel": max_parallel_workers,
            "spawned_workers": min(queued_worker_slices, max_parallel_workers),
            "queued_workers": queued_worker_slices,
            "scheduled_workers": queued_worker_slices,
        },
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    write_status(ctx.layout["status"], ctx.status)

    # --- Heartbeat thread ---
    active_workers_lock = threading.Lock()
    active_workers: set[str] = set()
    active_worker_started_at: dict[str, float] = {}
    active_worker_started_at_ms: dict[str, int] = {}
    active_worker_worktrees: dict[str, Path] = {}
    active_worker_subtasks: dict[str, dict[str, Any]] = {}
    active_worker_start_baselines: dict[str, dict[str, Any] | None] = {}
    active_worker_overlay_baseline_ignored: set[str] = set()
    active_worker_progress_snapshots: dict[str, dict[str, Any]] = {}
    active_worker_snapshot_digests: dict[str, str] = {}
    active_worker_one_sided_guard_seen_at: dict[str, dict[str, float]] = {}
    active_worker_abort_results: dict[str, dict[str, Any]] = {}
    active_worker_snapshot_inflight: set[str] = set()
    active_worker_snapshot_threads: list[threading.Thread] = []
    last_progress_event_at = 0.0
    worker_heartbeat_stop = threading.Event()

    def _runaway_diff_max_bytes() -> int:
        raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_RUNAWAY_DIFF_MAX_BYTES") or "").strip()
        if raw:
            try:
                return max(1_000, int(raw))
            except ValueError:
                logger.warning("Ignoring invalid ACA_WORKER_RUNAWAY_DIFF_MAX_BYTES=%r", raw)
        return 1_000_000

    def _destructive_diff_max_deletions() -> int:
        raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_DESTRUCTIVE_DIFF_MAX_DELETIONS") or "").strip()
        if raw:
            try:
                return max(25, int(raw))
            except ValueError:
                logger.warning("Ignoring invalid ACA_WORKER_DESTRUCTIVE_DIFF_MAX_DELETIONS=%r", raw)
        return 200

    def _active_worker_start_baseline(wid: str, subtask: dict[str, Any]) -> dict[str, Any] | None:
        with active_workers_lock:
            if wid in active_worker_start_baselines:
                return active_worker_start_baselines[wid]
        baseline = _latest_worker_start_baseline(ctx, wid, subtask)
        if baseline is None:
            return None
        with active_workers_lock:
            active_worker_start_baselines[wid] = baseline
        return baseline

    def _active_worker_snapshot_still_current(
        wid: str,
        worktree: Path,
        subtask_id: str = "",
    ) -> bool:
        with active_workers_lock:
            if wid not in active_workers:
                return False
            current_worktree = active_worker_worktrees.get(wid)
            current_subtask = active_worker_subtasks.get(wid) or {}
        if current_worktree is not None and current_worktree != worktree:
            return False
        current_subtask_id = str(current_subtask.get("id") or "").strip()
        if subtask_id and current_subtask_id and current_subtask_id != subtask_id:
            return False
        return True

    def _snapshot_worker_progress_diff(wid: str, worktree: Path) -> dict[str, Any] | None:
        try:
            from src.tandem_agents.core.execution.worker import (  # noqa: PLC0415
                _applyable_working_diff,
                _worktree_changed_files,
                git_working_diff,
            )

            with active_workers_lock:
                if wid not in active_workers:
                    return None
                subtask = dict(active_worker_subtasks.get(wid) or {})
                started_at = float(active_worker_started_at.get(wid) or time.monotonic())
            changed_files = _changed_files_scoped_to_subtask(_worktree_changed_files(worktree), subtask)
            if not changed_files:
                return None
            baseline = _active_worker_start_baseline(wid, subtask)
            if baseline is None:
                return None
            fresh_changed_files = _fresh_changed_files_since_baseline(worktree, changed_files, baseline)
            if not fresh_changed_files:
                with active_workers_lock:
                    already_reported = wid in active_worker_overlay_baseline_ignored
                    active_worker_overlay_baseline_ignored.add(wid)
                if not already_reported:
                    append_event(
                        ctx.layout["events"],
                        "worker.overlay_baseline_ignored",
                        ctx.run_id,
                        {
                            "worker_id": wid,
                            "changed_files": list(changed_files),
                            "baseline_changed_files": list(baseline.get("baseline_changed_files") or []),
                            "reason": "worker worktree only contains inherited overlay changes",
                        },
                        task_id=ctx.task.get("task_id"),
                        role="worker",
                        repo={"path": ctx.repo.get("path")},
                    )
                return None
            changed_files = fresh_changed_files
            raw_diff_text = _applyable_working_diff(worktree)
            diff_text = _filter_diff_text_to_files(raw_diff_text, changed_files)
            if not str(diff_text or "").strip():
                diff_text = _worktree_changed_files_diff(worktree, changed_files)
            if not str(diff_text or "").strip():
                diff_text = _filter_diff_text_to_files(git_working_diff(worktree), changed_files)
            if not str(diff_text or "").strip():
                return None
            validation_changed_files = _validation_changed_files_with_carried_baseline(
                subtask,
                changed_files,
                baseline,
            )
            validation_diff_text = diff_text
            if validation_changed_files != changed_files:
                validation_diff_text = _filter_diff_text_to_files(raw_diff_text, validation_changed_files)
                if not str(validation_diff_text or "").strip():
                    validation_diff_text = _worktree_changed_files_diff(worktree, validation_changed_files)
                if not str(validation_diff_text or "").strip():
                    validation_diff_text = _filter_diff_text_to_files(
                        git_working_diff(worktree),
                        validation_changed_files,
                    )
                if not str(validation_diff_text or "").strip():
                    validation_diff_text = diff_text
            diff_bytes = len(diff_text.encode("utf-8", errors="replace"))
            diff_lines = diff_text.count("\n") + 1
            validation_diff_bytes = len(validation_diff_text.encode("utf-8", errors="replace"))
            validation_diff_lines = validation_diff_text.count("\n") + 1
            digest = hashlib.sha256(diff_text.encode("utf-8", errors="replace")).hexdigest()
            previous_snapshot = active_worker_progress_snapshots.get(wid)
            same_digest = active_worker_snapshot_digests.get(wid) == digest
            diff_ok = True
            diff_check_detail = ""
            if not same_digest:
                diff_ok, diff_check_detail = _diff_apply_check(worktree, diff_text)
            if not diff_ok:
                snapshot = active_worker_progress_snapshots.get(wid)
                append_event(
                    ctx.layout["events"],
                    "worker.progress_partial_diff_invalid",
                    ctx.run_id,
                    {
                        "worker_id": wid,
                        "changed_files": list(changed_files),
                        "diff_bytes": diff_bytes,
                        "diff_lines": diff_lines,
                        "previous_partial_diff_artifact": (snapshot or {}).get("partial_diff_artifact", ""),
                        "reason": "progress-time git diff did not apply cleanly to HEAD",
                        "detail": diff_check_detail[:1000],
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            artifacts_dir = ctx.run_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            subtask = active_worker_subtasks.get(wid) or {}
            execution_id = str(subtask.get("_worker_execution_id") or "").strip()
            artifact_stem = f"{wid}-{execution_id}" if execution_id else wid
            artifact_path = artifacts_dir / f"{artifact_stem}.progress-partial-worker-diff.patch"
            status_rows = "\n".join(f"- {path}" for path in changed_files)
            validation_status_rows = "\n".join(f"- {path}" for path in validation_changed_files)
            max_bytes = _runaway_diff_max_bytes()
            max_deletions = _destructive_diff_max_deletions()
            additions, deletions = _diff_add_delete_counts(diff_text)
            if _diff_is_destructive_rewrite(diff_text, max_deletions=max_deletions):
                excerpt = diff_text[:20_000].rstrip()
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker diff tripped ACA destructive rewrite guard\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## destructive rewrite guard\n\n"
                    f"- additions: {additions}\n"
                    f"- deletions: {deletions}\n"
                    f"- max_deletions: {max_deletions}\n"
                    "- reason: diff deletes far more code than it adds before producing a terminal result\n\n"
                    "## clipped git diff excerpt\n\n"
                    f"{excerpt}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_destructive_diff_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "additions": additions,
                    "deletions": deletions,
                    "max_deletions": max_deletions,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                with active_workers_lock:
                    subtask = active_worker_subtasks.get(wid)
                    subtask_id = _abort_result_subtask_id(
                        subtask,
                        active_worker_worktrees.get(wid),
                    )
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_DESTRUCTIVE_DIFF",
                        "blocker_kind": "worker_runaway_diff",
                        "output_excerpt": (
                            "Worker diff tripped ACA destructive rewrite guard "
                            f"({deletions} deletions, {additions} additions; max deletions {max_deletions}). "
                            "ACA preserved a clipped summary and abandoned this worker before more churn."
                        ),
                        "recovery_action": (
                            "Block this run and inspect the clipped diff evidence before resetting the task. "
                            "The next prompt must preserve existing file structure and avoid broad rewrites."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                        **_subtask_retry_metadata(subtask),
                    }
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.runaway_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            if diff_bytes > max_bytes:
                excerpt = diff_text[:20_000].rstrip()
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker diff exceeded ACA runaway size guard\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## runaway guard\n\n"
                    f"- diff_bytes: {diff_bytes}\n"
                    f"- diff_lines: {diff_lines}\n"
                    f"- max_bytes: {max_bytes}\n\n"
                    "## clipped git diff excerpt\n\n"
                    f"{excerpt}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_runaway_diff_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "max_bytes": max_bytes,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                with active_workers_lock:
                    subtask = active_worker_subtasks.get(wid)
                    subtask_id = _abort_result_subtask_id(
                        subtask,
                        active_worker_worktrees.get(wid),
                    )
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_RUNAWAY_DIFF",
                        "blocker_kind": "worker_runaway_diff",
                        "output_excerpt": (
                            f"Worker diff exceeded ACA runaway guard ({diff_bytes} bytes across "
                            f"{diff_lines} lines; max {max_bytes}). ACA preserved a clipped summary "
                            "and abandoned this worker instead of writing a giant patch artifact."
                        ),
                        "recovery_action": (
                            "Block this run and inspect the clipped diff evidence before resetting the task. "
                            "The next prompt must inspect diff stats before continuing after large generated edits."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                        **_subtask_retry_metadata(subtask),
                    }
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.runaway_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            now = time.monotonic()
            elapsed_seconds = max(0.0, now - started_at)
            testless_abort_seconds = _effective_worker_testless_diff_abort_seconds(ctx, subtask, wid)
            test_only_abort_seconds = _effective_worker_test_only_diff_abort_seconds(ctx, subtask, wid)
            comment_only_abort_seconds = _worker_comment_only_diff_abort_seconds(ctx)
            required_test_files = _subtask_required_test_files(subtask)
            testless_guard_active = (
                _subtask_requires_test_changes(subtask)
                and not _changed_files_satisfy_required_test_files(validation_changed_files, required_test_files)
            )
            test_only_guard_active = _subtask_has_required_test_only_diff(subtask, validation_changed_files)
            testless_guard_elapsed = _one_sided_guard_elapsed_seconds(
                active_worker_one_sided_guard_seen_at,
                wid,
                "testless",
                testless_guard_active,
                now,
            )
            test_only_guard_elapsed = _one_sided_guard_elapsed_seconds(
                active_worker_one_sided_guard_seen_at,
                wid,
                "test_only",
                test_only_guard_active,
                now,
            )
            if (
                testless_abort_seconds > 0
                and testless_guard_active
                and testless_guard_elapsed >= testless_abort_seconds
            ):
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker drifted off required regression/test coverage\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## off-track guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- guard_elapsed_seconds: {testless_guard_elapsed:.1f}\n"
                    f"- abort_seconds: {testless_abort_seconds:.1f}\n"
                    f"- required_test_files: {required_test_files}\n"
                    "- reason: subtask requires test/regression coverage but the worker has only changed non-test files\n\n"
                    f"## git diff --binary\n\n{diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "validation_changed_files": list(validation_changed_files),
                    "baseline_changed_files": list(baseline.get("baseline_changed_files") or []),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_off_track_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "guard_elapsed_seconds": round(testless_guard_elapsed, 1),
                    "abort_seconds": testless_abort_seconds,
                    "required_test_files": required_test_files,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = str(subtask.get("id") or "").strip()
                with active_workers_lock:
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "validation_changed_files": list(validation_changed_files),
                        "baseline_changed_files": list(baseline.get("baseline_changed_files") or []),
                        "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                        "blocker_kind": "worker_off_track",
                        "output_excerpt": (
                            "Worker drifted off the required regression/test coverage path: "
                            f"after {testless_guard_elapsed:.0f}s in a source-only diff state "
                            f"({elapsed_seconds:.0f}s total) it had changed only non-test files "
                            f"while required test files were {', '.join(required_test_files)}."
                        ),
                        "recovery_action": (
                            "Retry from a clean checkout. First read and edit the required test file, "
                            "then make any minimal production change needed for those assertions."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                        **_subtask_retry_metadata(subtask),
                    }
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.off_track_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            if (
                test_only_abort_seconds > 0
                and test_only_guard_active
                and test_only_guard_elapsed >= test_only_abort_seconds
            ):
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker changed only required test files without production implementation\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## test-only guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- guard_elapsed_seconds: {test_only_guard_elapsed:.1f}\n"
                    f"- abort_seconds: {test_only_abort_seconds:.1f}\n"
                    f"- required_test_files: {required_test_files}\n"
                    "- reason: subtask requires a production-path regression fix but the worker has only changed tests\n\n"
                    f"## git diff --binary\n\n{diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_test_only_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "guard_elapsed_seconds": round(test_only_guard_elapsed, 1),
                    "abort_seconds": test_only_abort_seconds,
                    "required_test_files": required_test_files,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = str(subtask.get("id") or "").strip()
                with active_workers_lock:
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_TEST_ONLY_DIFF",
                        "blocker_kind": "worker_incomplete_diff",
                        "output_excerpt": (
                            "Worker changed only required test files for a regression subtask: "
                            f"after {test_only_guard_elapsed:.0f}s in a test-only diff state "
                            f"({elapsed_seconds:.0f}s total) it had not made the required production change."
                        ),
                        "recovery_action": (
                            "Retry from a clean checkout. Preserve the useful test intent, but require the "
                            "worker to implement the production path in the same attempt before returning."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                        **_subtask_retry_metadata(subtask),
                    }
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.test_only_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            unproductive_reason = ""
            if _diff_has_unproductive_marker(diff_text):
                unproductive_reason = "worker diff contains an explicit placeholder/blocker marker"
            elif _diff_has_placeholder_noop_test(diff_text):
                unproductive_reason = "worker diff adds an explicit placeholder/no-op test"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and (missing_followups := _diff_changed_files_missing_substantive_production_followup(
                    validation_diff_text,
                    validation_changed_files,
                    subtask,
                ))
            ):
                unproductive_reason = (
                    "worker carried a test-only partial diff but did not make a substantive production "
                    "follow-up change in: " + ", ".join(missing_followups)
                )
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _diff_is_comment_only(diff_text)
            ):
                unproductive_reason = "worker diff is comment-only after the comment-only guard budget"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and (missing_calls := _diff_missing_production_function_calls(worktree, diff_text, changed_files))
            ):
                unproductive_reason = (
                    "worker test-only diff calls missing production helper(s): "
                    + ", ".join(missing_calls)
                )
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and _diff_is_local_string_oracle_test(diff_text)
            ):
                unproductive_reason = "worker test-only diff asserts hardcoded local strings instead of production behavior"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and _diff_has_tautological_boolean_assertion(diff_text)
            ):
                unproductive_reason = "worker diff contains only tautological boolean assertions"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and _diff_is_string_only_change(diff_text)
            ):
                unproductive_reason = "worker diff changes only string wording in tests"
            if unproductive_reason:
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker produced an unproductive placeholder/comment-only diff\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## unproductive diff guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- comment_only_abort_seconds: {comment_only_abort_seconds:.1f}\n"
                    f"- reason: {unproductive_reason}\n\n"
                    f"## git diff --binary\n\n{diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_unproductive_diff_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "comment_only_abort_seconds": comment_only_abort_seconds,
                    "reason": unproductive_reason,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = str(subtask.get("id") or "").strip()
                with active_workers_lock:
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_UNPRODUCTIVE_DIFF",
                        "blocker_kind": "worker_unproductive_diff",
                        "output_excerpt": (
                            "Worker produced an unproductive partial diff: "
                            f"{unproductive_reason}. ACA preserved the patch and abandoned "
                            "this worker instead of waiting for another engine timeout."
                        ),
                        "recovery_action": (
                            "Retry from a clean checkout with a smaller repair prompt. Require a real "
                            "production-path assertion or implementation change before any comments or blockers."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                        **_subtask_retry_metadata(subtask),
                    }
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.unproductive_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            verifiable_abort_seconds = _worker_verifiable_diff_abort_seconds(ctx)
            syntax_errors = _changed_python_syntax_errors(worktree, validation_changed_files)
            if (
                verifiable_abort_seconds > 0
                and elapsed_seconds >= verifiable_abort_seconds
                and syntax_errors
                and _subtask_has_verifiable_source_and_test_diff(subtask, validation_changed_files)
            ):
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker produced source and required-test changes with Python syntax errors before terminal result\n\n"
                    f"## changed files\n\n{validation_status_rows}\n\n"
                    "## syntax errors\n\n"
                    + "\n".join(f"- {error}" for error in syntax_errors[:20])
                    + "\n\n## verifiable diff guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- abort_seconds: {verifiable_abort_seconds:.1f}\n"
                    f"- required_test_files: {required_test_files}\n"
                    "- reason: subtask has production and required-test changes but changed Python files do not parse; retry should fix syntax before verification\n\n"
                    f"## git diff --binary\n\n{validation_diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(validation_changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_syntax_invalid_diff_guard",
                    "diff_bytes": validation_diff_bytes,
                    "diff_lines": validation_diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "abort_seconds": verifiable_abort_seconds,
                    "required_test_files": required_test_files,
                    "syntax_errors": syntax_errors[:20],
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = str(subtask.get("id") or "").strip()
                with active_workers_lock:
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(validation_changed_files),
                        "failure_reason": "WORKER_SYNTAX_INVALID_DIFF",
                        "blocker_kind": "worker_incomplete_diff",
                        "output_excerpt": (
                            "Worker produced a source plus required-test partial diff, but changed Python files "
                            f"did not parse after {elapsed_seconds:.0f}s: " + "; ".join(syntax_errors[:5])
                        ),
                        "recovery_action": (
                            "Retry with the preserved patch already applied. Fix the reported Python syntax errors first, "
                            "then run the narrow verification before returning a terminal result."
                        ),
                        "syntax_errors": syntax_errors[:20],
                        "write_required": True,
                        "verified_existing": False,
                        **_subtask_retry_metadata(subtask),
                    }
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.syntax_invalid_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            if (
                verifiable_abort_seconds > 0
                and elapsed_seconds >= verifiable_abort_seconds
                and _subtask_has_verifiable_source_and_test_diff(subtask, validation_changed_files)
            ):
                if not _diff_has_substantive_required_test_addition(validation_diff_text, required_test_files):
                    artifact_path.write_text(
                        "# Partial worker diff captured during worker progress heartbeat\n"
                        "# Reason: active worker produced source and required-test file changes without a substantive test assertion before terminal result\n\n"
                        f"## changed files\n\n{validation_status_rows}\n\n"
                        "## verifiable diff guard\n\n"
                        f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                        f"- abort_seconds: {verifiable_abort_seconds:.1f}\n"
                        f"- required_test_files: {required_test_files}\n"
                        "- reason: changed test files did not add a test method or assertion; retry should add meaningful regression coverage before sync\n\n"
                        f"## git diff --binary\n\n{validation_diff_text}\n",
                        encoding="utf-8",
                    )
                    snapshot = {
                        "worker_id": wid,
                        "partial_diff_artifact": str(artifact_path),
                        "changed_files": list(validation_changed_files),
                        "partial_diff_state": "preserved_not_accepted",
                        "source": "worker_verifiable_diff_weak_test_guard",
                        "diff_bytes": validation_diff_bytes,
                        "diff_lines": validation_diff_lines,
                        "elapsed_seconds": round(elapsed_seconds, 1),
                        "abort_seconds": verifiable_abort_seconds,
                        "required_test_files": required_test_files,
                    }
                    active_worker_snapshot_digests[wid] = digest
                    active_worker_progress_snapshots[wid] = snapshot
                    with active_workers_lock:
                        active_worker_abort_results[wid] = {
                            "worker_id": wid,
                            "subtask_id": subtask_id,
                            "status": "failed",
                            "returncode": 1,
                            "partial_diff_state": "preserved_not_accepted",
                            "partial_diff_artifact": str(artifact_path),
                            "artifacts": {"partial_diff": str(artifact_path)},
                            "changed_files": list(validation_changed_files),
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                            "blocker_kind": "worker_incomplete_diff",
                            "output_excerpt": (
                                "Worker produced source plus required-test file changes, but the test diff did not add "
                                f"a test method or assertion after {elapsed_seconds:.0f}s."
                            ),
                            "recovery_action": (
                                "Retry with the preserved patch already applied. Add a meaningful regression assertion "
                                "in the required test file before syncing the diff."
                            ),
                            "write_required": True,
                            "verified_existing": False,
                            **_subtask_retry_metadata(subtask),
                        }
                    _clear_active_worker_attempt_marker(ctx, wid)
                    append_event(
                        ctx.layout["events"],
                        "worker.verifiable_diff_weak_test",
                        ctx.run_id,
                        snapshot,
                        task_id=ctx.task.get("task_id"),
                        role="worker",
                        repo={"path": ctx.repo.get("path")},
                    )
                    return snapshot
                missing_symbols = _diff_required_tests_missing_added_public_symbols(
                    validation_diff_text,
                    required_test_files,
                )
                if missing_symbols:
                    missing_symbols_text = ", ".join(missing_symbols[:8])
                    artifact_path.write_text(
                        "# Partial worker diff captured during worker progress heartbeat\n"
                        "# Reason: active worker produced source and required-test file changes, but the tests did not exercise the new production API\n\n"
                        f"## changed files\n\n{validation_status_rows}\n\n"
                        "## verifiable diff guard\n\n"
                        f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                        f"- abort_seconds: {verifiable_abort_seconds:.1f}\n"
                        f"- required_test_files: {required_test_files}\n"
                        f"- missing_production_symbols: {missing_symbols}\n"
                        "- reason: required test additions did not reference any newly introduced public production symbol; retry should test the API added by the source diff\n\n"
                        f"## git diff --binary\n\n{validation_diff_text}\n",
                        encoding="utf-8",
                    )
                    snapshot = {
                        "worker_id": wid,
                        "partial_diff_artifact": str(artifact_path),
                        "changed_files": list(validation_changed_files),
                        "partial_diff_state": "preserved_not_accepted",
                        "source": "worker_verifiable_diff_misaligned_test_guard",
                        "diff_bytes": validation_diff_bytes,
                        "diff_lines": validation_diff_lines,
                        "elapsed_seconds": round(elapsed_seconds, 1),
                        "abort_seconds": verifiable_abort_seconds,
                        "required_test_files": required_test_files,
                        "missing_production_symbols": missing_symbols,
                    }
                    active_worker_snapshot_digests[wid] = digest
                    active_worker_progress_snapshots[wid] = snapshot
                    subtask_id = str(subtask.get("id") or "").strip()
                    with active_workers_lock:
                        active_worker_abort_results[wid] = {
                            "worker_id": wid,
                            "subtask_id": subtask_id,
                            "status": "failed",
                            "returncode": 1,
                            "partial_diff_state": "preserved_not_accepted",
                            "partial_diff_artifact": str(artifact_path),
                            "artifacts": {"partial_diff": str(artifact_path)},
                            "changed_files": list(validation_changed_files),
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_WEAK_TEST",
                            "blocker_kind": "worker_incomplete_diff",
                            "output_excerpt": (
                                "Worker produced source plus required-test file changes, but the required test "
                                "additions did not exercise newly introduced production symbol(s) after "
                                f"{elapsed_seconds:.0f}s: {missing_symbols_text}."
                            ),
                            "recovery_action": (
                                "Retry with the preserved patch already applied. Update the required test file so "
                                "it calls the production API added by the source diff before syncing the diff."
                            ),
                            "write_required": True,
                            "verified_existing": False,
                            **_subtask_retry_metadata(subtask),
                        }
                    _clear_active_worker_attempt_marker(ctx, wid)
                    append_event(
                        ctx.layout["events"],
                        "worker.verifiable_diff_misaligned_test",
                        ctx.run_id,
                        snapshot,
                        task_id=ctx.task.get("task_id"),
                        role="worker",
                        repo={"path": ctx.repo.get("path")},
                    )
                    return snapshot
                test_result = _changed_python_tests_result(worktree, validation_changed_files)
                if test_result is not None and not bool(test_result.get("ok")):
                    command = [str(part) for part in test_result.get("command") or []]
                    output = str(test_result.get("output") or "").strip()
                    subtask_id = str(subtask.get("id") or "").strip()
                    if not _active_worker_snapshot_still_current(wid, worktree, subtask_id):
                        return None
                    artifact_path.write_text(
                        "# Partial worker diff captured during worker progress heartbeat\n"
                        "# Reason: active worker produced source and required-test changes that failed focused tests before terminal result\n\n"
                        f"## changed files\n\n{validation_status_rows}\n\n"
                        "## focused verification\n\n"
                        f"- command: {' '.join(command)}\n"
                        f"- returncode: {test_result.get('returncode')}\n"
                        f"- timed_out: {bool(test_result.get('timed_out'))}\n\n"
                        "## test output\n\n"
                        f"{output[:8000] or '(no output)'}\n\n"
                        "## verifiable diff guard\n\n"
                        f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                        f"- abort_seconds: {verifiable_abort_seconds:.1f}\n"
                        f"- required_test_files: {required_test_files}\n"
                        "- reason: subtask has production and required-test changes but focused tests failed; retry should fix the preserved patch before sync\n\n"
                        f"## git diff --binary\n\n{validation_diff_text}\n",
                        encoding="utf-8",
                    )
                    snapshot = {
                        "worker_id": wid,
                        "partial_diff_artifact": str(artifact_path),
                        "changed_files": list(validation_changed_files),
                        "partial_diff_state": "preserved_not_accepted",
                        "source": "worker_verifiable_diff_test_failed_guard",
                        "diff_bytes": validation_diff_bytes,
                        "diff_lines": validation_diff_lines,
                        "elapsed_seconds": round(elapsed_seconds, 1),
                        "abort_seconds": verifiable_abort_seconds,
                        "required_test_files": required_test_files,
                        "verification_command": command,
                        "verification_returncode": test_result.get("returncode"),
                        "verification_timed_out": bool(test_result.get("timed_out")),
                    }
                    active_worker_snapshot_digests[wid] = digest
                    active_worker_progress_snapshots[wid] = snapshot
                    subtask_id = str(subtask.get("id") or "").strip()
                    with active_workers_lock:
                        active_worker_abort_results[wid] = {
                            "worker_id": wid,
                            "subtask_id": subtask_id,
                            "status": "failed",
                            "returncode": 1,
                            "partial_diff_state": "preserved_not_accepted",
                            "partial_diff_artifact": str(artifact_path),
                            "artifacts": {"partial_diff": str(artifact_path)},
                            "changed_files": list(validation_changed_files),
                            "failure_reason": "WORKER_VERIFIABLE_DIFF_TEST_FAILED",
                            "blocker_kind": "worker_incomplete_diff",
                            "output_excerpt": (
                                "Worker produced a source plus required-test partial diff, but focused tests failed "
                                f"after {elapsed_seconds:.0f}s: {output[:2000]}"
                            ),
                            "verification_command": command,
                            "verification_returncode": test_result.get("returncode"),
                            "verification_timed_out": bool(test_result.get("timed_out")),
                            "verification_output_excerpt": output[:4000],
                            "recovery_action": (
                                "Retry with the preserved source+test patch already applied. Fix the focused test failure, "
                                "then rerun the reported verification command before returning a terminal result."
                            ),
                            "write_required": True,
                            "verified_existing": False,
                            **_subtask_retry_metadata(subtask),
                        }
                    _clear_active_worker_attempt_marker(ctx, wid)
                    append_event(
                        ctx.layout["events"],
                        "worker.verifiable_diff_tests_failed",
                        ctx.run_id,
                        snapshot,
                        task_id=ctx.task.get("task_id"),
                        role="worker",
                        repo={"path": ctx.repo.get("path")},
                    )
                    return snapshot
            if (
                verifiable_abort_seconds > 0
                and elapsed_seconds >= verifiable_abort_seconds
                and _subtask_has_verifiable_source_and_test_diff(subtask, validation_changed_files)
            ):
                terminal_test_result = _changed_python_tests_result(worktree, validation_changed_files)
                verification_command = [
                    str(part) for part in (terminal_test_result or {}).get("command") or []
                ]
                verification_output = str((terminal_test_result or {}).get("output") or "").strip()
                verification_status = "not_inferred"
                if terminal_test_result is not None:
                    verification_status = "passed" if bool(terminal_test_result.get("ok")) else "failed"
                subtask_id = str(subtask.get("id") or "").strip()
                if not _active_worker_snapshot_still_current(wid, worktree, subtask_id):
                    return None
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker produced source and required-test changes but no terminal worker result\n\n"
                    f"## changed files\n\n{validation_status_rows}\n\n"
                    "## focused verification\n\n"
                    f"- status: {verification_status}\n"
                    f"- command: {' '.join(verification_command) if verification_command else '(not inferred)'}\n"
                    f"- returncode: {(terminal_test_result or {}).get('returncode')}\n"
                    f"- timed_out: {bool((terminal_test_result or {}).get('timed_out'))}\n\n"
                    "## verification output\n\n"
                    f"{verification_output[:8000] or '(no output)'}\n\n"
                    "## verifiable diff guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- abort_seconds: {verifiable_abort_seconds:.1f}\n"
                    f"- required_test_files: {required_test_files}\n"
                    "- reason: subtask has production and required-test changes, but the worker did not return a terminal result; retry should finish or reject the preserved patch instead of counting it complete\n\n"
                    f"## git diff --binary\n\n{validation_diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(validation_changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_verifiable_diff_guard",
                    "diff_bytes": validation_diff_bytes,
                    "diff_lines": validation_diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "abort_seconds": verifiable_abort_seconds,
                    "required_test_files": required_test_files,
                    "verification_status": verification_status,
                    "verification_command": verification_command,
                    "verification_returncode": (terminal_test_result or {}).get("returncode"),
                    "verification_timed_out": bool((terminal_test_result or {}).get("timed_out")),
                }
                verification_note = ""
                if verification_status == "passed":
                    verification_note = " Focused changed-file tests passed, but that is not a substitute for a terminal worker verdict."
                elif verification_status == "failed":
                    verification_note = f" Focused changed-file tests failed: {verification_output[:1000]}"
                else:
                    verification_note = " ACA could not infer a focused changed-file test command."
                result = {
                    "worker_id": wid,
                    "subtask_id": subtask_id,
                    "status": "failed",
                    "returncode": 1,
                    "partial_diff_state": "preserved_not_accepted",
                    "partial_diff_artifact": str(artifact_path),
                    "artifacts": {"partial_diff": str(artifact_path)},
                    "changed_files": list(validation_changed_files),
                    "failure_reason": "WORKER_VERIFIABLE_DIFF_UNTERMINATED",
                    "blocker_kind": "worker_incomplete_diff",
                    "output_excerpt": (
                        "Worker produced a source plus required-test partial diff, but did not return a terminal "
                        f"result after {elapsed_seconds:.0f}s."
                        + verification_note
                    ),
                    "verification_command": verification_command,
                    "verification_returncode": (terminal_test_result or {}).get("returncode"),
                    "verification_timed_out": bool((terminal_test_result or {}).get("timed_out")),
                    "verification_output_excerpt": verification_output[:4000],
                    "recovery_action": (
                        "Retry with the preserved source+test patch already applied. Finish or reject the patch "
                        "with a terminal worker verdict, and rerun the focused verification before completion."
                    ),
                    "write_required": True,
                    "verified_existing": False,
                    **_subtask_retry_metadata(subtask),
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                with active_workers_lock:
                    active_worker_abort_results[wid] = result
                _clear_active_worker_attempt_marker(ctx, wid)
                append_event(
                    ctx.layout["events"],
                    "worker.verifiable_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                append_event(
                    ctx.layout["events"],
                    "worker.failed",
                    ctx.run_id,
                    {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "returncode": result["returncode"],
                        "partial_diff_state": result.get("partial_diff_state"),
                        "partial_diff_artifact": result.get("partial_diff_artifact"),
                        "changed_files": result.get("changed_files"),
                        "synced_files": result.get("synced_files"),
                        "failure_reason": result.get("failure_reason"),
                        "blocker_kind": result.get("blocker_kind"),
                        "recovery_action": result.get("recovery_action"),
                        "verification_command": verification_command,
                        "verification_returncode": result.get("verification_returncode"),
                        "verification_timed_out": result.get("verification_timed_out"),
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            if same_digest and previous_snapshot:
                return previous_snapshot
            artifact_path.write_text(
                "# Partial worker diff captured during worker progress heartbeat\n"
                "# Reason: active worker had filesystem changes before terminal result\n\n"
                f"## changed files\n\n{status_rows}\n\n"
                f"## git diff --binary\n\n{diff_text}\n",
                encoding="utf-8",
            )
            snapshot = {
                "worker_id": wid,
                "partial_diff_artifact": str(artifact_path),
                "changed_files": list(changed_files),
                "partial_diff_state": "preserved_not_accepted",
                "source": "worker_progress_snapshot",
                "diff_bytes": diff_bytes,
                "diff_lines": diff_lines,
            }
            active_worker_snapshot_digests[wid] = digest
            active_worker_progress_snapshots[wid] = snapshot
            append_event(
                ctx.layout["events"],
                "worker.progress_partial_diff_snapshot",
                ctx.run_id,
                snapshot,
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
            return snapshot
        except Exception:
            logger.debug("Failed to snapshot worker progress diff for %s", wid, exc_info=True)
            return None

    def _schedule_worker_progress_snapshot(wid: str, worktree: Path) -> None:
        """Run expensive progress diff capture away from the lease heartbeat."""

        with active_workers_lock:
            if wid in active_worker_snapshot_inflight:
                return
            active_worker_snapshot_inflight.add(wid)

        def _run_snapshot() -> None:
            try:
                _snapshot_worker_progress_diff(wid, worktree)
            finally:
                with active_workers_lock:
                    active_worker_snapshot_inflight.discard(wid)

        thread = threading.Thread(
            target=_run_snapshot,
            name=f"aca-worker-progress-snapshot-{wid}",
            daemon=True,
        )
        active_worker_snapshot_threads.append(thread)
        thread.start()

    def _maybe_abort_no_change_repair_worker(wid: str, worktree: Path) -> None:
        with active_workers_lock:
            if active_worker_abort_results.get(wid):
                return
            subtask = dict(active_worker_subtasks.get(wid) or {})
            started_at = float(active_worker_started_at.get(wid) or time.monotonic())
        baseline = _active_worker_start_baseline(wid, subtask)
        if baseline is None:
            return
        retry_write_required = _latest_worker_retry_write_required(ctx, wid, subtask)
        if retry_write_required is not None:
            subtask["write_required"] = retry_write_required
        if _subtask_is_repair_no_change_guard_candidate(subtask):
            abort_seconds = _effective_worker_repair_no_change_abort_seconds(ctx, subtask, wid)
            configured_abort_seconds = _worker_repair_no_change_abort_seconds(ctx)
            event_type = "worker.repair_no_change_detected"
            failure_reason = "WORKER_REPAIR_NO_CHANGE"
            reason = "repair worker made no filesystem changes"
            excerpt = (
                "Repair worker made no filesystem changes before the no-change guard fired: "
                "after {elapsed:.0f}s it had not edited any target files."
            )
            recovery_action = (
                "Retry with a smaller repair prompt or healthier engine route. The worker should read and edit "
                "the first required target before spending a full prompt budget."
            )
        elif _subtask_is_no_change_guard_candidate(subtask):
            abort_seconds = _effective_worker_no_change_abort_seconds(ctx, subtask, wid)
            configured_abort_seconds = _worker_no_change_abort_seconds(ctx)
            event_type = "worker.no_change_detected"
            failure_reason = "WORKER_NO_CHANGE"
            reason = "write-required worker made no filesystem changes"
            excerpt = (
                "Write-required worker made no filesystem changes before the no-change guard fired: "
                "after {elapsed:.0f}s it had not edited any target files."
            )
            recovery_action = (
                "Retry with a smaller worker prompt or healthier engine route. The worker should read and edit "
                "a declared target before spending a full prompt budget."
            )
        else:
            return
        if abort_seconds <= 0:
            return
        elapsed_seconds = max(0.0, time.monotonic() - started_at)
        tool_loop_abort_seconds = _worker_no_diff_tool_loop_abort_seconds(ctx)
        tool_loop_summary: dict[str, Any] | None = None
        engine_silence_summary: dict[str, Any] | None = None
        blocker_kind = "worker_no_progress"
        if tool_loop_abort_seconds > 0 and elapsed_seconds >= tool_loop_abort_seconds:
            if not _worktree_has_subtask_changes(worktree, subtask, baseline):
                tool_loop_summary = _active_worker_no_diff_tool_loop(ctx, wid)
        if not tool_loop_summary and elapsed_seconds < abort_seconds:
            return
        if _worktree_has_subtask_changes(worktree, subtask, baseline):
            return
        subtask_id = str(subtask.get("id") or "").strip()
        if tool_loop_summary:
            event_type = "worker.no_diff_tool_loop_detected"
            failure_reason = "WORKER_NO_DIFF_TOOL_LOOP"
            reason = str(tool_loop_summary.get("reason") or "worker tool loop produced no filesystem changes")
            excerpt = (
                "Write-required worker produced no filesystem changes while tool calls were already failing "
                "unproductively: {reason}. invalid_patch_count={invalid_patch_count}; "
                "noop_edit_count={noop_edit_count}; tool_parts={tool_parts}; paths={paths}."
            )
            recovery_action = (
                "Retry with a smaller worker prompt and a narrower target-file contract. If the same pattern repeats, "
                "route away from the current engine/tool path before spending another full prompt budget."
            )
        else:
            engine_silence_summary = _active_worker_engine_silence_summary(ctx, wid)
            if engine_silence_summary:
                event_type = "worker.engine_empty_response_detected"
                failure_reason = "ENGINE_EMPTY_RESPONSE"
                blocker_kind = "engine_empty_response"
                reason = str(
                    engine_silence_summary.get("reason")
                    or "active worker engine session produced no assistant transcript"
                )
                excerpt = (
                    "Write-required worker produced no filesystem changes and the active engine session "
                    "had no assistant or tool activity after {elapsed:.0f}s. "
                    "session_id={session_id}; run_id={run_id}; message_count={message_count}."
                )
                recovery_action = (
                    "Retry through a fresh engine session or alternate route; ACA should not treat this "
                    "as a semantic worker repair failure."
                )
        result = {
            "worker_id": wid,
            "subtask_id": subtask_id,
            "status": "failed",
            "returncode": 1,
            "failure_reason": failure_reason,
            "blocker_kind": blocker_kind,
            "output_excerpt": excerpt.format(
                elapsed=elapsed_seconds,
                reason=reason,
                invalid_patch_count=(tool_loop_summary or {}).get("invalid_patch_count", 0),
                noop_edit_count=(tool_loop_summary or {}).get("noop_edit_count", 0),
                tool_parts=(tool_loop_summary or {}).get("tool_parts", 0),
                paths=", ".join((tool_loop_summary or {}).get("paths") or []),
                session_id=(engine_silence_summary or {}).get("session_id", ""),
                run_id=(engine_silence_summary or {}).get("run_id", ""),
                message_count=(engine_silence_summary or {}).get("message_count", 0),
            ),
            "recovery_action": recovery_action,
            "write_required": True,
            "verified_existing": False,
            **({"tool_loop_summary": tool_loop_summary} if tool_loop_summary else {}),
            **({"engine_silence_summary": engine_silence_summary} if engine_silence_summary else {}),
            **({"engine": engine_silence_summary} if engine_silence_summary else {}),
        }
        _attach_carried_partial_diff_to_repair_no_change_result(result, subtask, baseline)
        with active_workers_lock:
            active_worker_abort_results[wid] = result
        _clear_active_worker_attempt_marker(ctx, wid)
        append_event(
            ctx.layout["events"],
            event_type,
            ctx.run_id,
            {
                "worker_id": wid,
                "subtask_id": subtask_id,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "abort_seconds": abort_seconds,
                "configured_abort_seconds": configured_abort_seconds,
                "reason": reason,
                **({"engine_silence_summary": engine_silence_summary} if engine_silence_summary else {}),
            },
            task_id=ctx.task.get("task_id"),
            role="worker",
            repo={"path": ctx.repo.get("path")},
        )

    def _attach_progress_snapshot_to_failed_result(result: dict[str, Any]) -> None:
        if result.get("returncode") == 0 or result.get("partial_diff_artifact"):
            return
        blocker_kind = str(result.get("blocker_kind") or "").strip()
        if blocker_kind not in {"engine_prompt_timeout", "engine_tool_loop_stalled", "worker_no_progress"}:
            return
        wid = str(result.get("worker_id") or "").strip()
        snapshot = active_worker_progress_snapshots.get(wid)
        if not snapshot:
            return
        result.setdefault("artifacts", {})["partial_diff"] = snapshot["partial_diff_artifact"]
        result["partial_diff_artifact"] = snapshot["partial_diff_artifact"]
        result["changed_files"] = list(snapshot.get("changed_files") or [])
        result["progress_partial_diff_recovered"] = True
        result["engine_blocker_kind"] = blocker_kind
        result["blocker_kind"] = "worker_incomplete_diff"
        result["recovery_action"] = (
            "ACA captured a progress-time partial diff before the engine timeout; "
            "inspect that artifact and retry from a clean checkout."
        )
        append_event(
            ctx.layout["events"],
            "worker.partial_diff_preserved",
            ctx.run_id,
            {
                "worker_id": wid,
                "subtask_id": str(result.get("subtask_id") or "").strip(),
                "partial_diff_state": "preserved_not_accepted",
                "partial_diff_artifact": snapshot["partial_diff_artifact"],
                "changed_files": list(snapshot.get("changed_files") or []),
                "failure_reason": result.get("failure_reason"),
                "blocker_kind": result.get("blocker_kind"),
                "recovery_action": result.get("recovery_action"),
                "source": "worker_progress_snapshot",
            },
            task_id=ctx.task.get("task_id"),
            role="worker",
            repo={"path": ctx.repo.get("path")},
        )

    def _heartbeat_local_workers() -> None:
        while True:
            with active_workers_lock:
                sleep_subtasks = {
                    wid: dict(subtask or {})
                    for wid, subtask in active_worker_subtasks.items()
                }
            if not sleep_subtasks:
                sleep_subtasks = {
                    f"pending-{index + 1}": dict(subtask or {})
                    for index, subtask in enumerate(ctx.pending_subtasks)
                }
            sleep_s = _worker_progress_snapshot_sleep_seconds(ctx, sleep_subtasks)
            if worker_heartbeat_stop.wait(sleep_s):
                break
            _rc._touch_coordination(
                ctx.coordination,
                run_id=ctx.run_id,
                lease_id=ctx.lease_id,
                lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                status="running",
                phase="worker_execution",
                ctx=ctx,
            )
            with active_workers_lock:
                ids = list(active_workers)
                started_at = dict(active_worker_started_at)
                worktrees = dict(active_worker_worktrees)
            for wid in ids:
                try:
                    ctx.coordination.heartbeat_worker(
                        wid,
                        host_id=ctx.claim_identity["host_id"],
                        role="worker",
                        status="busy",
                        capabilities=worker_capabilities,
                        current_run_id=ctx.run_id,
                        current_lease_id=worker_lease_id,
                    )
                except Exception:
                    logger.debug("Heartbeat failed for worker %s", wid, exc_info=True)
                worktree = worktrees.get(wid)
                if worktree:
                    _schedule_worker_progress_snapshot(wid, worktree)
                    _maybe_abort_no_change_repair_worker(wid, worktree)
            now = time.monotonic()
            progress_interval = max(30.0, float(ctx.cfg.coordination.heartbeat_interval_seconds or 1) * 2.0)
            nonlocal last_progress_event_at
            if ids and now - last_progress_event_at >= progress_interval:
                last_progress_event_at = now
                elapsed_by_worker = {
                    wid: round(max(0.0, now - float(started_at.get(wid, now))), 1)
                    for wid in ids
                }
                detail = ", ".join(
                    f"{wid} running for {elapsed_by_worker[wid]:.0f}s" for wid in ids[:3]
                )
                phase = ctx.status.get("phase") if isinstance(ctx.status.get("phase"), dict) else {}
                phase["detail"] = detail or "worker still running"
                ctx.status["phase"] = phase
                append_event(
                    ctx.layout["events"],
                    "worker.progress",
                    ctx.run_id,
                    {
                        "active_workers": ids,
                        "elapsed_seconds_by_worker": elapsed_by_worker,
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                write_status(ctx.layout["status"], ctx.status)

    def _on_result(result: dict[str, Any]) -> None:
        wid = str(result.get("worker_id") or "").strip()
        subtask_id = str(result.get("subtask_id") or "").strip()
        with active_workers_lock:
            result_worktree = active_worker_worktrees.get(wid)
            result_subtask = dict(active_worker_subtasks.get(wid) or {})
        if result_worktree is not None:
            baseline = _active_worker_start_baseline(wid, result_subtask)
            if baseline is not None:
                raw_changed_files = [
                    str(path or "").strip().replace("\\", "/")
                    for path in result.get("changed_files") or []
                    if str(path or "").strip()
                ]
                if raw_changed_files:
                    fresh_changed_files = _fresh_changed_files_since_baseline(
                        result_worktree,
                        raw_changed_files,
                        baseline,
                    )
                    effective_changed_files = _validation_changed_files_with_carried_baseline(
                        result_subtask,
                        fresh_changed_files,
                        baseline,
                    )
                    if effective_changed_files != raw_changed_files:
                        result["changed_files"] = effective_changed_files
                        filtered_artifact = _filter_result_partial_diff_artifact(result, effective_changed_files)
                        append_event(
                            ctx.layout["events"],
                            "worker.overlay_baseline_result_filtered",
                            ctx.run_id,
                            {
                                "worker_id": wid,
                                "subtask_id": subtask_id,
                                "original_changed_files": raw_changed_files,
                                "fresh_changed_files": fresh_changed_files,
                                "effective_changed_files": effective_changed_files,
                                "baseline_changed_files": list(baseline.get("baseline_changed_files") or []),
                                "filtered_partial_diff_artifact": filtered_artifact,
                            },
                            task_id=ctx.task.get("task_id"),
                            role="worker",
                            repo={"path": ctx.repo.get("path")},
                        )
        _attach_progress_snapshot_to_failed_result(result)
        if result_worktree and _failed_result_has_reviewable_source_and_test_diff(result, result_subtask):
            guarded_non_reusable_patch = _result_has_non_reusable_guarded_partial_diff(result)
            changed_files = [str(path or "").strip() for path in result.get("changed_files") or [] if str(path or "").strip()]
            rejection = _reviewable_failed_diff_rejection(result_worktree, result_subtask, changed_files)
            if rejection is not None:
                rejection_reason = str(rejection.get("reason") or "").strip()
                if rejection_reason == "misaligned_test_diff":
                    result["failure_reason"] = "WORKER_VERIFIABLE_DIFF_MISALIGNED_TEST"
                    missing_symbols = [
                        str(symbol or "").strip()
                        for symbol in rejection.get("missing_symbols") or []
                        if str(symbol or "").strip()
                    ]
                    if missing_symbols:
                        result["misaligned_test_missing_symbols"] = missing_symbols
                    result["output_excerpt"] = (
                        "Worker produced source plus required-test file changes, but the required test additions "
                        "did not exercise newly introduced production symbol(s): "
                        + ", ".join(missing_symbols[:8])
                    )
                else:
                    result["failure_reason"] = "WORKER_VERIFIABLE_DIFF_WEAK_TEST"
                    result["output_excerpt"] = (
                        "Worker produced source plus required-test file changes, but ACA rejected the diff before "
                        "syncing it: "
                        + str(rejection.get("message") or rejection_reason or "failed focused validation")
                    )
                result["blocker_kind"] = "worker_incomplete_diff"
                result["recovery_action"] = (
                    "ACA rejected the preserved source+test diff before syncing it: "
                    + str(rejection.get("message") or rejection.get("reason") or "failed focused validation")
                )
                if guarded_non_reusable_patch:
                    result["patch_reusable"] = False
                    guard_note = (
                        "ACA already flagged this partial diff with a runaway/destructive guard. "
                        "Keep the preserved artifact only as failure evidence and rebuild the repair from clean "
                        "target files."
                    )
                    result["output_excerpt"] = (
                        guard_note + "\n" + str(result.get("output_excerpt") or "").strip()
                    ).strip()
                    result["recovery_action"] = guard_note
                append_event(
                    ctx.layout["events"],
                    "worker.verifiable_failed_diff_rejected",
                    ctx.run_id,
                    {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "changed_files": changed_files,
                        "reason": rejection.get("reason"),
                        "message": rejection.get("message"),
                        "command": rejection.get("command"),
                        "returncode": rejection.get("returncode"),
                        "timed_out": rejection.get("timed_out"),
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
            else:
                sync_ok, merged_changed_files, synced_files, sync_error = _sync_verifiable_worker_diff(
                    ctx,
                    worker_id=wid,
                    subtask_id=subtask_id,
                    worktree=result_worktree,
                    changed_files=changed_files,
                )
                if sync_ok:
                    accepted_unterminated = (
                        str(result.get("failure_reason") or "").strip() == "WORKER_VERIFIABLE_DIFF_UNTERMINATED"
                        and int(result.get("verification_returncode") or 0) == 0
                        and not bool(result.get("verification_timed_out"))
                    )
                    result["status"] = "completed"
                    result["returncode"] = 0
                    result["partial_diff_state"] = "reviewable_terminalized"
                    result["changed_files"] = merged_changed_files
                    result["synced_files"] = synced_files
                    result["failure_reason"] = None
                    result["blocker_kind"] = None
                    result["recovery_action"] = None
                    result["output_excerpt"] = (
                        "Worker produced a source plus required-test diff and focused verification passed. "
                        "ACA accepted and synced the reviewable diff for manager review and tests."
                        if accepted_unterminated
                        else (
                            "Worker returned a blocker after producing a source plus required-test diff. "
                            "ACA synced the reviewable diff for manager review and tests instead of retrying it."
                        )
                    )
                    append_event(
                        ctx.layout["events"],
                        "worker.verifiable_diff_accepted" if accepted_unterminated else "worker.verifiable_failed_diff_synced",
                        ctx.run_id,
                        {
                            "worker_id": wid,
                            "subtask_id": subtask_id,
                            "changed_files": merged_changed_files,
                            "synced_files": synced_files,
                            "verification_returncode": result.get("verification_returncode"),
                            "verification_timed_out": result.get("verification_timed_out"),
                        },
                        task_id=ctx.task.get("task_id"),
                        role="worker",
                        repo={"path": ctx.repo.get("path")},
                    )
                else:
                    result["recovery_action"] = (
                        "ACA found a source plus required-test partial diff but could not sync it for review: "
                        + str(sync_error or "unknown sync error")
                    )
        elif result_worktree and _failed_result_has_reviewable_production_diff(result, result_subtask, result_worktree):
            changed_files = [str(path or "").strip() for path in result.get("changed_files") or [] if str(path or "").strip()]
            sync_ok, merged_changed_files, synced_files, sync_error = _sync_verifiable_worker_diff(
                ctx,
                worker_id=wid,
                subtask_id=subtask_id,
                worktree=result_worktree,
                changed_files=changed_files,
            )
            if sync_ok:
                result["status"] = "completed"
                result["returncode"] = 0
                result["partial_diff_state"] = "reviewable_terminalized"
                result["changed_files"] = merged_changed_files
                result["synced_files"] = synced_files
                result["failure_reason"] = None
                result["blocker_kind"] = None
                result["recovery_action"] = None
                result["output_excerpt"] = (
                    "Worker timed out after producing a scoped production diff that matches the positive "
                    "subtask contract. ACA synced the reviewable diff for manager review and tests instead "
                    "of retrying it."
                )
                append_event(
                    ctx.layout["events"],
                    "worker.reviewable_production_failed_diff_synced",
                    ctx.run_id,
                    {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "changed_files": merged_changed_files,
                        "synced_files": synced_files,
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
            else:
                result["recovery_action"] = (
                    "ACA found a scoped production partial diff but could not sync it for review: "
                    + str(sync_error or "unknown sync error")
                )
        if int(result.get("returncode") or 0) == 0:
            changed_files = [
                str(path or "").strip().replace("\\", "/")
                for path in result.get("changed_files") or []
                if str(path or "").strip()
            ]
            if not _changed_files_satisfy_primary_source_target(changed_files, result_subtask):
                primary_sources = _subtask_primary_source_files(result_subtask)
                result["status"] = "failed"
                result["returncode"] = 1
                result["failure_reason"] = "WORKER_PRIMARY_SOURCE_TARGET_MISSING"
                result["blocker_kind"] = "worker_off_target"
                result["recovery_action"] = (
                    "Retry with a narrower prompt that edits the worker's primary source target first: "
                    + ", ".join(primary_sources)
                )
                result["output_excerpt"] = (
                    "Worker completed without touching its primary source target. "
                    f"primary_source_files={primary_sources}; changed_files={changed_files}."
                )
                append_event(
                    ctx.layout["events"],
                    "worker.primary_source_target_missing",
                    ctx.run_id,
                    {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "changed_files": changed_files,
                        "primary_source_files": primary_sources,
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
        _rc._record_worker_result(ctx.blackboard, ctx.worker_results, result)
        for item in ctx.blackboard["subtasks"]:
            if item.get("id") == subtask_id:
                item["status"] = result.get("status") or "failed"
                break
        ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        write_status(ctx.layout["status"], ctx.status)
        if wid:
            ctx.coordination.register_worker(
                worker_id=wid,
                host_id=ctx.claim_identity["host_id"],
                role="worker",
                status="idle",
                capabilities=worker_capabilities,
                current_run_id=None,
                current_lease_id=None,
            )
            with active_workers_lock:
                active_workers.discard(wid)
                active_worker_started_at.pop(wid, None)
                active_worker_started_at_ms.pop(wid, None)
                active_worker_worktrees.pop(wid, None)
                active_worker_subtasks.pop(wid, None)
                active_worker_start_baselines.pop(wid, None)
                active_worker_overlay_baseline_ignored.discard(wid)
                active_worker_progress_snapshots.pop(wid, None)
                active_worker_snapshot_digests.pop(wid, None)
                active_worker_one_sided_guard_seen_at.pop(wid, None)
                active_worker_abort_results.pop(wid, None)

    def _on_start(wid: str, subtask: dict[str, Any]) -> None:
        wid = str(wid or "").strip()
        if not wid:
            return
        ctx.coordination.register_worker(
            worker_id=wid,
            host_id=ctx.claim_identity["host_id"],
            role="worker",
            status="busy",
            capabilities=worker_capabilities,
            current_run_id=ctx.run_id,
            current_lease_id=worker_lease_id,
        )
        with active_workers_lock:
            active_workers.add(wid)
            active_worker_started_at[wid] = time.monotonic()
            active_worker_started_at_ms[wid] = int(time.time() * 1000)
            subtask_id = str(subtask.get("id") or "").strip()
            if subtask_id:
                worktree_name = str(subtask.get("_worker_worktree_name") or "").strip()
                if not worktree_name or "/" in worktree_name or "\\" in worktree_name or ".." in worktree_name:
                    worktree_name = worker_worktree_name(wid, subtask_id)
                active_worker_worktrees[wid] = ctx.run_dir / "worktrees" / worktree_name
                active_worker_subtasks[wid] = dict(subtask)

    def _abort_result(index: int, subtask: dict[str, Any], wid: str) -> dict[str, Any] | None:
        with active_workers_lock:
            result = active_worker_abort_results.get(str(wid or ""))
        if not result:
            return None
        result = dict(result)
        result.setdefault("subtask_index", index)
        result.setdefault("subtask_id", subtask.get("id"))
        result.setdefault("title", subtask.get("title"))
        return result

    heartbeat_thread = threading.Thread(target=_heartbeat_local_workers, daemon=True)
    heartbeat_thread.start()

    try:
        logger.info(
            "Dispatching %d worker(s) (run_id=%s)", len(ctx.pending_subtasks), ctx.run_id
        )
        new_results = _rc._execute_local_worker_pool(
            ctx.cfg,
            ctx.run_id,
            ctx.repo_path,
            ctx.run_dir,
            ctx.task,
            ctx.pending_subtasks,
            max_parallel_workers,
            on_start=_on_start,
            on_result=_on_result,
            abort_result=_abort_result,
            cancel_worker=lambda wid, reason: _cancel_active_worker_engine_session(ctx, wid, reason),
            worker_timeout_seconds=_worker_no_progress_timeout_seconds(ctx, ctx.pending_subtasks),
        )
        # Merge any results that bypassed _on_result
        for r in new_results:
            if not any(
                existing.get("worker_id") == r.get("worker_id")
                and existing.get("subtask_id") == r.get("subtask_id")
                for existing in ctx.worker_results
            ):
                ctx.worker_results.append(r)
    finally:
        worker_heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
        with active_workers_lock:
            lingering = list(active_workers)
            active_workers.clear()
        for wid in lingering:
            try:
                ctx.coordination.register_worker(
                    worker_id=wid,
                    host_id=ctx.claim_identity["host_id"],
                    role="worker",
                    status="idle",
                    capabilities=worker_capabilities,
                    current_run_id=None,
                    current_lease_id=None,
                )
            except Exception:
                logger.debug("Failed to unregister lingering worker %s", wid, exc_info=True)
        for thread in list(active_worker_snapshot_threads):
            if thread.is_alive():
                thread.join(timeout=0.1)

    _apply_tolerated_failures(ctx)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)

    _post_dispatch_validation(ctx)


def _apply_tolerated_failures(ctx: RunContext) -> None:
    """Upgrade 'failed' results to 'tolerated_failure' when target files are present post-sync."""
    from src.tandem_agents.core.repository.repo_truth import subtask_satisfied
    from src.tandem_agents.core.execution import runner_core as _rc

    task_source = ctx.task.get("source") if isinstance(ctx.task, dict) else {}
    if isinstance(task_source, dict) and str(task_source.get("type") or "").strip() == "github_project":
        return
    if isinstance(task_source, dict) and str(task_source.get("type") or "").strip() == "linear":
        return
    if _rc._task_mentions_external_pr_candidates(ctx.task):
        return

    for result in ctx.worker_results:
        if result.get("status") != "failed":
            continue
        matching = next(
            (s for s in ctx.planned_subtasks if s["id"] == result.get("subtask_id")),
            None,
        )
        failure_reason = str(result.get("failure_reason") or "").upper()
        blocker_kind = str(result.get("blocker_kind") or "").lower()
        if (
            failure_reason.startswith("ENGINE_")
            or failure_reason.startswith("ENGINE_ERROR:")
            or blocker_kind.startswith("engine_")
            or blocker_kind in _TERMINAL_WORKER_BLOCKER_KINDS
            or (matching and (matching.get("pr_candidate_context") or matching.get("pr_candidate_refs")))
        ):
            continue
        if matching and subtask_satisfied(ctx.repo_path, matching):
            result["status"] = "tolerated_failure"
            result["verified_existing"] = True
            for item in ctx.blackboard["subtasks"]:
                if item.get("id") == result["subtask_id"]:
                    item["status"] = "tolerated_failure"
                    break
            _rc._append_blackboard_note(
                ctx.blackboard,
                f"Tolerated noisy worker `{result['worker_id']}` because its target files were present after sync.",
            )


def _post_dispatch_validation(ctx: RunContext) -> None:
    """Refresh repo_validation and coding_run_contract after worker execution."""
    from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    ctx.expected_repo_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    changed_files: list[str] = _rc._collect_worker_changed_files(ctx.worker_results)
    if _rc._task_mentions_external_pr_candidates(ctx.task):
        if changed_files:
            ctx.expected_repo_files = changed_files
    else:
        ctx.expected_repo_files = _rc._sticky_expected_repo_files(
            ctx.blackboard,
            ctx.expected_repo_files,
        )
        ctx.expected_repo_files = _rc._validation_expected_repo_files(
            ctx.repo_path,
            ctx.expected_repo_files,
            changed_files,
        )
        ctx.blackboard["expected_repo_files"] = ctx.expected_repo_files
    ctx.repo_validation = _rc._deterministic_repo_validation(ctx.repo_path, ctx.expected_repo_files)
    if changed_files:
        unexpected_files = _rc._pr_candidate_unexpected_changed_files(ctx.planned_subtasks, changed_files)
        if unexpected_files:
            ctx.repo_validation = dict(ctx.repo_validation)
            ctx.repo_validation["unexpected_files"] = unexpected_files
            ctx.repo_validation["ok"] = False
    coding_run_contract = build_coding_run_contract(
        run_id=ctx.run_id,
        task=ctx.task,
        repo_path=ctx.repo_path,
        branch_name=ctx.branch_name,
        expected_repo_files=ctx.expected_repo_files,
    )
    ctx.blackboard["repo_validation"] = ctx.repo_validation
    _rc._record_coding_run_contract(ctx.blackboard, coding_run_contract)

    if ctx.repo_validation.get("ok"):
        _rc._append_blackboard_note(
            ctx.blackboard, "Deterministic repo validation passed for expected files."
        )
    else:
        blocker = _rc._repo_validation_blocker_message(ctx.repo_validation)
        _rc._append_blackboard_note(
            ctx.blackboard,
            f"Deterministic repo validation found issues: {blocker or 'unknown issue'}",
        )
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
