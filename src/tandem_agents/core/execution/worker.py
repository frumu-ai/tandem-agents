from __future__ import annotations

import json
import logging
import re
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
    engine_visible_path,
    git_diff_stat,
    list_engine_permissions,
    prompt_tandem_session_sync,
    reply_engine_permission,
    sync_worktree_changes,
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
    sdk_session_messages,
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
    "ENGINE_EMPTY_RESPONSE",
)


def _print_line(prefix: str, line: str) -> None:
    with PRINT_LOCK:
        print(f"[{prefix}] {line}", end="", flush=True)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(exclude_none=True))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _message_dict(message: Any) -> dict[str, Any] | None:
    if hasattr(message, "model_dump"):
        message = message.model_dump(exclude_none=True)
    if isinstance(message, dict):
        return message
    return None


def _message_role(message: dict[str, Any]) -> str:
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    for value in (
        info.get("role") if isinstance(info, dict) else None,
        message.get("role"),
        message.get("author"),
    ):
        role = str(value or "").strip().lower()
        if role:
            return role
    return ""


def _extract_text_from_message_part(part: Any) -> str:
    if hasattr(part, "model_dump"):
        part = part.model_dump(exclude_none=True)
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        chunks: list[str] = []
        for key in ("text", "content", "value", "delta", "message"):
            value = part.get(key)
            if isinstance(value, str):
                chunks.append(value)
            elif isinstance(value, (dict, list)):
                chunks.append(_extract_text_from_message_part(value))
        return "".join(chunk for chunk in chunks if chunk)
    if isinstance(part, list):
        return "".join(_extract_text_from_message_part(item) for item in part)
    return ""


def _extract_session_reply(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    assistant_parts: list[Any] = []
    for message in reversed(messages):
        message_dict = _message_dict(message)
        if not message_dict:
            continue
        if _message_role(message_dict) != "assistant":
            continue
        parts = message_dict.get("parts") or message_dict.get("content") or []
        if isinstance(parts, list):
            assistant_parts = list(parts)
            break
        text = _extract_text_from_message_part(parts)
        if text:
            return text.strip()
    if not assistant_parts:
        for message in reversed(messages):
            message_dict = _message_dict(message)
            if not message_dict:
                continue
            text = _extract_text_from_message_part(message_dict)
            if text:
                return text.strip()
            break
    text_chunks: list[str] = []
    for part in assistant_parts:
        text = _extract_text_from_message_part(part)
        if text:
            text_chunks.append(text)
    return "\n".join(chunk.rstrip("\n") for chunk in text_chunks if chunk).strip()


def _event_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        chunks: list[str] = []
        for key in ("delta", "text", "content", "message"):
            if key in value:
                chunks.append(_event_value_text(value.get(key)))
        return "".join(chunk for chunk in chunks if chunk)
    if isinstance(value, list):
        return "".join(_event_value_text(item) for item in value)
    return ""


def _extract_run_event_text(events: Any) -> str:
    if not isinstance(events, list):
        return ""
    chunks: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        properties = event.get("properties") or event.get("payload") or {}
        if not isinstance(properties, dict):
            properties = {}
        event_type = str(event.get("type") or properties.get("type") or "").strip()
        if event_type in {"run.complete", "run.completed", "session.run.finished"}:
            continue
        text = _event_value_text(properties)
        if text:
            chunks.append(text)
    return "".join(chunks).strip()


def _write_engine_snapshot(log_path: Path, label: str, payload: Any) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "snapshot"
    snapshot_path = log_path.with_name(f"{log_path.stem}.{safe_label}.json")
    snapshot_path.write_text(
        json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(snapshot_path)


def _exception_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _recover_engine_text_from_state(
    cfg: ResolvedConfig,
    *,
    session_id: str | None,
    run_id: str | None,
    log_path: Path,
) -> tuple[str, dict[str, Any]]:
    recovery: dict[str, Any] = {"errors": []}
    text = ""
    if run_id:
        try:
            events = sdk_run_events(cfg, run_id, tail=500)
            recovery["events_path"] = _write_engine_snapshot(log_path, f"engine-events-{run_id}", events)
            text = _extract_run_event_text(events)
        except Exception as exc:
            if _exception_status_code(exc) == 404:
                recovery.setdefault("notes", []).append("run_events: engine run events were unavailable for this completed run")
            else:
                recovery.setdefault("errors", []).append(f"run_events: {exc}")
            logger.debug("Failed to recover text from run events", exc_info=True)
    if session_id:
        try:
            messages = sdk_session_messages(cfg, session_id)
            recovery["messages_path"] = _write_engine_snapshot(log_path, f"engine-messages-{session_id}", messages)
            if not text.strip():
                text = _extract_session_reply(messages)
        except Exception as exc:
            recovery.setdefault("errors", []).append(f"session_messages: {exc}")
            logger.debug("Failed to recover text from session messages", exc_info=True)
    return text.strip(), recovery


def _extract_prompt_sync_text(response: Any) -> str:
    if hasattr(response, "model_dump"):
        response = response.model_dump(exclude_none=True)
    if isinstance(response, dict):
        for key in ("messages", "message", "response", "output", "stdout", "text", "content"):
            value = response.get(key)
            if key == "messages":
                text = _extract_session_reply(value)
            else:
                text = _extract_text_from_message_part(value)
            if text.strip():
                return text.strip()
        return _extract_text_from_message_part(response).strip()
    if isinstance(response, list):
        text = _extract_session_reply(response) or _extract_text_from_message_part(response)
        return text.strip()
    return _extract_text_from_message_part(response).strip()


def _empty_transcript_retry_prompt() -> str:
    return (
        "The previous async engine run completed without a visible assistant transcript. "
        "Retry the same task now using the context already in this session. "
        "Use tools if they are required. Finish with a concise textual summary. "
        "If blocked, state the exact blocker and the next operator action."
    )


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


def _materialize_worker_context(worktree: Path, subtask: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(subtask)
    pr_context = prepared.get("pr_candidate_context")
    source_artifact = str(prepared.get("pr_candidate_context_artifact") or "").strip()
    if not pr_context and not source_artifact:
        return prepared

    context_dir = worktree / ".aca"
    context_dir.mkdir(parents=True, exist_ok=True)
    destination = context_dir / "pr_candidate_context.json"
    try:
        if source_artifact and Path(source_artifact).exists():
            shutil.copyfile(source_artifact, destination)
        else:
            destination.write_text(
                json.dumps(
                    {"pull_requests": pr_context or [], "source_artifact": source_artifact},
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
        prepared["pr_candidate_context_artifact"] = ".aca/pr_candidate_context.json"
        prepared["pr_candidate_context_artifact_source"] = source_artifact
    except Exception as exc:
        prepared["pr_candidate_context_artifact_error"] = str(exc)
    return prepared


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


def _diff_touches_target_files(worktree: Path, subtask: dict[str, Any]) -> bool:
    targets = set(_subtask_targets(subtask))
    if not targets:
        return False
    result = run_command(["git", "-C", str(worktree), "diff", "--name-only"])
    if result.returncode != 0:
        return False
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return bool(changed.intersection(targets))


def _worktree_preflight(cfg: ResolvedConfig, worktree: Path) -> tuple[bool, str]:
    if not worktree.exists():
        return False, f"worktree path does not exist: {worktree}"
    if not (worktree / ".git").exists():
        return False, f"worktree git metadata missing: {worktree / '.git'}"
    git_result = run_command(["git", "-C", str(worktree), "rev-parse", "--git-dir"])
    if git_result.returncode != 0:
        return False, git_result.stderr.strip() or git_result.stdout.strip() or "worktree git preflight failed"
    engine_worktree = engine_visible_path(worktree)
    if engine_worktree != worktree:
        return True, "ok"
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
    has_pr_context = bool(subtask.get("pr_candidate_context") or subtask.get("pr_candidate_refs"))
    steps = [
        "Retry this subtask and use tools immediately.",
        "Use lightweight repository tools first; if shell sandboxing is unavailable, use read/glob/apply_patch instead of stopping.",
    ]
    if has_pr_context:
        refs = [
            f"refs/aca/pr-{ref.get('number')}"
            for ref in (subtask.get("pr_candidate_refs") or [])
            if isinstance(ref, dict) and ref.get("ok") and ref.get("number")
        ]
        steps.extend(
            [
                "Read `.aca/pr_candidate_context.json` and inspect the fetched PR candidate refs.",
                "Do not produce only an applicability matrix; apply still-relevant code changes into the worktree.",
            ]
        )
        if refs:
            steps.append(
                "Available candidate refs: " + ", ".join(f"`{ref}`" for ref in refs) + "."
            )
    if targets:
        if has_pr_context:
            steps.append(
                "Restrict edits to existing relevant candidate files unless a candidate patch requires a nearby supporting change: "
                + ", ".join(f"`{path}`" for path in targets)
            )
        else:
            steps.append(
                "Create any missing parent directories for the target files before writing them: "
                + ", ".join(f"`{path}`" for path in targets)
            )
    steps.extend(
        [
            "Then create or edit the target files using `write`, `edit`, or `apply_patch`.",
            "Finish by verifying the changed files with `ls -la`, `read`, or `grep`.",
            "Do not reply with a summary until you have either produced a filesystem diff or produced a structured no-safe-changes blocker naming every inspected PR.",
        ]
    )
    return "\n\nRetry instructions:\n- " + "\n- ".join(steps) + "\n"


def _coerce_worker_failure(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    subtask: dict[str, Any],
    *,
    require_filesystem_changes: bool = False,
) -> dict[str, Any]:
    stdout_text = str(result.get("stdout") or "")
    targets_exist = _target_files_exist(worktree, subtask)
    readable_targets = _readable_target_files(worktree, subtask)
    diff_text = git_diff_stat(worktree).strip()
    diff_touches_targets = _diff_touches_target_files(worktree, subtask)
    if result.get("returncode", 0) != 0 and (not targets_exist or not diff_text):
        recovered, recovered_diff = _wait_for_target_files(worktree, subtask)
        if recovered:
            targets_exist = True
            diff_text = recovered_diff
            readable_targets = _readable_target_files(worktree, subtask)
            diff_touches_targets = _diff_touches_target_files(worktree, subtask)
    if result.get("returncode", 0) != 0 and diff_text and (targets_exist or diff_touches_targets):
        message = (
            "Worker returned a nonzero status, but filesystem changes were detected in declared target files. "
            "Treating as success.\n"
        )
        log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
        result["stdout"] = f"{stdout_text}{message}"
        result["returncode"] = 0
        result["recovered_success"] = True
        return result
    if (
        result.get("returncode", 0) != 0
        and readable_targets
        and not diff_text
        and not require_filesystem_changes
    ):
        message = "Worker returned a nonzero status, but all target files were readable in the worktree. Treating as verification success.\n"
        log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
        result["stdout"] = f"{stdout_text}{message}"
        result["returncode"] = 0
        result["verified_existing"] = True
        return result
    failure_reason = str(result.get("failure_reason") or "").strip()
    for marker in WORKER_FAILURE_MARKERS:
        if not failure_reason and marker in stdout_text:
            if marker == "ENGINE_ERROR:":
                failure_reason = next(
                    (line.strip() for line in stdout_text.splitlines() if marker in line),
                    marker,
                )
            else:
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
    if failure_reason == "ENGINE_EMPTY_RESPONSE":
        result["blocker_kind"] = "engine_empty_response"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Check Tandem engine provider/model routing and persisted engine snapshots, then retry the task."
            )
    elif failure_reason == "NO_FILESYSTEM_CHANGES":
        result["blocker_kind"] = "worker_no_diff"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Inspect the worker log and PR candidate context; reset the task to Backlog if another attempt is needed."
            )
    elif failure_reason == "ENGINE_EXCEPTION":
        result["blocker_kind"] = "engine_exception"
    elif failure_reason.startswith("ENGINE_ERROR:"):
        lower_reason = failure_reason.lower()
        if "api key" in lower_reason or "authorization" in lower_reason:
            result["blocker_kind"] = "engine_provider_auth"
            if not result.get("recovery_action"):
                result["recovery_action"] = (
                    "Repair the Tandem Control Panel provider credentials/model route, then reset the task to Backlog."
                )
        else:
            result["blocker_kind"] = "engine_dispatch_failed"
            if not result.get("recovery_action"):
                result["recovery_action"] = (
                    "Inspect Tandem engine dispatch logs and provider routing, then reset the task to Backlog."
                )
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
        last_run_id = ""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== {role} @ {now_ms()} ===\n")
            log.write(prompt.strip() + "\n\n")
            try:
                create_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        session_temperature = None
                        if hasattr(cfg, "sampling_for_role"):
                            session_temperature = cfg.sampling_for_role(role).get("temperature")
                        session_id = create_tandem_session(
                            cfg,
                            title=f"ACA {role}",
                            directory=cwd,
                            provider=provider,
                            model=model,
                            temperature=session_temperature,
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

                engine_meta: dict[str, Any] = {
                    "session_id": session_id,
                    "run_id": "",
                    "retry_count": 0,
                    "fallback_mode": None,
                    "recovery": [],
                }

                def _writer(delta: str) -> None:
                    for line in delta.splitlines(keepends=True):
                        log.write(line)
                        log.flush()
                        _print_line(role, line)

                def _run_async_once(prompt_text: str, attempt: int) -> tuple[str, bool, str]:
                    async_result = sdk_sessions_prompt_async(
                        cfg,
                        session_id=session_id,
                        prompt=prompt_text,
                        tool_mode="required" if require_tool_use else "auto",
                        tool_allowlist=SESSION_TOOL_ALLOWLIST,
                        context_mode=None,
                        write_required=write_required,
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
                                logger.debug("Failed to extract run_id from object attr %s", attr, exc_info=True)
                                continue
                    engine_meta["run_id"] = run_id
                    engine_meta["retry_count"] = attempt
                    nonlocal last_run_id
                    last_run_id = run_id
                    stream_result = (
                        sdk_stream_run_text(cfg, session_id, run_id, _writer, timeout_seconds=600.0)
                        if run_id
                        else {"text": "", "completed": False}
                    )
                    streamed_text = str(stream_result.get("text") or "")
                    completed = bool(stream_result.get("completed"))
                    if completed and streamed_text.strip():
                        return streamed_text, True, run_id
                    recovered_text = ""
                    if completed:
                        recovered_text, recovery = _recover_engine_text_from_state(
                            cfg,
                            session_id=session_id,
                            run_id=run_id,
                            log_path=log_path,
                        )
                        recovery["run_id"] = run_id
                        recovery["attempt"] = attempt
                        engine_meta.setdefault("recovery", []).append(recovery)
                        for key in ("events_path", "messages_path"):
                            if recovery.get(key):
                                engine_meta[key] = recovery[key]
                    if completed and recovered_text.strip():
                        return recovered_text, True, run_id
                    return streamed_text, completed and bool(streamed_text.strip()), run_id

                stdout_text, completed, run_id = _run_async_once(prompt, 0)
                if not completed:
                    retry_notice = (
                        f"ENGINE_EMPTY_RESPONSE_RETRY: engine run {run_id or 'unknown'} completed "
                        "without transcript text; retrying once in the same session.\n"
                    )
                    log.write(retry_notice)
                    log.flush()
                    _print_line(role, retry_notice)
                    retry_text, retry_completed, retry_run_id = _run_async_once(_empty_transcript_retry_prompt(), 1)
                    if retry_completed:
                        stdout_text = retry_text
                        completed = True
                        run_id = retry_run_id
                    else:
                        stdout_text = retry_text or stdout_text
                        run_id = retry_run_id or run_id

                if not completed:
                    engine_meta["fallback_mode"] = "prompt_sync"
                    fallback_notice = (
                        f"ENGINE_EMPTY_RESPONSE_FALLBACK: engine run {run_id or 'unknown'} still had "
                        "no transcript text; using prompt_sync fallback.\n"
                    )
                    log.write(fallback_notice)
                    log.flush()
                    _print_line(role, fallback_notice)
                    sync_response = prompt_tandem_session_sync(
                        cfg,
                        session_id=session_id,
                        prompt=_empty_transcript_retry_prompt(),
                        tool_allowlist=SESSION_TOOL_ALLOWLIST,
                        require_tool_use=require_tool_use,
                        write_required=write_required,
                    )
                    engine_meta["sync_snapshot_path"] = _write_engine_snapshot(
                        log_path,
                        f"engine-sync-{session_id}",
                        sync_response,
                    )
                    sync_text = _extract_prompt_sync_text(sync_response)
                    if sync_text.strip():
                        stdout_text = sync_text
                        completed = True

                failure_reason = ""
                blocker_kind = ""
                if completed:
                    if stdout_text and not stdout_text.endswith("\n"):
                        stdout_text += "\n"
                else:
                    failure_reason = "ENGINE_EMPTY_RESPONSE"
                    blocker_kind = "engine_empty_response"
                    stdout_text = (
                        "ENGINE_EMPTY_RESPONSE: Tandem engine completed async prompt, same-session "
                        "retry, and prompt_sync fallback without transcript text. "
                        f"Last engine run: {run_id or last_run_id or 'unknown'}.\n"
                    )
                if stdout_text:
                    log.write(stdout_text)
                    log.flush()
                    _print_line(role, stdout_text)
                return {
                    "role": role,
                    "returncode": 0 if completed else 1,
                    "stdout": stdout_text,
                    "log_path": str(log_path),
                    "cwd": str(cwd),
                    "session_id": session_id,
                    "engine_run_id": run_id or last_run_id,
                    "engine": engine_meta,
                    "failure_reason": failure_reason,
                    "blocker_kind": blocker_kind,
                    "recovery_action": (
                        "Check Tandem engine provider/model routing and persisted engine snapshots, "
                        "then retry the task after the engine returns assistant text."
                    )
                    if blocker_kind
                    else "",
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
                    "engine_run_id": last_run_id,
                    "engine": {
                        "session_id": session_id,
                        "run_id": last_run_id,
                        "retry_count": 0,
                        "fallback_mode": None,
                    },
                    "failure_reason": "ENGINE_EXCEPTION",
                    "blocker_kind": "engine_exception",
                    "recovery_action": "Inspect the worker log and Tandem engine health, then retry the task.",
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
    diff_stat = git_diff_stat(worktree).strip()
    changed_files = [
        line.split("|", 1)[0].strip()
        for line in diff_stat.splitlines()
        if "|" in line and " file changed" not in line and " files changed" not in line
    ]
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
        "failure_reason": str(result.get("failure_reason") or ""),
        "blocker_kind": str(result.get("blocker_kind") or ""),
        "recovery_action": str(result.get("recovery_action") or ""),
        "engine": dict(result.get("engine") or {}),
        "write_required": bool(result.get("write_required", True)),
        "verified_existing": bool(result.get("verified_existing")),
        "changed_files": [path for path in changed_files if path],
        "diff_stat": diff_stat,
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
    subtask = _materialize_worker_context(worktree, subtask)
    
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
    
    task_source = task.get("source") if isinstance(task, dict) else {}
    require_filesystem_changes = (
        isinstance(task_source, dict)
        and str(task_source.get("type") or "").strip() == "github_project"
    )

    worktree_satisfied = bool(subtask.get("pre_satisfied")) and not require_filesystem_changes
    if not worktree_satisfied and not require_filesystem_changes:
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
    
    result = _coerce_worker_failure(
        result,
        log_path,
        worktree,
        subtask,
        require_filesystem_changes=require_filesystem_changes,
    )
    
    if result["returncode"] != 0 and result.get("blocker_kind") != "engine_empty_response":
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
        
        retry_result = _coerce_worker_failure(
            retry_result,
            log_path,
            worktree,
            subtask,
            require_filesystem_changes=require_filesystem_changes,
        )
        if retry_result["returncode"] == 0:
            result = retry_result
            
    append_event(
        layout["events"],
        "worker.completed" if result["returncode"] == 0 else "worker.failed",
        run_id,
        {
            "worker_id": worker_id,
            "subtask_id": subtask["id"],
            "returncode": result["returncode"],
            "failure_reason": result.get("failure_reason"),
            "blocker_kind": result.get("blocker_kind"),
            "recovery_action": result.get("recovery_action"),
            "engine": result.get("engine"),
        },
        task_id=task.get("task_id"),
        role="worker",
        repo={"path": str(repo_path)},
    )
    
    # Finalize by syncing worktree changes back to the main repo path if successful
    if result["returncode"] == 0:
        sync_worktree_changes(worktree, repo_path)
        
    return summarize_worker_notes(result, worker_id, subtask, worktree, index)
