from __future__ import annotations

import json
import logging
import queue
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
    engine_env,
    engine_session_provider_model,
    engine_visible_path,
    git_command_for_worktree,
    git_diff_stat,
    git_working_diff,
    git_worktree_preflight,
    list_worktree_changes,
    list_engine_permissions,
    prompt_tandem_session_sync,
    reply_engine_permission,
    sync_worktree_changes,
    worker_worktree_name,
    write_provider_override_config,
    engine_health,
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
SESSION_PERMISSION_RULES = [
    {"permission": tool, "pattern": "*", "action": "allow"}
    for tool in SESSION_TOOL_ALLOWLIST
]
WORKER_FAILURE_MARKERS = (
    "ENGINE_ERROR:",
    "TOOL_MODE_REQUIRED_NOT_SATISFIED",
    "WRITE_REQUIRED_NOT_SATISFIED",
    "ENGINE_EMPTY_RESPONSE",
    "ENGINE_PROMPT_TIMEOUT",
    "ENGINE_TOOL_LOOP_STALLED",
    "ENGINE_SESSION_RUN_CONFLICT",
)

TERMINAL_ENGINE_STREAM_REASONS = {"timeout", "no_text_timeout", "max_events_without_text"}
NON_RETRYABLE_WORKER_BLOCKERS = {
    "coordination_lost",
    "engine_empty_response",
    "engine_prompt_timeout",
    "engine_session_run_conflict",
    "engine_provider_auth",
    "engine_tool_loop_stalled",
}
PR_CANDIDATE_SEED_CODE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".py")
PR_CANDIDATE_IMPORT_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")
PR_CANDIDATE_SEED_EXCLUDED_PREFIXES = (".jules/", "jules/")
PR_CANDIDATE_SEED_EXCLUDED_FILES = {".jules/bolt.md", "jules/bolt.md"}


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


def _engine_session_run_conflict(exc: Exception) -> tuple[str, int] | None:
    text = str(exc)
    if "SESSION_RUN_CONFLICT" not in text:
        return None
    run_match = re.search(r'"(?:runID|runId|run_id)"\s*:\s*"([^"]+)"', text)
    retry_match = re.search(r'"retryAfterMs"\s*:\s*(\d+)', text)
    run_id = run_match.group(1) if run_match else ""
    retry_after_ms = int(retry_match.group(1)) if retry_match else 500
    return run_id, retry_after_ms


def _engine_sync_conflict_wait_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_SYNC_CONFLICT_WAIT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_SYNC_CONFLICT_WAIT_SECONDS=%s", raw)
    return 12.0


def _engine_prompt_sync_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_PROMPT_SYNC_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(5.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_PROMPT_SYNC_TIMEOUT_SECONDS=%s", raw)
    return 240.0


def _engine_prompt_sync_connect_retries(cfg: ResolvedConfig) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_PROMPT_SYNC_CONNECT_RETRIES", "") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_PROMPT_SYNC_CONNECT_RETRIES=%s", raw)
    return 2


def _engine_prompt_sync_connect_retry_delay_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_PROMPT_SYNC_CONNECT_RETRY_DELAY_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(0.1, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_PROMPT_SYNC_CONNECT_RETRY_DELAY_SECONDS=%s", raw)
    return 1.0


def _engine_exception_is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _engine_exception_is_connection_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "could not connect",
            "connection refused",
            "connection reset",
            "connection aborted",
            "failed to establish",
            "temporarily unavailable",
            "name or service not known",
        )
    )


def _prompt_sync_with_connect_retries(
    cfg: ResolvedConfig,
    *,
    engine_meta: dict[str, Any],
    log: Any,
    role: str,
    **kwargs: Any,
) -> Any:
    attempts = _engine_prompt_sync_connect_retries(cfg) + 1
    delay_seconds = _engine_prompt_sync_connect_retry_delay_seconds(cfg)
    last_exc: Exception | None = None
    for attempt_index in range(attempts):
        try:
            return prompt_tandem_session_sync(cfg, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _engine_exception_is_connection_failure(exc) or attempt_index >= attempts - 1:
                raise
            retry_number = attempt_index + 1
            engine_meta["retry_count"] = max(int(engine_meta.get("retry_count") or 0), retry_number)
            recovery: dict[str, Any] = {
                "attempt": retry_number,
                "stream_reason": "prompt_sync_connect_retry",
                "error": str(exc),
            }
            try:
                recovery["health"] = engine_health(cfg, timeout=2.0)
            except Exception as health_exc:
                recovery["health_error"] = str(health_exc)
            engine_meta.setdefault("recovery", []).append(recovery)
            notice = (
                "ENGINE_PROMPT_SYNC_CONNECT_RETRY: Tandem engine prompt_sync was temporarily "
                f"unreachable; retry {retry_number}/{attempts - 1}.\n"
            )
            log.write(notice)
            log.flush()
            _print_line(role, notice)
            time.sleep(delay_seconds * retry_number)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("prompt_sync retry loop exited without an attempt")


def _call_with_timeout(fn: Any, *, timeout_seconds: float) -> Any:
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put(("ok", fn()))
        except Exception as exc:
            result_queue.put(("err", exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(max(0.1, timeout_seconds))
    if thread.is_alive():
        raise TimeoutError(f"operation did not finish within {timeout_seconds:.1f}s")
    status, value = result_queue.get_nowait()
    if status == "err":
        raise value
    return value


def _engine_snapshot_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_SNAPSHOT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_SNAPSHOT_TIMEOUT_SECONDS=%s", raw)
    return 8.0


def _recover_engine_text_from_state(
    cfg: ResolvedConfig,
    *,
    session_id: str | None,
    run_id: str | None,
    log_path: Path,
) -> tuple[str, dict[str, Any]]:
    recovery: dict[str, Any] = {"errors": []}
    text = ""
    snapshot_timeout = _engine_snapshot_timeout_seconds(cfg)
    if run_id:
        try:
            events = _call_with_timeout(lambda: sdk_run_events(cfg, run_id, tail=500), timeout_seconds=snapshot_timeout)
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
            messages = _call_with_timeout(lambda: sdk_session_messages(cfg, session_id), timeout_seconds=snapshot_timeout)
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


def _first_balanced_json_object(text: str) -> dict[str, Any] | None:
    start = -1
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start:index + 1])
                except Exception:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _manager_plan_stream_complete(text: str) -> bool:
    plan = _first_balanced_json_object(text)
    if not isinstance(plan, dict):
        return False
    return {"summary", "subtasks", "risks", "tests"}.issubset(set(plan))


def _empty_transcript_retry_prompt(
    *,
    role: str = "",
    require_tool_use: bool = False,
    write_required: bool = False,
) -> str:
    if role == "manager" and not require_tool_use and not write_required:
        return (
            "The previous async manager run completed without a visible assistant transcript. "
            "Retry the planning task using the context already in this session. "
            "Return JSON only with keys: summary, subtasks, risks, tests."
        )
    if require_tool_use or write_required:
        return (
            "The previous async worker run completed without a visible assistant transcript. "
            "Continue the original task using repository tools now. "
            "Do not stop with only a summary. Finish only after you have either produced and verified "
            "filesystem changes, or inspected the target files and reported a concrete blocker with the "
            "exact next operator action."
        )
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
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        return False
    changed = {str(change.get("path") or "").strip() for change in changes}
    return bool(changed.intersection(targets))


def _is_aca_repo_artifact_path(path: str) -> bool:
    rel_path = str(path or "").strip().replace("\\", "/")
    name = Path(rel_path).name
    if not rel_path:
        return True
    if rel_path.startswith(".aca/"):
        return True
    if name.startswith("aca-") and name.endswith(".md"):
        return True
    if name.startswith("ACA_") and name.endswith(".md"):
        return True
    return False


def _worktree_changed_files(worktree: Path) -> list[str]:
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        changes = []
    changed: list[str] = []
    for change in changes:
        path = str(change.get("path") or "").strip()
        if path and not _is_aca_repo_artifact_path(path):
            changed.append(path)
    if changed:
        return changed
    try:
        status_text = git_diff_stat(worktree)
    except Exception:
        return []
    for raw_line in status_text.splitlines():
        if not raw_line.strip():
            continue
        path = raw_line[3:].strip() if len(raw_line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path and not _is_aca_repo_artifact_path(path):
            changed.append(path)
    return changed


def _engine_prompt_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_PROMPT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_PROMPT_TIMEOUT_SECONDS=%s", raw)
    coordination = getattr(cfg, "coordination", None)
    lease_ttl = float(getattr(coordination, "lease_ttl_seconds", 300) or 300)
    heartbeat = float(getattr(coordination, "heartbeat_interval_seconds", 30) or 30)
    return max(60.0, min(240.0, lease_ttl - heartbeat))


def _engine_no_text_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_NO_TEXT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(15.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_NO_TEXT_TIMEOUT_SECONDS=%s", raw)
    return 210.0


def _engine_max_events_without_text(cfg: ResolvedConfig, role: str) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_MAX_EVENTS_WITHOUT_TEXT", "") or "").strip()
    if raw:
        try:
            return max(10, int(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_MAX_EVENTS_WITHOUT_TEXT=%s", raw)
    if role.startswith("worker-"):
        return 150
    return 150


def _preserve_partial_worker_diff(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    *,
    reason: str,
) -> dict[str, Any]:
    stdout_text = str(result.get("stdout") or "")
    normalized_reason = str(result.get("failure_reason") or reason or "").strip()
    if "ENGINE_ERROR:" in stdout_text:
        normalized_reason = next(
            (line.strip() for line in stdout_text.splitlines() if "ENGINE_ERROR:" in line),
            "ENGINE_ERROR:",
        )
    elif "ENGINE_PROMPT_TIMEOUT" in stdout_text:
        normalized_reason = "ENGINE_PROMPT_TIMEOUT"
    elif "ENGINE_TOOL_LOOP_STALLED" in stdout_text:
        normalized_reason = "ENGINE_TOOL_LOOP_STALLED"
    if normalized_reason:
        result["failure_reason"] = normalized_reason
    if not result.get("blocker_kind"):
        if normalized_reason == "ENGINE_PROMPT_TIMEOUT":
            result["blocker_kind"] = "engine_prompt_timeout"
        elif normalized_reason == "ENGINE_TOOL_LOOP_STALLED":
            result["blocker_kind"] = "engine_tool_loop_stalled"
        elif normalized_reason.startswith("ENGINE_ERROR:"):
            result["blocker_kind"] = "engine_dispatch_failed"
    if not result.get("recovery_action"):
        result["recovery_action"] = (
            "Inspect the preserved partial diff and engine/session logs, then retry from a clean checkout."
        )
    diff_text = ""
    status_text = ""
    try:
        diff_text = git_working_diff(worktree)
    except Exception:
        logger.debug("Failed to capture partial worker diff", exc_info=True)
    try:
        status_text = git_diff_stat(worktree)
    except Exception:
        logger.debug("Failed to capture partial worker status", exc_info=True)
    artifacts_dir = log_path.parent.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / f"{log_path.stem}.partial-worker-diff.patch"
    artifact_path.write_text(
        f"# Partial worker diff preserved after nonterminal engine result\n"
        f"# Reason: {reason}\n\n"
        f"## git status --short --untracked-files=all\n\n{status_text}\n"
        f"## git diff --binary\n\n{diff_text}\n",
        encoding="utf-8",
    )
    result.setdefault("artifacts", {})["partial_diff"] = str(artifact_path)
    result["partial_diff_artifact"] = str(artifact_path)
    return result


def _recover_tool_stall_with_diff(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    *,
    reason: str,
) -> dict[str, Any]:
    changed_files = _worktree_changed_files(worktree)
    if not changed_files:
        return _preserve_partial_worker_diff(result, log_path, worktree, reason=reason)
    recovered = _preserve_partial_worker_diff(result, log_path, worktree, reason=reason)
    changed_summary = "\n".join(f"- {path}" for path in changed_files)
    message = (
        "\nACA recovered this worker because the Tandem engine stalled after producing a real diff.\n"
        "The partial diff was preserved and will be sent through normal review/verification gates.\n"
        f"Changed files:\n{changed_summary}\n"
    )
    log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
    recovered["stdout"] = f"{recovered.get('stdout') or ''}{message}"
    recovered["returncode"] = 0
    recovered["recovered_from_engine_stall"] = True
    recovered["changed_files"] = changed_files
    recovered.setdefault("warnings", []).append("engine_tool_loop_stalled_after_diff")
    recovered.pop("failure_reason", None)
    recovered.pop("blocker_kind", None)
    recovered.pop("recovery_action", None)
    return recovered


def _engine_failure_should_not_recover(result: dict[str, Any], stdout_text: str) -> bool:
    reason = str(result.get("failure_reason") or "").upper()
    blocker = str(result.get("blocker_kind") or "").lower()
    text = stdout_text.upper()
    return (
        "ENGINE_PROMPT_TIMEOUT" in reason
        or "ENGINE_TOOL_LOOP_STALLED" in reason
        or "ENGINE_EMPTY_RESPONSE" in reason
        or "ENGINE_EXCEPTION" in reason
        or "ENGINE_SESSION_RUN_CONFLICT" in reason
        or "ENGINE_ERROR:" in text
        or "ENGINE_DISPATCH_FAILED" in text
        or "ITERATION BUDGET" in text
        or blocker
        in {
            "engine_empty_response",
            "engine_exception",
            "engine_prompt_timeout",
            "engine_session_run_conflict",
            "engine_tool_loop_stalled",
        }
        or "ENGINE_PROMPT_TIMEOUT" in text
        or "ENGINE_TOOL_LOOP_STALLED" in text
        or "ENGINE_SESSION_RUN_CONFLICT" in text
    )


def _worker_result_should_retry(result: dict[str, Any]) -> bool:
    if result.get("returncode") == 0:
        return False
    blocker_kind = str(result.get("blocker_kind") or "").strip()
    return blocker_kind not in NON_RETRYABLE_WORKER_BLOCKERS


def _subtask_requires_real_diff(subtask: dict[str, Any]) -> bool:
    return bool(subtask.get("pr_candidate_context") or subtask.get("pr_candidate_refs"))


def _pr_candidate_ref_map(subtask: dict[str, Any]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for ref_entry in subtask.get("pr_candidate_refs") or []:
        if not isinstance(ref_entry, dict) or not ref_entry.get("ok"):
            continue
        number = str(ref_entry.get("number") or "").strip()
        ref = str(ref_entry.get("ref") or f"refs/aca/pr-{number}").strip()
        if number and ref:
            refs[number] = ref
    return refs


def _candidate_seed_file_decision(
    entry: dict[str, Any],
    target_files: set[str],
) -> tuple[str, str | None]:
    path = str(entry.get("filename") or "").strip().lstrip("/")
    if not path:
        return "", "missing_filename"
    if path in PR_CANDIDATE_SEED_EXCLUDED_FILES or path.startswith(PR_CANDIDATE_SEED_EXCLUDED_PREFIXES):
        return path, "excluded_generated_or_private_file"
    if entry.get("current_layout_stale") or not entry.get("base_path_exists"):
        return path, "stale_or_missing_current_layout"
    if target_files and path not in target_files:
        return path, "outside_candidate_target_files"
    if not path.endswith(PR_CANDIDATE_SEED_CODE_EXTENSIONS):
        return path, "unsupported_file_type"
    return path, None


def _seedable_pr_candidate_specs(subtask: dict[str, Any]) -> list[dict[str, Any]]:
    """Return conservative PR candidates ACA can seed before retrying a no-diff worker.

    The seed path is intentionally narrow: it only applies candidate files that
    exist in the current layout, are source/script files, and have fetched refs.
    Stale docs, generated churn, and lockfiles are skipped per file so one stale
    file does not discard an otherwise safe current-layout candidate.
    """

    ref_by_number = _pr_candidate_ref_map(subtask)
    if not ref_by_number:
        return []
    target_files = set(_subtask_targets(subtask))
    specs: list[dict[str, Any]] = []
    for context in subtask.get("pr_candidate_context") or []:
        if not isinstance(context, dict) or context.get("error"):
            continue
        number = str(context.get("number") or "").strip()
        ref = ref_by_number.get(number)
        if not number or not ref:
            continue
        file_entries = context.get("files")
        if not isinstance(file_entries, list) or not file_entries:
            continue
        files: list[str] = []
        skipped: list[dict[str, str]] = []
        for entry in file_entries:
            if not isinstance(entry, dict):
                skipped.append({"path": "", "reason": "malformed_file_entry"})
                break
            path, reason = _candidate_seed_file_decision(entry, target_files)
            if reason:
                skipped.append({"path": path, "reason": reason})
                continue
            files.append(path)
        if not files:
            continue
        specs.append({"number": number, "ref": ref, "files": files, "skipped_files": skipped})
    return specs


def _unseedable_pr_candidate_summaries(
    subtask: dict[str, Any],
    seedable_specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seedable_numbers = {str(spec.get("number") or "").strip() for spec in seedable_specs}
    ref_by_number = _pr_candidate_ref_map(subtask)
    target_files = set(_subtask_targets(subtask))
    summaries: list[dict[str, Any]] = []
    for context in subtask.get("pr_candidate_context") or []:
        if not isinstance(context, dict):
            continue
        number = str(context.get("number") or "").strip()
        if not number or number in seedable_numbers:
            continue
        ref = ref_by_number.get(number)
        reasons: list[str] = []
        if context.get("error"):
            reasons.append(str(context.get("error")))
        if not ref:
            reasons.append("candidate ref unavailable")
        file_entries = context.get("files")
        if not isinstance(file_entries, list) or not file_entries:
            reasons.append("no file entries in candidate context")
        else:
            for entry in file_entries:
                if not isinstance(entry, dict):
                    reasons.append("malformed file entry")
                    continue
                path, reason = _candidate_seed_file_decision(entry, target_files)
                if reason:
                    reasons.append(f"{path or '<unknown>'}: {reason}")
        summaries.append(
            {
                "number": number,
                "ref": ref,
                "reason": "no seedable current-layout source files"
                + (f": {'; '.join(reasons)}" if reasons else ""),
            }
        )
    return summaries


def _resolve_relative_import(worktree: Path, importer: str, import_path: str) -> Path | None:
    if not import_path.startswith(("./", "../")):
        return None
    base = (worktree / importer).parent / import_path
    for extension in PR_CANDIDATE_IMPORT_EXTENSIONS:
        candidate = Path(str(base) + extension)
        if candidate.is_file():
            return candidate
    for extension in PR_CANDIDATE_IMPORT_EXTENSIONS[1:]:
        candidate = base / f"index{extension}"
        if candidate.is_file():
            return candidate
    return None


def _missing_relative_imports(worktree: Path, files: list[str]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    import_pattern = re.compile(r"(?:import|export)\s+(?:[^\"']+?\s+from\s+)?[\"'](\.{1,2}/[^\"']+)[\"']")
    for file_path in files:
        if not file_path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
            continue
        target = worktree / file_path
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in import_pattern.finditer(text):
            import_path = match.group(1)
            if _resolve_relative_import(worktree, file_path, import_path) is None:
                missing.append({"file": file_path, "import": import_path})
    return missing


def _reverse_apply_candidate_diff(worktree: Path, diff_text: str) -> subprocess.CompletedProcess[str]:
    reverse_cmd = [
        "--work-tree=." if arg.startswith("--work-tree=") else arg
        for arg in git_command_for_worktree(worktree, "apply", "-R", "--3way", "--whitespace=nowarn")
    ]
    return subprocess.run(
        reverse_cmd,
        cwd=str(worktree),
        input=diff_text,
        capture_output=True,
        text=True,
        check=False,
    )


def _seed_pr_candidate_diff(
    worktree: Path,
    subtask: dict[str, Any],
    log_path: Path,
) -> dict[str, Any] | None:
    def append_log(message: str) -> None:
        previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        log_path.write_text(previous + message, encoding="utf-8")

    specs = _seedable_pr_candidate_specs(subtask)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = _unseedable_pr_candidate_summaries(subtask, specs)
    for spec in specs:
        files = [str(path) for path in spec.get("files") or [] if str(path).strip()]
        ref = str(spec.get("ref") or "").strip()
        number = str(spec.get("number") or "").strip()
        if not files or not ref:
            continue
        diff_result = run_command(
            git_command_for_worktree(worktree, "diff", "--binary", f"HEAD..{ref}", "--", *files)
        )
        if diff_result.returncode != 0 or not diff_result.stdout.strip():
            append_log(
                f"ACA PR candidate seed skipped for PR #{number}: {diff_result.stderr or 'empty diff'}\n"
            )
            skipped.append({
                "number": number,
                "ref": ref,
                "files": files,
                "reason": diff_result.stderr or "empty diff",
            })
            continue
        apply_cmd = [
            "--work-tree=." if arg.startswith("--work-tree=") else arg
            for arg in git_command_for_worktree(worktree, "apply", "--3way", "--whitespace=nowarn")
        ]
        apply_proc = subprocess.run(
            apply_cmd,
            cwd=str(worktree),
            input=diff_result.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
        if apply_proc.returncode != 0:
            append_log(f"ACA PR candidate seed failed for PR #{number}: {apply_proc.stderr or apply_proc.stdout}\n")
            skipped.append({
                "number": number,
                "ref": ref,
                "files": files,
                "reason": apply_proc.stderr or apply_proc.stdout or "apply failed",
            })
            continue
        missing_imports = _missing_relative_imports(worktree, files)
        if missing_imports:
            reverse_proc = _reverse_apply_candidate_diff(worktree, diff_result.stdout)
            detail = ", ".join(
                f"{item.get('file')} imports {item.get('import')}" for item in missing_imports
            )
            reason = f"introduced missing relative import: {detail}"
            dirty_after_reverse = any(path in set(_worktree_changed_files(worktree)) for path in files)
            if reverse_proc.returncode != 0 or dirty_after_reverse:
                checkout_proc = run_command(git_command_for_worktree(worktree, "checkout", "--", *files))
                if checkout_proc.returncode != 0:
                    reason += f"; rollback failed: {reverse_proc.stderr or reverse_proc.stdout or checkout_proc.stderr or checkout_proc.stdout}"
            append_log(f"ACA PR candidate seed rejected for PR #{number}: {reason}\n")
            skipped.append({
                "number": number,
                "ref": ref,
                "files": files,
                "reason": reason,
            })
            continue
        message = (
            f"ACA seeded PR candidate #{number} into the worker worktree before retry "
            f"because the first worker attempt produced no diff. Files: {', '.join(files)}\n"
        )
        append_log(message)
        applied.append({
            "number": number,
            "ref": ref,
            "files": files,
            "skipped_files": list(spec.get("skipped_files") or []),
        })
    changed_files = _worktree_changed_files(worktree)
    diff_stat = git_diff_stat(worktree).strip()
    if not applied or not changed_files or not diff_stat:
        return None
    return {
        "number": applied[0].get("number"),
        "numbers": [item.get("number") for item in applied if item.get("number")],
        "ref": applied[0].get("ref"),
        "refs": [item.get("ref") for item in applied if item.get("ref")],
        "files": sorted({path for item in applied for path in (item.get("files") or [])}),
        "candidates": applied,
        "skipped_candidates": skipped,
        "changed_files": changed_files,
        "diff_stat": diff_stat,
    }
    return None


def _recover_seeded_pr_candidate_diff(
    result: dict[str, Any],
    seeded_diff: dict[str, Any],
    log_path: Path,
) -> dict[str, Any]:
    message = (
        "\nACA recovered this worker by applying a conservative pre-fetched PR candidate diff "
        "after the engine did not produce a usable worker result. The seeded diff will continue "
        "through normal review and verification gates.\n"
        "Seeded PR candidates: "
        + ", ".join(f"#{number}" for number in seeded_diff.get("numbers") or [seeded_diff.get("number")])
        + "\n"
        "Changed files:\n"
        + "\n".join(f"- {path}" for path in seeded_diff.get("changed_files") or [])
        + "\n"
    )
    candidate_notes: list[str] = []
    for candidate in seeded_diff.get("candidates") or []:
        skipped_files = candidate.get("skipped_files") or []
        if not skipped_files:
            candidate_notes.append(f"- #{candidate.get('number')}: applied {', '.join(candidate.get('files') or [])}")
            continue
        skipped_text = "; ".join(
            f"{item.get('path') or '<unknown>'} ({item.get('reason')})" for item in skipped_files
        )
        candidate_notes.append(
            f"- #{candidate.get('number')}: applied {', '.join(candidate.get('files') or [])}; skipped {skipped_text}"
        )
    skipped_candidates = seeded_diff.get("skipped_candidates") or []
    if candidate_notes or skipped_candidates:
        message += "\nPR candidate applicability:\n" + "\n".join(candidate_notes)
        if skipped_candidates:
            message += "\nSkipped candidate diffs:\n" + "\n".join(
                f"- #{item.get('number')}: {item.get('reason')}" for item in skipped_candidates
            )
        message += "\n"
    previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.write_text(previous + message, encoding="utf-8")
    result["stdout"] = f"{result.get('stdout') or ''}{message}"
    result["returncode"] = 0
    result["changed_files"] = list(seeded_diff.get("changed_files") or [])
    result["diff_stat"] = str(seeded_diff.get("diff_stat") or "")
    result["recovered_from_pr_candidate_seed"] = True
    result["seeded_pr_candidate"] = dict(seeded_diff)
    result.setdefault("warnings", []).append("pr_candidate_seeded_after_engine_failure")
    result.pop("failure_reason", None)
    result.pop("blocker_kind", None)
    result.pop("recovery_action", None)
    return result


def _recover_nonzero_result_with_diff(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    *,
    reason: str,
) -> dict[str, Any]:
    changed_files = _worktree_changed_files(worktree)
    diff_text = git_diff_stat(worktree).strip()
    if result.get("returncode", 0) == 0 or not diff_text or not changed_files:
        return result
    stdout_text = str(result.get("stdout") or "")
    reason_text = str(result.get("failure_reason") or reason or "").upper()
    blocker_kind = str(result.get("blocker_kind") or "").lower()
    if (
        "ENGINE_TOOL_LOOP_STALLED" in reason_text
        or blocker_kind == "engine_tool_loop_stalled"
        or "ENGINE_TOOL_LOOP_STALLED" in stdout_text.upper()
    ):
        return _recover_tool_stall_with_diff(result, log_path, worktree, reason=reason)
    if _engine_failure_should_not_recover(result, str(result.get("stdout") or "")):
        return _preserve_partial_worker_diff(result, log_path, worktree, reason=reason)
    message = (
        f"Worker returned a nonzero status ({reason}), but filesystem changes were detected. "
        "Treating as success.\n"
    )
    log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
    result["stdout"] = f"{stdout_text}{message}"
    result["returncode"] = 0
    result["recovered_success"] = True
    result["recovered_failure_reason"] = result.get("failure_reason") or reason
    result.pop("failure_reason", None)
    result.pop("blocker_kind", None)
    result.pop("recovery_action", None)
    result["changed_files"] = changed_files
    result["diff_stat"] = diff_text
    return result


def _worktree_preflight(cfg: ResolvedConfig, worktree: Path) -> tuple[bool, str]:
    if not worktree.exists():
        return False, f"worktree path does not exist: {worktree}"
    if not (worktree / ".git").exists():
        return False, f"worktree git metadata missing: {worktree / '.git'}"
    ok, detail = git_worktree_preflight(worktree)
    if not ok:
        return False, detail
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
    engine_failure = _engine_failure_should_not_recover(result, stdout_text)
    requires_real_diff = require_filesystem_changes or _subtask_requires_real_diff(subtask)
    targets_exist = _target_files_exist(worktree, subtask)
    readable_targets = _readable_target_files(worktree, subtask)
    diff_text = git_diff_stat(worktree).strip()
    diff_touches_targets = _diff_touches_target_files(worktree, subtask)
    changed_files = _worktree_changed_files(worktree)
    if result.get("returncode", 0) != 0 and (not targets_exist or not diff_text):
        recovered, recovered_diff = _wait_for_target_files(worktree, subtask)
        if recovered:
            targets_exist = True
            diff_text = recovered_diff
            readable_targets = _readable_target_files(worktree, subtask)
            diff_touches_targets = _diff_touches_target_files(worktree, subtask)
    if result.get("returncode", 0) != 0 and diff_text and (targets_exist or diff_touches_targets):
        return _recover_nonzero_result_with_diff(
            result,
            log_path,
            worktree,
            reason="target file diff",
        )
    if (
        result.get("returncode", 0) != 0
        and readable_targets
        and not diff_text
        and not requires_real_diff
        and not engine_failure
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
    if result.get("returncode", 0) == 0 and requires_real_diff and not changed_files:
        failure_reason = failure_reason or "NO_FILESYSTEM_CHANGES"
    elif result.get("returncode", 0) == 0 and not diff_text:
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
    elif failure_reason == "ENGINE_PROMPT_TIMEOUT":
        result["blocker_kind"] = "engine_prompt_timeout"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Inspect the preserved engine/session snapshots and reduce prompt/tool scope before retrying."
            )
    elif failure_reason == "ENGINE_SESSION_RUN_CONFLICT":
        result["blocker_kind"] = "engine_session_run_conflict"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Wait for the active Tandem engine session run to finish or clear it, then reset the task to Backlog."
            )
    elif failure_reason == "ENGINE_TOOL_LOOP_STALLED":
        result["blocker_kind"] = "engine_tool_loop_stalled"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Inspect the preserved partial diff and engine messages, reset the task to Backlog, and retry from a clean checkout."
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
    if diff_text and failure_reason == "ENGINE_TOOL_LOOP_STALLED":
        return _recover_tool_stall_with_diff(result, log_path, worktree, reason=failure_reason)
    if diff_text and _engine_failure_should_not_recover(result, str(result.get("stdout") or stdout_text)):
        return _preserve_partial_worker_diff(result, log_path, worktree, reason=failure_reason)
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
        if role == "manager" and not require_tool_use and not write_required:
            session_tool_allowlist: list[str] | None = []
            session_permission_rules: list[dict[str, str]] | None = None
            prompt_tool_mode = "none"
        else:
            session_tool_allowlist = SESSION_TOOL_ALLOWLIST
            session_permission_rules = SESSION_PERMISSION_RULES
            prompt_tool_mode = "required" if require_tool_use else "auto"
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
                            permission_rules=session_permission_rules,
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

                if role.startswith("worker") and write_required:
                    engine_meta["fallback_mode"] = "prompt_sync_first"
                    failure_reason = ""
                    blocker_kind = ""
                    recovery_action = ""
                    try:
                        sync_response = _prompt_sync_with_connect_retries(
                            cfg,
                            engine_meta=engine_meta,
                            log=log,
                            role=role,
                            session_id=session_id,
                            prompt=prompt,
                            tool_allowlist=session_tool_allowlist,
                            tool_mode=prompt_tool_mode,
                            require_tool_use=require_tool_use,
                            write_required=write_required,
                            timeout_seconds=_engine_prompt_sync_timeout_seconds(cfg),
                        )
                        engine_meta["sync_snapshot_path"] = _write_engine_snapshot(
                            log_path,
                            f"engine-sync-{session_id}",
                            sync_response,
                        )
                        stdout_text = _extract_prompt_sync_text(sync_response)
                    except Exception as exc:
                        conflict = _engine_session_run_conflict(exc)
                        if conflict:
                            conflict_run_id, retry_after_ms = conflict
                            last_run_id = conflict_run_id or last_run_id
                            engine_meta["run_id"] = conflict_run_id
                            engine_meta["sync_conflict"] = {
                                "run_id": conflict_run_id,
                                "retry_after_ms": retry_after_ms,
                            }
                            failure_reason = "ENGINE_SESSION_RUN_CONFLICT"
                            blocker_kind = "engine_session_run_conflict"
                            stdout_text = (
                                "ENGINE_SESSION_RUN_CONFLICT: Tandem engine had an active run "
                                "when ACA attempted prompt_sync on a fresh worker session. "
                                f"Active run: {conflict_run_id or 'unknown'}.\n"
                            )
                        elif _engine_exception_is_timeout(exc):
                            failure_reason = "ENGINE_PROMPT_TIMEOUT"
                            blocker_kind = "engine_prompt_timeout"
                            stdout_text = (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt "
                                f"did not finish within {_engine_prompt_sync_timeout_seconds(cfg):.0f}s.\n"
                            )
                        elif _engine_exception_is_connection_failure(exc):
                            failure_reason = "ENGINE_WORKSPACE_UNREACHABLE"
                            blocker_kind = "engine_workspace_unreachable"
                            stdout_text = (
                                "ENGINE_WORKSPACE_UNREACHABLE: Tandem engine prompt_sync worker prompt "
                                "could not reach the engine after connection retries.\n"
                            )
                        else:
                            raise
                    completed = bool(stdout_text.strip()) and not failure_reason
                    if not completed and not failure_reason:
                        failure_reason = "ENGINE_EMPTY_RESPONSE"
                        blocker_kind = "engine_empty_response"
                        stdout_text = (
                            "ENGINE_EMPTY_RESPONSE: Tandem engine prompt_sync worker prompt "
                            "finished without assistant transcript text.\n"
                        )
                    if stdout_text and not stdout_text.endswith("\n"):
                        stdout_text += "\n"
                    if stdout_text:
                        log.write(stdout_text)
                        log.flush()
                        _print_line(role, stdout_text)
                    if not completed and session_id:
                        _recovered_text, recovery = _recover_engine_text_from_state(
                            cfg,
                            session_id=session_id,
                            run_id=last_run_id,
                            log_path=log_path,
                        )
                        recovery["run_id"] = last_run_id
                        recovery["attempt"] = 0
                        recovery["stream_reason"] = blocker_kind or "prompt_sync_first"
                        engine_meta.setdefault("recovery", []).append(recovery)
                        for key in ("events_path", "messages_path"):
                            if recovery.get(key):
                                engine_meta[key] = recovery[key]
                    if blocker_kind == "engine_session_run_conflict":
                        recovery_action = (
                            "Wait for the active Tandem engine session run to finish or clear it, then reset the task to Backlog."
                        )
                    elif blocker_kind == "engine_prompt_timeout":
                        recovery_action = (
                            "Inspect engine/session snapshots and retry with a smaller scoped prompt or healthier provider route."
                        )
                    elif blocker_kind:
                        recovery_action = (
                            "Check Tandem engine provider/model routing and persisted engine snapshots, "
                            "then retry the task after the engine returns assistant text."
                        )
                    return {
                        "role": role,
                        "returncode": 0 if completed else 1,
                        "stdout": stdout_text,
                        "log_path": str(log_path),
                        "cwd": str(cwd),
                        "session_id": session_id,
                        "engine_run_id": last_run_id,
                        "engine": engine_meta,
                        "failure_reason": failure_reason,
                        "blocker_kind": blocker_kind,
                        "recovery_action": recovery_action,
                    }

                def _run_async_once(prompt_text: str, attempt: int) -> tuple[str, bool, str, str]:
                    async_result = sdk_sessions_prompt_async(
                        cfg,
                        session_id=session_id,
                        prompt=prompt_text,
                        tool_mode=prompt_tool_mode,
                        tool_allowlist=session_tool_allowlist,
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
                        sdk_stream_run_text(
                            cfg,
                            session_id,
                            run_id,
                            _writer,
                            timeout_seconds=_engine_prompt_timeout_seconds(cfg),
                            no_text_timeout_seconds=_engine_no_text_timeout_seconds(cfg),
                            max_events_without_text=_engine_max_events_without_text(cfg, role),
                            stop_when_text=(
                                _manager_plan_stream_complete
                                if role == "manager" and not require_tool_use and not write_required
                                else None
                            ),
                        )
                        if run_id
                        else {"text": "", "completed": False}
                    )
                    streamed_text = str(stream_result.get("text") or "")
                    completed = bool(stream_result.get("completed"))
                    stream_reason = str(stream_result.get("reason") or "").strip()
                    if stream_reason:
                        engine_meta["stream_reason"] = stream_reason
                    if stream_result.get("event_count") is not None:
                        engine_meta["stream_event_count"] = stream_result.get("event_count")
                    if completed and streamed_text.strip():
                        return streamed_text, True, run_id, stream_reason
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
                        return recovered_text, True, run_id, stream_reason
                    return streamed_text, completed and bool(streamed_text.strip()), run_id, stream_reason

                stdout_text, completed, run_id, stream_reason = _run_async_once(prompt, 0)
                if not completed and stream_reason not in TERMINAL_ENGINE_STREAM_REASONS:
                    retry_notice = (
                        f"ENGINE_EMPTY_RESPONSE_RETRY: engine run {run_id or 'unknown'} completed "
                        "without transcript text; retrying once in the same session.\n"
                    )
                    log.write(retry_notice)
                    log.flush()
                    _print_line(role, retry_notice)
                    retry_text, retry_completed, retry_run_id, retry_reason = _run_async_once(
                        _empty_transcript_retry_prompt(
                            role=role,
                            require_tool_use=require_tool_use,
                            write_required=write_required,
                        ),
                        1,
                    )
                    stream_reason = retry_reason or stream_reason
                    if retry_completed:
                        stdout_text = retry_text
                        completed = True
                        run_id = retry_run_id
                    else:
                        stdout_text = retry_text or stdout_text
                        run_id = retry_run_id or run_id

                fallback_failure_reason = ""
                fallback_blocker_kind = ""
                fallback_failure_message = ""
                if not completed and stream_reason not in TERMINAL_ENGINE_STREAM_REASONS:
                    engine_meta["fallback_mode"] = "prompt_sync"
                    fallback_notice = (
                        f"ENGINE_EMPTY_RESPONSE_FALLBACK: engine run {run_id or 'unknown'} still had "
                        "no transcript text; using prompt_sync fallback.\n"
                    )
                    log.write(fallback_notice)
                    log.flush()
                    _print_line(role, fallback_notice)
                    sync_deadline = time.monotonic() + _engine_sync_conflict_wait_seconds(cfg)
                    while not completed:
                        try:
                            sync_response = _prompt_sync_with_connect_retries(
                                cfg,
                                engine_meta=engine_meta,
                                log=log,
                                role=role,
                                session_id=session_id,
                                prompt=_empty_transcript_retry_prompt(
                                    role=role,
                                    require_tool_use=require_tool_use,
                                    write_required=write_required,
                                ),
                                tool_allowlist=session_tool_allowlist,
                                tool_mode=prompt_tool_mode,
                                require_tool_use=require_tool_use,
                                write_required=write_required,
                                timeout_seconds=_engine_prompt_sync_timeout_seconds(cfg),
                            )
                        except Exception as exc:
                            conflict = _engine_session_run_conflict(exc)
                            if not conflict:
                                if _engine_exception_is_timeout(exc):
                                    fallback_failure_reason = "ENGINE_PROMPT_TIMEOUT"
                                    fallback_blocker_kind = "engine_prompt_timeout"
                                    fallback_failure_message = (
                                        "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync fallback timed out "
                                        "after an empty async transcript. "
                                        f"Last engine run: {run_id or last_run_id or 'unknown'}.\n"
                                    )
                                    break
                                if _engine_exception_is_connection_failure(exc):
                                    fallback_failure_reason = "ENGINE_WORKSPACE_UNREACHABLE"
                                    fallback_blocker_kind = "engine_workspace_unreachable"
                                    fallback_failure_message = (
                                        "ENGINE_WORKSPACE_UNREACHABLE: Tandem engine prompt_sync fallback "
                                        "could not reach the engine after connection retries.\n"
                                    )
                                    break
                                raise
                            conflict_run_id, retry_after_ms = conflict
                            if conflict_run_id:
                                run_id = conflict_run_id
                                last_run_id = conflict_run_id
                                engine_meta["run_id"] = conflict_run_id
                            engine_meta["sync_conflict"] = {
                                "run_id": conflict_run_id or run_id or last_run_id,
                                "retry_after_ms": retry_after_ms,
                            }
                            conflict_notice = (
                                "ENGINE_SESSION_RUN_CONFLICT: prompt_sync fallback found an active "
                                f"engine run {conflict_run_id or run_id or last_run_id or 'unknown'}; "
                                "waiting for the active run before retrying fallback.\n"
                            )
                            log.write(conflict_notice)
                            log.flush()
                            _print_line(role, conflict_notice)
                            recovered_text, recovery = _recover_engine_text_from_state(
                                cfg,
                                session_id=session_id,
                                run_id=conflict_run_id or run_id or last_run_id,
                                log_path=log_path,
                            )
                            recovery["run_id"] = conflict_run_id or run_id or last_run_id
                            recovery["attempt"] = engine_meta.get("retry_count", 0)
                            recovery["stream_reason"] = "session_run_conflict"
                            engine_meta.setdefault("recovery", []).append(recovery)
                            for key in ("events_path", "messages_path"):
                                if recovery.get(key):
                                    engine_meta[key] = recovery[key]
                            if recovered_text.strip():
                                stdout_text = recovered_text
                                completed = True
                                break
                            remaining = sync_deadline - time.monotonic()
                            if remaining <= 0:
                                fallback_failure_reason = "ENGINE_SESSION_RUN_CONFLICT"
                                fallback_blocker_kind = "engine_session_run_conflict"
                                fallback_failure_message = (
                                    "ENGINE_SESSION_RUN_CONFLICT: Tandem engine still had an active run "
                                    "when ACA attempted prompt_sync fallback after an empty async transcript. "
                                    f"Active run: {conflict_run_id or run_id or last_run_id or 'unknown'}.\n"
                                )
                                break
                            time.sleep(min(max(retry_after_ms / 1000.0, 0.1), remaining, 2.0))
                            continue
                        engine_meta["sync_snapshot_path"] = _write_engine_snapshot(
                            log_path,
                            f"engine-sync-{session_id}",
                            sync_response,
                        )
                        sync_text = _extract_prompt_sync_text(sync_response)
                        if sync_text.strip():
                            stdout_text = sync_text
                            completed = True
                        break

                failure_reason = ""
                blocker_kind = ""
                if completed:
                    if stdout_text and not stdout_text.endswith("\n"):
                        stdout_text += "\n"
                elif fallback_failure_reason:
                    failure_reason = fallback_failure_reason
                    blocker_kind = fallback_blocker_kind
                    stdout_text = fallback_failure_message
                elif stream_reason == "timeout":
                    failure_reason = "ENGINE_PROMPT_TIMEOUT"
                    blocker_kind = "engine_prompt_timeout"
                    stdout_text = (
                        "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response "
                        f"within {_engine_prompt_timeout_seconds(cfg):.0f}s. Last engine run: "
                        f"{run_id or last_run_id or 'unknown'}.\n"
                    )
                elif stream_reason in {"no_text_timeout", "max_events_without_text"}:
                    failure_reason = "ENGINE_TOOL_LOOP_STALLED"
                    blocker_kind = "engine_tool_loop_stalled"
                    stdout_text = (
                        "ENGINE_TOOL_LOOP_STALLED: Tandem engine produced tool/permission activity without "
                        "assistant progress long enough to trip the ACA watchdog. Last engine run: "
                        f"{run_id or last_run_id or 'unknown'}.\n"
                    )
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
                if not completed and session_id:
                    _recovered_text, recovery = _recover_engine_text_from_state(
                        cfg,
                        session_id=session_id,
                        run_id=run_id or last_run_id,
                        log_path=log_path,
                    )
                    recovery["run_id"] = run_id or last_run_id
                    recovery["attempt"] = engine_meta.get("retry_count", 0)
                    recovery["stream_reason"] = stream_reason
                    engine_meta.setdefault("recovery", []).append(recovery)
                    for key in ("events_path", "messages_path"):
                        if recovery.get(key):
                            engine_meta[key] = recovery[key]
                recovery_action = ""
                if blocker_kind == "engine_tool_loop_stalled":
                    recovery_action = (
                        "Preserve the partial diff and engine messages, reset the checkout before retry, "
                        "and narrow the worker prompt/tool scope."
                    )
                elif blocker_kind == "engine_session_run_conflict":
                    recovery_action = (
                        "Wait for the active Tandem engine session run to finish or clear it, then reset the task to Backlog."
                    )
                elif blocker_kind == "engine_prompt_timeout":
                    recovery_action = (
                        "Inspect engine/session snapshots and retry with a smaller scoped prompt or healthier provider route."
                    )
                elif blocker_kind:
                    recovery_action = (
                        "Check Tandem engine provider/model routing and persisted engine snapshots, "
                        "then retry the task after the engine returns assistant text."
                    )
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
                    "recovery_action": recovery_action,
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
                        _call_with_timeout(lambda: delete_tandem_session(cfg, session_id), timeout_seconds=5.0)
                    except TimeoutError:
                        logger.debug("Timed out deleting tandem session %s", session_id)
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
    changed_files = list(result.get("changed_files") or _worktree_changed_files(worktree))
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
    
    worker_model_selection = engine_session_provider_model(cfg, "worker")
    worker_cli_provider = worker_model_selection["provider"]
    worker_model = worker_model_selection["model"]
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
    
    seeded_diff: dict[str, Any] | None = None
    if (
        result.get("returncode") != 0
        and str(result.get("blocker_kind") or "") in {"worker_no_diff", "engine_tool_loop_stalled"}
        and _subtask_requires_real_diff(subtask)
        and not _worktree_changed_files(worktree)
    ):
        seeded_diff = _seed_pr_candidate_diff(worktree, subtask, log_path)
        if seeded_diff:
            append_event(
                layout["events"],
                "worker.pr_candidate_seeded",
                run_id,
                {
                    "worker_id": worker_id,
                    "subtask_id": subtask["id"],
                    "pr_number": seeded_diff.get("number"),
                    "pr_numbers": seeded_diff.get("numbers") or [],
                    "ref": seeded_diff.get("ref"),
                    "refs": seeded_diff.get("refs") or [],
                    "files": seeded_diff.get("files") or [],
                    "candidates": seeded_diff.get("candidates") or [],
                    "skipped_candidates": seeded_diff.get("skipped_candidates") or [],
                    "changed_files": seeded_diff.get("changed_files") or [],
                },
                task_id=task.get("task_id"),
                role="worker",
                repo={"path": str(repo_path)},
            )
            result = _recover_seeded_pr_candidate_diff(result, seeded_diff, log_path)

    if _worker_result_should_retry(result):
        retry_prompt = prompt + _worker_prompt_retry_suffix(subtask)
        if seeded_diff:
            retry_prompt += (
                "\n\nACA runtime recovery already seeded a conservative candidate diff before this retry.\n"
                f"- Seeded PR: #{seeded_diff.get('number')} ({seeded_diff.get('ref')})\n"
                "- Seeded files: "
                + ", ".join(f"`{path}`" for path in seeded_diff.get("files") or [])
                + "\n"
                "- Inspect this diff first. Keep and refine it if it is safe; revert only if it is demonstrably wrong.\n"
                "- Do not spend the retry on a broad applicability matrix before verifying the seeded diff.\n"
            )
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
        if retry_result["returncode"] != 0:
            retry_result = _recover_nonzero_result_with_diff(
                retry_result,
                log_path,
                worktree,
                reason=str(retry_result.get("failure_reason") or "retry produced diff"),
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
