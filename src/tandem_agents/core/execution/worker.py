from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("aca.worker")

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import (
    execute_engine_tool,
    create_tandem_session,
    create_worktree,
    delete_tandem_session,
    effective_tandem_provider,
    engine_env,
    git_diff_stat,
    list_engine_permissions,
    prompt_tandem_session_sync,
    reply_engine_permission,
    worker_worktree_name,
    write_provider_override_config,
)
from src.tandem_agents.core.engine.prompts import build_worker_prompt
from src.tandem_agents.core.repository.repo_truth import file_is_readable, shell_quote_path
from src.tandem_agents.core.engine.process_utils import run_command
from src.tandem_agents.runtime.artifact_store import mirror_run_tree
from src.tandem_agents.runtime.runstate import append_event, ensure_layout
from src.tandem_agents.utils.utils import now_ms
from src.tandem_agents.core.engine.tandem_client_sdk import (
    sdk_sessions_prompt_async,
    sdk_run_events,
    sdk_stream_run_text,
)

PRINT_LOCK = threading.Lock()
SESSION_TOOL_ALLOWLIST = [
    "read",
    "glob",
    "grep",
    "bash",
    "write",
    "edit",
    "apply_patch",
    "browser_open",
    "browser_type",
    "browser_click",
    "browser_screenshot",
    "browser_content",
]
WORKER_FAILURE_MARKERS = (
    "ENGINE_ERROR:",
    "TOOL_MODE_REQUIRED_NOT_SATISFIED",
    "WRITE_REQUIRED_NOT_SATISFIED",
)


def _print_line(prefix: str, line: str) -> None:
    with PRINT_LOCK:
        print(f"[{prefix}] {line}", end="", flush=True)


def _extract_session_reply(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    assistant_parts: list[dict[str, Any]] = []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        info = message.get("info") or {}
        if str(info.get("role") or "").strip().lower() != "assistant":
            continue
        parts = message.get("parts") or []
        if isinstance(parts, list):
            assistant_parts = [part for part in parts if isinstance(part, dict)]
            break
    text_chunks: list[str] = []
    for part in assistant_parts:
        text = part.get("text")
        if isinstance(text, str) and text:
            text_chunks.append(text)
    return "\n".join(chunk.rstrip("\n") for chunk in text_chunks if chunk).strip()


def _subtask_targets(subtask: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for key in ("files", "target_files"):
        for raw_path in subtask.get(key) or []:
            rel_path = str(raw_path or "").strip()
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)
            targets.append(rel_path)
    return targets


def _prepare_worktree_targets(worktree: Path, subtask: dict[str, Any]) -> None:
    for raw_path in _subtask_targets(subtask):
        rel_path = str(raw_path or "").strip()
        if not rel_path:
            continue
        parent = (worktree / rel_path).parent
        parent.mkdir(parents=True, exist_ok=True)


def _target_files_exist(worktree: Path, subtask: dict[str, Any]) -> bool:
    targets = _subtask_targets(subtask)
    if not targets:
        return False
    return all((worktree / rel_path).exists() for rel_path in targets)


def _readable_target_files(worktree: Path, subtask: dict[str, Any]) -> list[str]:
    readable: list[str] = []
    for rel_path in _subtask_targets(subtask):
        if not rel_path:
            continue
        target = worktree / rel_path
        if target.exists() and target.is_file() and file_is_readable(target):
            readable.append(rel_path)
    return readable


def _wait_for_target_files(worktree: Path, subtask: dict[str, Any], timeout_seconds: float = 3.0) -> tuple[bool, str]:
    deadline = time.time() + max(0.0, timeout_seconds)
    last_diff = ""
    while True:
        last_diff = git_diff_stat(worktree).strip()
        if _target_files_exist(worktree, subtask) and last_diff:
            return True, last_diff
        if time.time() >= deadline:
            return False, last_diff
        time.sleep(0.25)


def _worktree_preflight(cfg: ResolvedConfig, worktree: Path) -> tuple[bool, str]:
    if not worktree.exists():
        return False, f"worktree path does not exist: {worktree}"
    if not (worktree / ".git").exists():
        return False, f"worktree git metadata missing: {worktree / '.git'}"
    git_result = run_command(["git", "-C", str(worktree), "rev-parse", "--git-dir"])
    if git_result.returncode != 0:
        return False, git_result.stderr.strip() or git_result.stdout.strip() or "worktree git preflight failed"
    try:
        result = execute_engine_tool(
            cfg,
            "bash",
            {
                "command": (
                    f"test -d {shell_quote_path(worktree)} "
                    f"&& test -e {shell_quote_path(worktree / '.git')} "
                    "&& printf ACA_WORKTREE_OK"
                )
            },
        )
        text = str(result)
        if "ACA_WORKTREE_OK" not in text:
            return False, "engine could not confirm worktree visibility"
    except Exception as exc:
        return False, f"engine worktree preflight failed: {exc}"
    return True, "ok"


def _worker_prompt_retry_suffix(subtask: dict[str, Any]) -> str:
    targets = _subtask_targets(subtask)
    steps = [
        "Retry this subtask and use tools immediately.",
        "Start with `pwd` and `ls -la` in the current directory.",
    ]
    if targets:
        steps.append(
            "Create any missing parent directories for the target files with `bash` before writing them: "
            + ", ".join(f"`{path}`" for path in targets)
        )
    steps.extend(
        [
            "Then create or edit the target files using `write`, `edit`, or `apply_patch`.",
            "Finish by verifying the changed files with `ls -la`, `read`, or `grep`.",
            "Do not reply with a summary until you have completed at least one productive tool call.",
        ]
    )
    return "\n\nRetry instructions:\n- " + "\n- ".join(steps) + "\n"


def _coerce_worker_failure(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    subtask: dict[str, Any],
) -> dict[str, Any]:
    stdout_text = str(result.get("stdout") or "")
    targets_exist = _target_files_exist(worktree, subtask)
    readable_targets = _readable_target_files(worktree, subtask)
    diff_text = git_diff_stat(worktree).strip()
    if result.get("returncode", 0) != 0 and (not targets_exist or not diff_text):
        recovered, recovered_diff = _wait_for_target_files(worktree, subtask)
        if recovered:
            targets_exist = True
            diff_text = recovered_diff
            readable_targets = _readable_target_files(worktree, subtask)
    if result.get("returncode", 0) != 0 and diff_text and targets_exist:
        message = "Worker returned a nonzero status, but target files exist and filesystem changes were detected. Treating as success.\n"
        log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
        result["stdout"] = f"{stdout_text}{message}"
        result["returncode"] = 0
        result["recovered_success"] = True
        return result
    if result.get("returncode", 0) != 0 and readable_targets and not diff_text:
        message = "Worker returned a nonzero status, but all target files were readable in the worktree. Treating as verification success.\n"
        log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
        result["stdout"] = f"{stdout_text}{message}"
        result["returncode"] = 0
        result["verified_existing"] = True
        return result
    failure_reason = ""
    for marker in WORKER_FAILURE_MARKERS:
        if marker in stdout_text:
            failure_reason = marker
            break
    if result.get("returncode", 0) == 0 and not diff_text:
        failure_reason = failure_reason or "NO_FILESYSTEM_CHANGES"
    if not failure_reason:
        return result
    message = ""
    if failure_reason == "NO_FILESYSTEM_CHANGES":
        message = "Worker reported success but produced no filesystem changes in its worktree.\n"
    elif failure_reason not in stdout_text:
        message = f"Worker failed: {failure_reason}\n"
    if message:
        log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
        result["stdout"] = f"{stdout_text}{message}"
    result["returncode"] = 1
    result["failure_reason"] = failure_reason
    return result


def stream_tandem_prompt(
    cfg: ResolvedConfig,
    *,
    role: str,
    prompt: str,
    cwd: Path,
    provider: str,
    model: str,
    env: dict[str, str],
    log_path: Path,
    config_path: Path | None = None,
    require_tool_use: bool = False,
    write_required: bool = False,
) -> dict[str, Any]:
    if config_path is None:
        session_id: str | None = None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== {role} @ {now_ms()} ===\n")
            log.write(prompt.strip() + "\n\n")
            try:
                create_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        session_id = create_tandem_session(
                            cfg,
                            title=f"ACA {role}",
                            directory=cwd,
                            provider=provider,
                            model=model,
                        )
                        create_exc = None
                        break
                    except Exception as exc:
                        create_exc = exc
                        if attempt >= 2:
                            raise
                        time.sleep(0.5 * (attempt + 1))
                if session_id is None and create_exc is not None:
                    raise create_exc
                
                async_result = sdk_sessions_prompt_async(
                    cfg,
                    session_id=session_id,
                    prompt=prompt,
                    tool_mode="required" if require_tool_use else "auto",
                    tool_allowlist=SESSION_TOOL_ALLOWLIST,
                    context_mode=None,
                )
                run_id = ""
                try:
                    run_id = str((async_result or {}).get("run_id"))  # type: ignore[attr-defined]
                except Exception:
                    logger.debug("Failed to extract run_id from dict", exc_info=True)
                if not run_id:
                    for attr in ("runID", "runId", "id", "run_id"):
                        try:
                            val = getattr(async_result, attr)
                            if val:
                                run_id = str(val)
                                break
                        except Exception:
                            logger.debug(f"Failed to extract run_id from object attr {attr}", exc_info=True)
                            continue
                def _writer(delta: str) -> None:
                    for line in delta.splitlines(keepends=True):
                        log.write(line)
                        log.flush()
                        _print_line(role, line)
                stream_result = sdk_stream_run_text(cfg, session_id, run_id, _writer, timeout_seconds=600.0) if run_id else {"text": "", "completed": False}
                stdout_text = str(stream_result.get("text") or "")
                completed = bool(stream_result.get("completed"))
                if stdout_text and not stdout_text.endswith("\n"):
                    stdout_text += "\n"
                return {
                    "role": role,
                    "returncode": 0 if completed else 1,
                    "stdout": stdout_text,
                    "log_path": str(log_path),
                    "cwd": str(cwd),
                    "session_id": session_id,
                }
            except Exception as exc:
                message = f"Error: {exc}\n"
                log.write(message)
                log.flush()
                _print_line(role, message)
                return {
                    "role": role,
                    "returncode": 1,
                    "stdout": message,
                    "log_path": str(log_path),
                    "cwd": str(cwd),
                    "session_id": session_id,
                }
            finally:
                if session_id:
                    try:
                        delete_tandem_session(cfg, session_id)
                    except Exception:
                        logger.debug("Failed to delete tandem session", exc_info=True)
    return {"role": role, "returncode": 1, "stdout": "Internal Error: session-less stream requested"}


def summarize_worker_notes(
    result: dict[str, Any],
    worker_id: str,
    subtask: dict[str, Any],
    worktree: Path,
    index: int,
) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "subtask_index": index,
        "subtask_id": subtask["id"],
        "title": subtask["title"],
        "status": (
            "skipped_existing"
            if result.get("skipped_existing")
            else ("completed" if result["returncode"] == 0 else "failed")
        ),
        "returncode": result["returncode"],
        "worktree": str(worktree),
        "log_path": result["log_path"],
        "output_excerpt": result["stdout"][:2000],
        "write_required": bool(result.get("write_required", True)),
        "verified_existing": bool(result.get("verified_existing")),
    }


def sync_worker_artifacts(worktree: Path, run_artifacts_dir: Path, run_id: str, worker_id: str, events_path: Path) -> None:
    """Detects and moves artifacts (like screenshots) from worktree to run artifacts dir."""
    worker_screenshots = worktree / "screenshots"
    if not worker_screenshots.exists():
        return
    
    run_artifacts_dir.mkdir(parents=True, exist_ok=True)
    for item in worker_screenshots.glob("*"):
        if item.is_file():
            # Use a name that includes worker_id to avoid collisions
            dest_name = f"{worker_id}_{item.name}"
            dest_path = run_artifacts_dir / dest_name
            shutil.move(str(item), str(dest_path))
            
            # Emit event for Control Panel to pick up
            append_event(
                events_path,
                "worker.artifact_captured",
                run_id,
                {
                    "worker_id": worker_id,
                    "artifact_type": "screenshot" if item.suffix.lower() in (".png", ".jpg", ".jpeg") else "file",
                    "name": dest_name,
                    "url": f"/runs/{run_id}/artifacts/{dest_name}"
                }
            )
    mirror_run_tree(run_id, run_artifacts_dir, logical_prefix="artifacts")


def run_worker_subtask(
    cfg: ResolvedConfig,
    run_id: str,
    repo_path: Path,
    run_dir: Path,
    task: dict[str, Any],
    subtask: dict[str, Any],
    worker_id: str,
    index: int,
) -> dict[str, Any]:
    layout = ensure_layout(run_dir)
    
    # Create an isolated worktree for this worker/subtask ownership pair.
    worktree_path = layout["worktrees"] / worker_worktree_name(worker_id, subtask.get("id"))
    worktree = create_worktree(repo_path, worktree_path)
    
    _prepare_worktree_targets(worktree, subtask)
    preflight_ok, preflight_detail = _worktree_preflight(cfg, worktree)
    if not preflight_ok:
        _prepare_worktree_targets(worktree, subtask)
        preflight_ok, preflight_detail = _worktree_preflight(cfg, worktree)
    
    worker_provider, worker_model = cfg.provider_for_role("worker")
    worker_cli_provider = effective_tandem_provider(worker_provider, cfg)
    env = engine_env(cfg)
    log_path = layout["logs"] / f"{worker_id}.log"
    config_path = None
    
    worktree_satisfied = bool(subtask.get("pre_satisfied"))
    if not worktree_satisfied:
        worktree_satisfied = _target_files_exist(worktree, subtask) and bool(_readable_target_files(worktree, subtask))
    
    write_required = not worktree_satisfied
    prompt = build_worker_prompt(run_id, worker_id, subtask, task, worktree)
    if not preflight_ok:
        prompt += (
            "\n\nPreflight warning:\n"
            f"- {preflight_detail}\n"
            "- Re-check the current directory with tools before doing any work.\n"
        )
    
    append_event(layout["events"], "worker.started", run_id, {"worker_id": worker_id, "subtask_id": subtask["id"], "worktree": str(worktree)}, task_id=task.get("task_id"), role="worker", repo={"path": str(repo_path)})
    
    result = stream_tandem_prompt(
        cfg,
        role=worker_id,
        prompt=prompt,
        cwd=worktree,
        provider=worker_cli_provider,
        model=worker_model,
        env=env,
        log_path=log_path,
        config_path=config_path,
        require_tool_use=True,
        write_required=write_required,
    )
    result["write_required"] = write_required
    
    # Sync artifacts after turn
    sync_worker_artifacts(worktree, layout["artifacts"], run_id, worker_id, layout["events"])
    
    result = _coerce_worker_failure(result, log_path, worktree, subtask)
    
    if result["returncode"] != 0:
        retry_prompt = prompt + _worker_prompt_retry_suffix(subtask)
        retry_result = stream_tandem_prompt(
            cfg,
            role=worker_id,
            prompt=retry_prompt,
            cwd=worktree,
            provider=worker_cli_provider,
            model=worker_model,
            env=env,
            log_path=log_path,
            config_path=config_path,
            require_tool_use=True,
            write_required=write_required,
        )
        retry_result["write_required"] = write_required
        
        # Sync artifacts after retry turn
        sync_worker_artifacts(worktree, layout["artifacts"], run_id, worker_id, layout["events"])
        
        retry_result = _coerce_worker_failure(retry_result, log_path, worktree, subtask)
        if retry_result["returncode"] == 0:
            result = retry_result
            
    append_event(
        layout["events"],
        "worker.completed" if result["returncode"] == 0 else "worker.failed",
        run_id,
        {"worker_id": worker_id, "subtask_id": subtask["id"], "returncode": result["returncode"]},
        task_id=task.get("task_id"),
        role="worker",
        repo={"path": str(repo_path)},
    )
    
    # Finalize by syncing worktree changes back to the main repo path if successful
    if result["returncode"] == 0:
        sync_worktree_changes(worktree, repo_path)
        
    return summarize_worker_notes(result, worker_id, subtask, worktree, index)
