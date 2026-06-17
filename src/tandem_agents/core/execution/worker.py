from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import subprocess
import tempfile
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
from src.tandem_agents.utils.utils import atomic_write_json, now_ms
from src.tandem_agents.core.engine.tandem_client_sdk import (
    sdk_session_messages,
    sdk_sessions_prompt_async,
    sdk_run_events,
    sdk_stream_run_text,
)

PRINT_LOCK = threading.Lock()
WORKER_STATUS_LOCK = threading.Lock()
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


def _worker_execution_worktree_name(worker_id: str, subtask: dict[str, Any]) -> str:
    override = str(subtask.get("_worker_worktree_name") or "").strip()
    if override and "/" not in override and "\\" not in override and ".." not in override:
        return override
    return worker_worktree_name(worker_id, subtask.get("id"))
WORKER_FAILURE_MARKERS = (
    "ENGINE_ERROR:",
    "TOOL_MODE_REQUIRED_NOT_SATISFIED",
    "WRITE_REQUIRED_NOT_SATISFIED",
    "ENGINE_EMPTY_RESPONSE",
    "ENGINE_PROMPT_TIMEOUT",
    "ENGINE_TOOL_LOOP_STALLED",
    "ENGINE_SESSION_RUN_CONFLICT",
    "TERMINALIZED_WITH_REMAINING_BLOCKERS",
)
WORKER_BLOCKED_STATUS_RE = re.compile(
    r"(?im)^\s*(?:status\s*:\s*blocked.*|blocked(?:\s*(?:[^\w\s].*)?)?)\s*$"
)

TERMINAL_ENGINE_STREAM_REASONS = {"timeout", "no_text_timeout", "dispatch_timeout"}
NON_RETRYABLE_WORKER_BLOCKERS = {
    "coordination_lost",
    "engine_empty_response",
    "engine_prompt_timeout",
    "engine_session_run_conflict",
    "engine_tool_loop_stalled_no_diff",
    "engine_provider_auth",
    "worker_incomplete_diff",
    "worker_corrupt_diff",
    "ignored_path_changes",
}
PR_CANDIDATE_SEED_CODE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".py")
PR_CANDIDATE_IMPORT_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")
PR_CANDIDATE_SEED_EXCLUDED_PREFIXES = (".jules/", "jules/")
PR_CANDIDATE_SEED_EXCLUDED_FILES = {".jules/bolt.md", "jules/bolt.md"}
METADATA_ONLY_TARGET_FILES = {
    "cargo.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
}
SOURCE_OR_TEST_TARGET_EXTENSIONS = {
    ".rs",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".sh",
}
SUPPORT_ONLY_TARGET_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".yml", ".yaml", ".toml", ".json"}


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
    return 90.0


def _worker_prompt_sync_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS=%s", raw)
    return 300.0


def _worker_prompt_sync_max_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS=%s", raw)
    return 480.0


def _prompt_sync_timeout_seconds(cfg: ResolvedConfig, role: str, write_required: bool) -> float:
    if role.startswith("worker") and write_required:
        return _worker_prompt_sync_timeout_seconds(cfg)
    return _engine_prompt_sync_timeout_seconds(cfg)


def _scaled_prompt_sync_timeout_seconds(
    cfg: ResolvedConfig,
    role: str,
    write_required: bool,
    timeout_multiplier: float,
) -> float:
    timeout = _scaled_timeout_seconds(
        _prompt_sync_timeout_seconds(cfg, role, write_required),
        timeout_multiplier,
    )
    if role.startswith("worker") and write_required:
        return min(timeout, _worker_prompt_sync_max_timeout_seconds(cfg))
    return timeout


def _worker_terminalize_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(10.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS=%s", raw)
    return 60.0


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


def _worker_prompt_sync_first_enabled(cfg: ResolvedConfig) -> bool:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_PROMPT_SYNC_FIRST", "") or "").strip().lower()
    if raw:
        return raw not in {"0", "false", "no", "off"}
    return True


def _use_prompt_sync_first(cfg: ResolvedConfig, override: bool | None) -> bool:
    if override is not None:
        return override
    return _worker_prompt_sync_first_enabled(cfg)


def _skip_tool_recovery_when_partial_diff_exists(
    *,
    role: str,
    write_required: bool,
    cwd: Path,
    blocker_kind: str,
) -> bool:
    if not role.startswith("worker") or not write_required:
        return False
    if blocker_kind not in {"engine_prompt_timeout", "engine_empty_response"}:
        return False
    try:
        return bool(_worktree_changed_files(cwd))
    except Exception:
        logger.debug("Failed to inspect worker worktree before async recovery", exc_info=True)
        return False


def _scaled_timeout_seconds(timeout_seconds: float, multiplier: float) -> float:
    return min(600.0, max(1.0, float(timeout_seconds) * max(1.0, float(multiplier or 1.0))))


def _worker_timeout_multiplier(subtask: dict[str, Any]) -> float:
    merged_count = len(subtask.get("merged_subtasks") or [])
    targets = _subtask_targets(subtask)
    target_count = len(targets)
    if merged_count <= 1 and target_count <= 6:
        new_crate_contract = "Cargo.toml" in targets and any(
            path.startswith("crates/") and path.endswith("/Cargo.toml")
            for path in targets
        )
        module_contract = target_count >= 5 and any("/src/" in path for path in targets)
        if not new_crate_contract and not module_contract:
            return 1.0
        return min(2.5, 1.4 + (0.12 * target_count))
    if merged_count > 1:
        return min(3.0, 1.0 + (0.25 * merged_count) + (0.05 * target_count))
    return min(2.0, 1.0 + (0.08 * max(0, target_count - 6)))


def _engine_exception_is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "operation did not finish within" in text


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
            timeout_seconds = float(kwargs.get("timeout_seconds") or _engine_prompt_sync_timeout_seconds(cfg))
            return _call_with_timeout(
                lambda: prompt_tandem_session_sync(cfg, **kwargs),
                timeout_seconds=timeout_seconds,
            )
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
            if isinstance(messages, list):
                recovery["message_count"] = len(messages)
                tool_part_count = 0
                assistant_message_count = 0
                for message in messages:
                    message_dict = _message_dict(message)
                    if not message_dict:
                        continue
                    if _message_role(message_dict) == "assistant":
                        assistant_message_count += 1
                    parts = message_dict.get("parts") or message_dict.get("content") or []
                    if isinstance(parts, list):
                        tool_part_count += sum(1 for part in parts if isinstance(part, dict) and part.get("type") == "tool")
                recovery["tool_part_count"] = tool_part_count
                recovery["assistant_message_count"] = assistant_message_count
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


def _normalize_target_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").strip("/")


def _subtask_search_text(subtask: dict[str, Any]) -> str:
    return " ".join(
        [
            str(subtask.get("title") or ""),
            str(subtask.get("goal") or ""),
            str(subtask.get("description") or ""),
            " ".join(str(entry or "") for entry in subtask.get("acceptance_criteria") or []),
            " ".join(str(entry or "") for entry in subtask.get("deliverables") or []),
        ]
    ).lower()


def _subtask_is_ci_workflow_task(subtask: dict[str, Any]) -> bool:
    text = f" {_subtask_search_text(subtask).replace('-', ' ')} "
    ci_markers = (" ci ", " workflow ", " github actions ", " pull request ", " pull_request ", " pr ")
    action_markers = (" run ", " gate ", " check ", " tests ", " test ")
    return any(marker in text for marker in ci_markers) and any(marker in text for marker in action_markers)


def _subtask_requires_in_module_private_helper_tests(subtask: dict[str, Any]) -> bool:
    text = _subtask_search_text(subtask)
    private_helper_markers = (
        "resolve_registered_tool",
        "resolve_tool_path",
        "is_within_workspace_root",
        "approval_classifier::classify",
        "standing_allow_is_unsafe",
    )
    return any(marker in text for marker in private_helper_markers)


def _is_ci_workflow_path(path: str) -> bool:
    rel_path = _normalize_target_path(path).lower()
    return rel_path.startswith(".github/workflows/") and rel_path.endswith((".yml", ".yaml"))


def _is_metadata_only_target_path(path: str) -> bool:
    rel_path = _normalize_target_path(path)
    return bool(rel_path) and Path(rel_path).name.lower() in METADATA_ONLY_TARGET_FILES


def _is_source_or_test_target_path(path: str) -> bool:
    rel_path = _normalize_target_path(path).lower()
    if not rel_path:
        return False
    if "/tests/" in f"/{rel_path}/" or rel_path.startswith("tests/"):
        return True
    name = Path(rel_path).name
    if name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return True
    return any(rel_path.endswith(ext) for ext in SOURCE_OR_TEST_TARGET_EXTENSIONS)


def _is_support_only_target_path(path: str) -> bool:
    rel_path = _normalize_target_path(path).lower()
    if not rel_path:
        return False
    if _is_metadata_only_target_path(rel_path):
        return True
    if rel_path.startswith("docs/") or "/docs/" in f"/{rel_path}/":
        return True
    return any(rel_path.endswith(ext) for ext in SUPPORT_ONLY_TARGET_EXTENSIONS)


def _substantive_target_files(subtask: dict[str, Any]) -> list[str]:
    targets = _subtask_targets(subtask)
    source_or_test_targets = [path for path in targets if _is_source_or_test_target_path(path)]
    if source_or_test_targets:
        return source_or_test_targets
    return [path for path in targets if not _is_metadata_only_target_path(path)]


def _support_only_changed_files_for_subtask(subtask: dict[str, Any], changed_files: list[str]) -> bool:
    if not _substantive_target_files(subtask):
        return False
    if _subtask_is_ci_workflow_task(subtask) and all(_is_ci_workflow_path(path) for path in changed_files):
        return False
    return bool(changed_files) and all(_is_support_only_target_path(path) for path in changed_files)


def _subtask_requires_substantive_target_diff(
    subtask: dict[str, Any],
    *,
    requires_real_diff: bool,
) -> bool:
    if not requires_real_diff or not _substantive_target_files(subtask):
        return False
    text = f" {_subtask_search_text(subtask).replace('-', ' ')} "
    markers = (
        " coverage",
        " fixture",
        " assertion",
        " assertions",
        " quality gate",
        " quality gates",
        " end to end",
        " smoke",
        " test ",
        " tests ",
        " behavior",
        " behaviour",
        " regression",
    )
    return any(marker in text for marker in markers)


def _git_ignored_paths(worktree: Path, paths: list[str]) -> list[str]:
    ignored: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        rel_path = str(raw_path or "").strip().replace("\\", "/").strip("/")
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)
        try:
            result = run_command(git_command_for_worktree(worktree, "check-ignore", "--quiet", "--", rel_path))
        except Exception:
            continue
        if result.returncode == 0:
            ignored.append(rel_path)
    return ignored


def _annotate_ignored_target_files(worktree: Path, subtask: dict[str, Any]) -> dict[str, Any]:
    targets = _subtask_targets(subtask)
    ignored = set(_git_ignored_paths(worktree, targets))
    if not ignored:
        return subtask
    prepared = dict(subtask)
    prepared["ignored_target_files"] = sorted(ignored)
    for key in ("files", "target_files"):
        current = [str(entry or "").strip().replace("\\", "/").strip("/") for entry in prepared.get(key) or []]
        filtered = [entry for entry in current if entry and entry not in ignored]
        prepared[key] = filtered
    return prepared


def _ignored_existing_target_files(worktree: Path, subtask: dict[str, Any]) -> list[str]:
    candidates = _subtask_targets(subtask)
    candidates.extend(str(entry or "").strip() for entry in subtask.get("ignored_target_files") or [])
    ignored = _git_ignored_paths(worktree, candidates)
    return [path for path in ignored if (worktree / path).exists()]


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


def _diff_touches_substantive_target_files(worktree: Path, subtask: dict[str, Any]) -> bool:
    if _diff_touches_ci_workflow_files(worktree, subtask):
        return True
    targets = set(_substantive_target_files(subtask))
    if not targets:
        return False
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        return False
    changed = {str(change.get("path") or "").strip() for change in changes}
    return bool(changed.intersection(targets))


def _package_root_for_path(path: str) -> str:
    rel_path = _normalize_target_path(path)
    parts = [part for part in rel_path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"crates", "packages", "apps"}:
        return "/".join(parts[:2])
    if len(parts) >= 1 and parts[-1] in {"Cargo.toml", "package.json", "pyproject.toml"}:
        return "/".join(parts[:-1])
    return ""


def _path_is_test_file(path: str) -> bool:
    rel_path = _normalize_target_path(path)
    name = Path(rel_path).name.lower()
    return (
        "/tests/" in f"/{rel_path}/"
        or rel_path.startswith("tests/")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
        or (name.endswith(".rs") and rel_path.endswith("/tests/" + name))
    )


def _diff_touches_nearby_test_files(worktree: Path, subtask: dict[str, Any]) -> bool:
    if _subtask_requires_in_module_private_helper_tests(subtask):
        return False
    if not _subtask_requires_substantive_target_diff(subtask, requires_real_diff=True):
        return False
    roots = {
        root
        for root in (_package_root_for_path(path) for path in _subtask_targets(subtask))
        if root
    }
    if not roots:
        return False
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        return False
    for change in changes:
        changed_path = _normalize_target_path(str(change.get("path") or ""))
        if not changed_path or _is_aca_repo_artifact_path(changed_path) or not _path_is_test_file(changed_path):
            continue
        for root in roots:
            if changed_path == root or changed_path.startswith(f"{root}/"):
                return True
    return False


def _diff_touches_ci_workflow_files(worktree: Path, subtask: dict[str, Any]) -> bool:
    if not _subtask_is_ci_workflow_task(subtask):
        return False
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        return False
    return any(_is_ci_workflow_path(str(change.get("path") or "")) for change in changes)


def _diff_satisfies_targeted_subtask(
    subtask: dict[str, Any],
    *,
    requires_real_diff: bool,
    targets_exist: bool,
    diff_touches_targets: bool,
    diff_touches_substantive_targets: bool,
) -> bool:
    targets = _subtask_targets(subtask)
    if not targets:
        return True
    if _subtask_requires_substantive_target_diff(
        subtask,
        requires_real_diff=requires_real_diff,
    ):
        return diff_touches_substantive_targets
    if requires_real_diff:
        return diff_touches_targets
    return targets_exist or diff_touches_targets


def _is_aca_repo_artifact_path(path: str) -> bool:
    rel_path = str(path or "").strip().replace("\\", "/")
    name = Path(rel_path).name
    if not rel_path:
        return True
    if rel_path.startswith(".aca/"):
        return True
    if name == "__aca_temp_probe.txt":
        return True
    if name == ".aca_worker_blocker_note.txt":
        return True
    if name.startswith("aca-") and name.endswith(".md"):
        return True
    if name.startswith("ACA_") and name.endswith(".md"):
        return True
    return False


def _non_aca_worktree_changes(worktree: Path) -> list[dict[str, Any]]:
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        return []
    filtered: list[dict[str, Any]] = []
    for change in changes:
        path = str(change.get("path") or "").strip()
        if path and not _is_aca_repo_artifact_path(path):
            filtered.append(change)
    return filtered


def _worktree_changed_files(worktree: Path) -> list[str]:
    changed: list[str] = []
    for change in _non_aca_worktree_changes(worktree):
        path = str(change.get("path") or "").strip()
        if path:
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


def _changed_files_have_substantive_content(worktree: Path, changed_files: list[str]) -> bool:
    """Return true when changed files contain reviewable content.

    `git status --short` reports a new zero-byte file as a change, but that is
    not enough to satisfy write-required ACA work. Tracked deletions and binary
    changes are still considered substantive.
    """

    paths = [path for path in changed_files if path and not _is_aca_repo_artifact_path(path)]
    if not paths:
        return False
    try:
        changes = list_worktree_changes(worktree)
    except Exception:
        return True
    if not changes:
        return True
    status_by_path = {str(change.get("path") or ""): str(change.get("status") or "") for change in changes}
    for path in paths:
        status = status_by_path.get(path, "")
        file_path = worktree / path
        if status.strip().startswith("?"):
            try:
                if file_path.is_file() and file_path.stat().st_size > 0:
                    return True
            except OSError:
                continue
            continue
        if "D" in status or "R" in status:
            return True
        diff_proc = run_command(git_command_for_worktree(worktree, "diff", "--numstat", "HEAD", "--", path))
        if diff_proc.returncode != 0:
            return True
        for line in diff_proc.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            added, deleted = parts[0], parts[1]
            if added == "-" or deleted == "-":
                return True
            try:
                if int(added) + int(deleted) > 0:
                    return True
            except ValueError:
                return True
    return False


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


def _worker_async_prompt_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_ASYNC_PROMPT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_WORKER_ASYNC_PROMPT_TIMEOUT_SECONDS=%s", raw)
    return 120.0


def _worker_async_no_text_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(15.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_WORKER_ASYNC_NO_TEXT_TIMEOUT_SECONDS=%s", raw)
    return 60.0


def _engine_async_dispatch_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_ASYNC_DISPATCH_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(2.0, float(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_ASYNC_DISPATCH_TIMEOUT_SECONDS=%s", raw)
    return 20.0


def _async_prompt_timeout_seconds(cfg: ResolvedConfig, role: str, write_required: bool) -> float:
    timeout = _engine_prompt_timeout_seconds(cfg)
    if role.startswith("worker") and write_required:
        return min(timeout, _worker_async_prompt_timeout_seconds(cfg))
    return timeout


def _async_no_text_timeout_seconds(cfg: ResolvedConfig, role: str, write_required: bool) -> float:
    timeout = _engine_no_text_timeout_seconds(cfg)
    if role.startswith("worker") and write_required:
        return min(timeout, _worker_async_no_text_timeout_seconds(cfg))
    return timeout


def _scaled_async_prompt_timeout_seconds(
    cfg: ResolvedConfig,
    role: str,
    write_required: bool,
    timeout_multiplier: float,
) -> float:
    timeout = _scaled_timeout_seconds(
        _async_prompt_timeout_seconds(cfg, role, write_required),
        timeout_multiplier,
    )
    if role.startswith("worker") and write_required:
        return min(timeout, _worker_async_prompt_timeout_seconds(cfg))
    return timeout


def _scaled_async_no_text_timeout_seconds(
    cfg: ResolvedConfig,
    role: str,
    write_required: bool,
    timeout_multiplier: float,
) -> float:
    timeout = _scaled_timeout_seconds(
        _async_no_text_timeout_seconds(cfg, role, write_required),
        timeout_multiplier,
    )
    if role.startswith("worker") and write_required:
        return min(timeout, _worker_async_no_text_timeout_seconds(cfg))
    return timeout


def _engine_max_events_without_text(cfg: ResolvedConfig, role: str) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_MAX_EVENTS_WITHOUT_TEXT", "") or "").strip()
    if raw:
        try:
            return max(10, int(raw))
        except ValueError:
            logger.debug("Invalid ACA_ENGINE_MAX_EVENTS_WITHOUT_TEXT=%s", raw)
    return 0


def _preserve_partial_worker_diff(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    *,
    reason: str,
) -> dict[str, Any]:
    stdout_text = str(result.get("stdout") or "")
    normalized_reason = str(result.get("failure_reason") or reason or "").strip()
    preserve_existing_reason = normalized_reason in {
        "TARGET_FILES_UNCHANGED",
        "IGNORED_PATH_CHANGES",
        "NO_FILESYSTEM_CHANGES",
    }
    if preserve_existing_reason:
        pass
    elif "ENGINE_ERROR:" in stdout_text:
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
    result["changed_files"] = _worktree_changed_files(worktree)
    if result["changed_files"]:
        try:
            diff_text = _applyable_working_diff(worktree)
            if not diff_text.strip():
                diff_text = git_working_diff(worktree)
        except Exception:
            logger.debug("Failed to capture partial worker diff", exc_info=True)
        status_rows = []
        for change in _non_aca_worktree_changes(worktree):
            status = str(change.get("status") or "").strip() or "??"
            path = str(change.get("path") or "").strip()
            if path:
                status_rows.append(f"{status:>2} {path}")
        status_text = "\n".join(status_rows)
    if diff_text.strip():
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


def _applyable_working_diff(worktree: Path) -> str:
    """Return a git-applyable patch for tracked and untracked worktree changes."""

    changes = _non_aca_worktree_changes(worktree)
    if not changes:
        return ""
    untracked = [
        str(change.get("path") or "").strip()
        for change in changes
        if str(change.get("status") or "").strip().startswith("?")
        and str(change.get("path") or "").strip()
    ]
    with tempfile.NamedTemporaryFile(prefix="aca-git-index-") as index_file:
        env = {"GIT_INDEX_FILE": index_file.name}
        read_tree = run_command(git_command_for_worktree(worktree, "read-tree", "HEAD"), env=env)
        if read_tree.returncode != 0:
            return ""
        if untracked:
            add_intent = run_command(
                git_command_for_worktree(worktree, "add", "-N", "--", *untracked),
                env=env,
            )
            if add_intent.returncode != 0:
                return ""
        diff = run_command(git_command_for_worktree(worktree, "diff", "--binary", "HEAD"), env=env)
        return diff.stdout if diff.returncode == 0 else ""


def _extract_partial_worker_diff_artifact(artifact_text: str) -> str:
    marker = "## git diff --binary"
    index = artifact_text.find(marker)
    if index < 0:
        return artifact_text.strip()
    return artifact_text[index + len(marker):].lstrip("\r\n")


def _apply_carry_forward_patch(worktree: Path, patch_path: Path, log_path: Path) -> bool:
    def append_log(message: str) -> None:
        previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        log_path.write_text(previous + message, encoding="utf-8")

    if not patch_path.is_file():
        append_log(f"ACA carry-forward patch was missing: {patch_path}\n")
        return False
    try:
        artifact_text = patch_path.read_text(encoding="utf-8")
    except OSError as exc:
        append_log(f"ACA carry-forward patch could not be read: {patch_path}: {exc}\n")
        return False
    diff_text = _extract_partial_worker_diff_artifact(artifact_text)
    if not diff_text.strip():
        append_log(f"ACA carry-forward patch contained no git diff: {patch_path}\n")
        return False
    apply_cmd = [
        "--work-tree=." if arg.startswith("--work-tree=") else arg
        for arg in git_command_for_worktree(worktree, "apply", "--3way", "--whitespace=nowarn")
    ]
    apply_proc = subprocess.run(
        apply_cmd,
        cwd=str(worktree),
        input=diff_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if apply_proc.returncode != 0:
        append_log(
            "ACA carry-forward patch did not apply cleanly: "
            f"{patch_path}: {apply_proc.stderr or apply_proc.stdout or 'git apply failed'}\n"
        )
        return False
    append_log(f"ACA applied carry-forward partial diff before retry: {patch_path}\n")
    return True


def _carry_forward_patch_failure_result(
    patch_path: Path,
    worker_id: str,
    subtask: dict[str, Any],
    log_path: Path,
) -> dict[str, Any]:
    message = (
        "ACA could not apply the preserved partial worker diff before retry. "
        "The patch artifact is missing, unreadable, mechanically invalid, or no longer applies cleanly."
    )
    return {
        "worker_id": worker_id,
        "subtask_id": str(subtask.get("id") or "").strip(),
        "status": "failed",
        "returncode": 1,
        "stdout": f"{message}\nPatch: {patch_path}",
        "failure_reason": "CARRY_FORWARD_PATCH_APPLY_FAILED",
        "blocker_kind": "carry_forward_patch_apply_failed",
        "recovery_action": (
            "Discard this preserved patch for the next repair attempt and plan a fresh narrow repair "
            "against the parent task target files."
        ),
        "log_path": str(log_path),
        "changed_files": [],
        "write_required": bool(subtask.get("write_required")),
        "verified_existing": False,
        "carry_forward_patch": str(patch_path),
    }


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
    corrupt_reason = _corrupt_repeated_source_diff_reason(worktree)
    if corrupt_reason:
        return _fail_corrupt_worker_diff(result, log_path, corrupt_reason)
    recovered = _preserve_partial_worker_diff(result, log_path, worktree, reason=reason)
    changed_summary = "\n".join(f"- {path}" for path in changed_files)
    message = (
        "\nACA preserved this partial worker diff because the Tandem engine stalled before a terminal response.\n"
        "The partial diff is not treated as a completed worker result; retry or block with this evidence.\n"
        f"Changed files:\n{changed_summary}\n"
    )
    log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
    recovered["stdout"] = f"{recovered.get('stdout') or ''}{message}"
    recovered["returncode"] = 1
    recovered["partial_diff_preserved_after_engine_stall"] = True
    recovered["changed_files"] = changed_files
    recovered.setdefault("warnings", []).append("engine_tool_loop_stalled_partial_diff")
    return recovered


def _corrupt_repeated_source_diff_reason(worktree: Path) -> str | None:
    try:
        diff_text = git_working_diff(worktree, max_chars=120000, max_file_chars=60000)
    except TypeError:
        diff_text = git_working_diff(worktree)
    except Exception:
        return None
    if not diff_text.strip():
        return None
    source_exts = (".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py")
    current_source_file = False
    current_target = ""
    counts: dict[str, int] = {}
    python_top_level_defs: dict[tuple[str, str], int] = {}
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            parts = raw_line.split()
            target = parts[-1][2:] if len(parts) >= 4 and parts[-1].startswith("b/") else ""
            current_target = target
            current_source_file = target.endswith(source_exts)
            continue
        if not current_source_file or raw_line.startswith("+++") or not raw_line.startswith("+"):
            continue
        if current_target.endswith(".py"):
            definition_match = re.match(r"^(async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b", raw_line[1:])
            if definition_match:
                definition = f"{definition_match.group(1)} {definition_match.group(2)}"
                key = (current_target, definition)
                python_top_level_defs[key] = python_top_level_defs.get(key, 0) + 1
                if python_top_level_defs[key] >= 2:
                    return f"duplicate top-level Python definition added in {current_target}: {definition}"
        line = raw_line[1:].strip()
        if len(line) < 24:
            continue
        if _repeat_line_is_common_source_syntax(line):
            continue
        counts[line] = counts.get(line, 0) + 1
        if counts[line] >= 5:
            preview = line[:120]
            return f"repeated added source line appears {counts[line]} times: {preview}"
    return None


def _repeat_line_is_common_source_syntax(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(("#[", "@", "use ", "import ", "from ")):
        return True
    if stripped in {"}", "};", "),"}:
        return True
    if "remove_dir_all" in stripped and stripped.endswith(".ok();"):
        return True
    if re.match(r"^let\s+\w+\s*=\s*\w*Test\w*::new\(", stripped):
        return True
    return False


def _self_referential_test_only_diff_reason(
    worktree: Path,
    subtask: dict[str, Any],
    changed_files: list[str],
) -> str | None:
    targets = _subtask_targets(subtask)
    declared_test_targets = [
        path
        for path in targets
        if "/tests/" in path or path.endswith(("_test.rs", ".test.ts", ".test.tsx", ".test.js"))
    ]
    if not declared_test_targets:
        return None
    if any(path in changed_files for path in declared_test_targets):
        return None
    task_text = " ".join(
        str(value or "")
        for value in (
            subtask.get("title"),
            subtask.get("goal"),
            " ".join(str(item or "") for item in (subtask.get("acceptance_criteria") or [])),
        )
    ).lower()
    if not any(marker in task_text for marker in ("regression", "coverage", "test", "readiness", "schema drift")):
        return None
    try:
        diff_text = git_working_diff(worktree, max_chars=120000, max_file_chars=60000)
    except TypeError:
        diff_text = git_working_diff(worktree)
    except Exception:
        return None
    added_lines = [line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    added_text = "\n".join(added_lines).lower()
    if not added_text:
        return None
    added_source_local_tests = "#[cfg(test)]" in added_text or "#[test]" in added_text or "mod tests" in added_text
    helper_or_oracle_terms = any(
        marker in added_text
        for marker in ("helper", "readiness", "schema_drift", "schema drift", "degraded", "regression")
    )
    if added_source_local_tests and helper_or_oracle_terms:
        return (
            "regression diff added source-local helper/test coverage without changing "
            f"the declared test target ({', '.join(declared_test_targets)}); "
            f"changed files: {', '.join(changed_files)}"
        )
    return None


def _fail_corrupt_worker_diff(
    result: dict[str, Any],
    log_path: Path,
    reason: str,
) -> dict[str, Any]:
    message = (
        "WORKER_CORRUPT_DIFF: Worker produced a target-touching diff, but ACA rejected it "
        f"because it appears mechanically corrupted ({reason}).\n"
    )
    log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
    result["stdout"] = f"{result.get('stdout') or ''}{message}"
    result["returncode"] = 1
    result["failure_reason"] = "WORKER_CORRUPT_DIFF"
    result["blocker_kind"] = "worker_corrupt_diff"
    result["recovery_action"] = (
        "Reset the worktree to a clean checkout before retrying; do not continue from this corrupted diff."
    )
    return result


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
        or "TERMINALIZED_WITH_REMAINING_BLOCKERS" in reason
        or "ENGINE_ERROR:" in text
        or "ENGINE_DISPATCH_FAILED" in text
        or "ITERATION BUDGET" in text
        or "TERMINALIZED_WITH_REMAINING_BLOCKERS" in text
        or blocker
        in {
            "engine_empty_response",
            "engine_exception",
            "engine_prompt_timeout",
            "engine_session_run_conflict",
            "engine_tool_loop_stalled",
            "engine_tool_loop_stalled_no_diff",
            "worker_incomplete_diff",
        }
        or "ENGINE_PROMPT_TIMEOUT" in text
        or "ENGINE_TOOL_LOOP_STALLED" in text
        or "ENGINE_SESSION_RUN_CONFLICT" in text
    )


def _engine_dispatch_failure_allows_diff_salvage(result: dict[str, Any], stdout_text: str) -> bool:
    reason = str(result.get("failure_reason") or "").upper()
    blocker = str(result.get("blocker_kind") or "").lower()
    text = f"{reason}\n{stdout_text}".upper()
    if any(marker in text for marker in ("API KEY", "AUTHORIZATION", "AUTHENTICATION")):
        return False
    return (
        blocker == "engine_dispatch_failed"
        or "ENGINE_DISPATCH_FAILED" in text
        or "ITERATION BUDGET" in text
    )


def _terminalized_note_reports_blockers(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return False
    hard_blocker_markers = (
        "placeholder",
        "not yet implement",
        "not yet implemented",
        "not implemented",
        "only partially implemented",
        "only partial",
        "partially implemented",
        "need to inspect",
        "needs source inspection",
        "source inspection is still required",
        "requested subtask also mentioned",
        "visible diff only",
        "diff only adds",
        "does not implement",
        "does not appear to add meaningful",
        "does not appear to add meaningful coverage",
        "does not appear to add meaningful github projects readiness drift regression coverage",
        "redundant test change",
        "no-op/redundant",
        "no-op diff",
        "effectively a no-op",
        "missing required",
        "missing coverage",
        "would not compile",
        "does not compile",
        "won't compile",
        "will not compile",
        "compile failure",
        "compile failures",
        "compilation failure",
        "type mismatch",
        "type mismatches",
        "unresolved name",
        "undefined name",
        "not in scope",
    )
    if any(marker in normalized for marker in hard_blocker_markers):
        return True
    no_blocker_markers = (
        "no blocker",
        "no blockers",
        "blocker: none",
        "blockers: none",
        "no remaining blocker",
        "no remaining blockers",
        "remaining blockers: none",
        "remaining blockers: - none",
        "remaining blockers: none visible",
        "remaining blockers: - none visible",
        "remaining implementation blockers: none",
        "remaining implementation blockers: - none",
        "remaining implementation blockers: none visible",
        "remaining implementation blockers: - none visible",
        "remaining implementation blockers: none visible from the diff",
        "remaining implementation blockers: - none visible from the diff",
        "remaining implementation blockers: none visible from the provided diff",
        "remaining implementation blockers: - none visible from the provided diff",
        "remaining implementation blockers: none visible from the provided diff excerpt",
        "remaining implementation blockers: - none visible from the provided diff excerpt",
        "remaining blockers: no actual test run visible",
        "remaining blockers: - no actual test run visible",
        "remaining blockers: no test run visible",
        "remaining blockers: - no test run visible",
        "remaining blockers: verification not run",
        "remaining blockers: - verification not run",
        "remaining blockers: no verification visible",
        "remaining blockers: - no verification visible",
        "remaining blockers - none",
        "remaining blockers: n/a",
    )
    if any(marker in normalized for marker in no_blocker_markers):
        return False
    blocker_markers = (
        "remaining blocker",
        "remaining blockers",
        "remaining implementation blocker",
        "remaining implementation blockers",
        "blocker:",
        "blockers:",
        "blocked by",
    )
    return any(marker in normalized for marker in blocker_markers)


def _worker_note_reports_blocked(text: str) -> bool:
    raw = str(text or "")
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").strip().lower()
            if status in {"blocked", "failed", "failure"}:
                return True
            if payload.get("approved") is False:
                return True
            blockers = payload.get("blockers")
            if isinstance(blockers, list) and any(str(item).strip() for item in blockers):
                return True
    return bool(WORKER_BLOCKED_STATUS_RE.search(raw))


def _terminalized_note_reports_no_visible_verification(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return False
    no_verification_markers = (
        "verification not run",
        "verification: not run",
        "verification was not run",
        "verification was not performed",
        "verification not performed",
        "no verification visible",
        "no test run visible",
        "no actual test run visible",
        "not verified",
    )
    return any(marker in normalized for marker in no_verification_markers)


def _changed_files_are_all_tests(changed_files: list[str]) -> bool:
    paths = [
        _normalize_target_path(path)
        for path in changed_files
        if path and not _is_aca_repo_artifact_path(path)
    ]
    return bool(paths) and all(_path_is_test_file(path) for path in paths)


def _worker_result_should_retry(result: dict[str, Any]) -> bool:
    if result.get("returncode") == 0:
        return False
    blocker_kind = str(result.get("blocker_kind") or "").strip()
    return blocker_kind not in NON_RETRYABLE_WORKER_BLOCKERS


def _run_has_terminal_status(layout: dict[str, Path]) -> bool:
    status_path = layout.get("status")
    if not isinstance(status_path, Path) or not status_path.is_file():
        return False
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    run = status.get("run") if isinstance(status, dict) else {}
    if not isinstance(run, dict):
        return False
    run_status = str(run.get("status") or "").strip().lower()
    return run_status in {"blocked", "completed"} or run.get("completed_at_ms") is not None


def _active_worker_attempts_path(layout: dict[str, Path]) -> Path | None:
    run_dir = layout.get("run_dir")
    if not isinstance(run_dir, Path):
        return None
    return run_dir / "active_worker_attempts.json"


def _active_worker_engine_sessions_path(run_dir: Path) -> Path:
    return run_dir / "active_worker_engine_sessions.json"


def _active_worker_engine_sessions_path_for_log(log_path: Path) -> Path | None:
    try:
        log_parent = log_path.parent
    except Exception:
        return None
    if log_parent.name == "logs":
        return _active_worker_engine_sessions_path(log_parent.parent)
    return _active_worker_engine_sessions_path(log_parent)


def _load_active_worker_engine_sessions(path: Path) -> dict[str, dict[str, Any]]:
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


def _write_active_worker_engine_sessions(path: Path, sessions: dict[str, dict[str, Any]]) -> None:
    cleaned: dict[str, dict[str, Any]] = {}
    for raw_worker_id, raw_info in sessions.items():
        worker_id = str(raw_worker_id or "").strip()
        session_id = str((raw_info or {}).get("session_id") or "").strip()
        if worker_id and session_id:
            cleaned[worker_id] = dict(raw_info)
            cleaned[worker_id]["session_id"] = session_id
    if cleaned:
        atomic_write_json(path, cleaned)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _mark_active_worker_engine_session(
    log_path: Path,
    *,
    worker_id: str,
    session_id: str,
    run_id: str = "",
    cwd: Path | None = None,
) -> None:
    worker_id = str(worker_id or "").strip()
    session_id = str(session_id or "").strip()
    if (worker_id != "manager" and not worker_id.startswith("worker-")) or not session_id:
        return
    path = _active_worker_engine_sessions_path_for_log(log_path)
    if path is None:
        return
    with WORKER_STATUS_LOCK:
        sessions = _load_active_worker_engine_sessions(path)
        current = dict(sessions.get(worker_id) or {})
        current.update(
            {
                "session_id": session_id,
                "run_id": str(run_id or current.get("run_id") or "").strip(),
                "log_path": str(log_path),
                "updated_at_ms": now_ms(),
            }
        )
        if cwd is not None:
            current["cwd"] = str(cwd)
        sessions[worker_id] = current
        _write_active_worker_engine_sessions(path, sessions)


def _clear_active_worker_engine_session(log_path: Path, worker_id: str, session_id: str) -> None:
    worker_id = str(worker_id or "").strip()
    session_id = str(session_id or "").strip()
    if (worker_id != "manager" and not worker_id.startswith("worker-")) or not session_id:
        return
    path = _active_worker_engine_sessions_path_for_log(log_path)
    if path is None:
        return
    with WORKER_STATUS_LOCK:
        sessions = _load_active_worker_engine_sessions(path)
        if str((sessions.get(worker_id) or {}).get("session_id") or "").strip() != session_id:
            return
        sessions.pop(worker_id, None)
        _write_active_worker_engine_sessions(path, sessions)


def _load_active_worker_attempts(layout: dict[str, Path]) -> dict[str, str]:
    path = _active_worker_attempts_path(layout)
    if not isinstance(path, Path) or not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items() if str(key).strip() and str(value).strip()}


def _write_active_worker_attempts(layout: dict[str, Path], attempts: dict[str, str]) -> None:
    path = _active_worker_attempts_path(layout)
    if not isinstance(path, Path):
        return
    if attempts:
        atomic_write_json(path, attempts)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _mark_active_worker_attempt(layout: dict[str, Path], worker_id: str, execution_id: str) -> None:
    worker_id = str(worker_id or "").strip()
    execution_id = str(execution_id or "").strip()
    if not worker_id or not execution_id:
        return
    with WORKER_STATUS_LOCK:
        attempts = _load_active_worker_attempts(layout)
        attempts[worker_id] = execution_id
        _write_active_worker_attempts(layout, attempts)


def _clear_active_worker_attempt(layout: dict[str, Path], worker_id: str, execution_id: str) -> None:
    worker_id = str(worker_id or "").strip()
    execution_id = str(execution_id or "").strip()
    if not worker_id or not execution_id:
        return
    with WORKER_STATUS_LOCK:
        attempts = _load_active_worker_attempts(layout)
        if attempts.get(worker_id) != execution_id:
            return
        attempts.pop(worker_id, None)
        _write_active_worker_attempts(layout, attempts)


def _worker_event_attempt_is_current(layout: dict[str, Path], payload: dict[str, Any]) -> bool:
    execution_id = str(payload.get("execution_id") or "").strip()
    worker_id = str(payload.get("worker_id") or "").strip()
    if not execution_id or not worker_id:
        return True
    attempts = _load_active_worker_attempts(layout)
    return str(attempts.get(worker_id) or "").strip() == execution_id


def _append_worker_event_if_run_active(
    layout: dict[str, Path],
    log_path: Path,
    event_type: str,
    run_id: str,
    payload: dict[str, Any],
    *,
    task_id: str | None = None,
    role: str | None = None,
    repo: dict[str, Any] | None = None,
) -> bool:
    if _run_has_terminal_status(layout):
        previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        log_path.write_text(
            previous
            + f"\nACA suppressed late worker event `{event_type}` because run {run_id} is already terminal.\n",
            encoding="utf-8",
        )
        return False
    if not _worker_event_attempt_is_current(layout, payload):
        previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        log_path.write_text(
            previous
            + f"\nACA suppressed stale worker event `{event_type}` from inactive execution "
            + f"{payload.get('execution_id')}.\n",
            encoding="utf-8",
        )
        return False
    append_event(
        layout["events"],
        event_type,
        run_id,
        payload,
        task_id=task_id,
        role=role,
        repo=repo,
    )
    return True


def _late_terminal_worker_result(
    result: dict[str, Any],
    *,
    log_path: Path,
    write_required: bool,
) -> dict[str, Any]:
    previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.write_text(
        previous
        + "\nACA ignored this worker result because the run was already terminal before the worker returned.\n",
        encoding="utf-8",
    )
    stale = dict(result)
    stale["returncode"] = 1
    stale["status"] = "failed"
    stale["failure_reason"] = "WORKER_RESULT_AFTER_RUN_TERMINAL"
    stale["blocker_kind"] = "stale_worker_result"
    stale["recovery_action"] = "Inspect the terminal run blocker; this late worker result was intentionally ignored."
    stale["log_path"] = str(log_path)
    stale["write_required"] = write_required
    stale.setdefault("stdout", "")
    return stale


def _inactive_worker_attempt_result(
    result: dict[str, Any],
    *,
    log_path: Path,
    write_required: bool,
    execution_id: str,
) -> dict[str, Any]:
    previous = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.write_text(
        previous
        + "\nACA ignored this worker result because a newer retry execution superseded it before sync.\n",
        encoding="utf-8",
    )
    stale = dict(result)
    stale["returncode"] = 1
    stale["status"] = "failed"
    stale["failure_reason"] = "WORKER_RESULT_AFTER_RETRY_SUPERSEDED"
    stale["blocker_kind"] = "stale_worker_result"
    stale["recovery_action"] = "Inspect the active retry result; this abandoned worker result was intentionally ignored."
    stale["log_path"] = str(log_path)
    stale["write_required"] = write_required
    stale["execution_id"] = str(execution_id or "").strip()
    stale.setdefault("stdout", "")
    return stale


def _subtask_requires_real_diff(subtask: dict[str, Any]) -> bool:
    if subtask.get("pr_candidate_context") or subtask.get("pr_candidate_refs"):
        return True
    if subtask.get("repair_verification_first"):
        return False
    if not _subtask_targets(subtask):
        return False
    text = _subtask_search_text(subtask)
    diff_markers = (
        " add ",
        " extend",
        " implement",
        " create",
        " update",
        " fix ",
        " coverage",
        " assertion",
        " assertions",
        " test ",
        " tests ",
        " documented command",
    )
    padded = f" {text} "
    return any(marker in padded for marker in diff_markers)


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
    if not _changed_files_have_substantive_content(worktree, changed_files):
        return result
    stdout_text = str(result.get("stdout") or "")
    reason_text = str(result.get("failure_reason") or reason or "").upper()
    blocker_kind = str(result.get("blocker_kind") or "").lower()
    if (
        "ENGINE_TOOL_LOOP_STALLED" in reason_text
        or "ENGINE_PROMPT_TIMEOUT" in reason_text
        or blocker_kind == "engine_tool_loop_stalled"
        or blocker_kind == "engine_prompt_timeout"
        or "ENGINE_TOOL_LOOP_STALLED" in stdout_text.upper()
        or "ENGINE_PROMPT_TIMEOUT" in stdout_text.upper()
    ):
        return _recover_tool_stall_with_diff(result, log_path, worktree, reason=reason)
    if _engine_dispatch_failure_allows_diff_salvage(result, stdout_text):
        message = (
            "Worker engine dispatch failed before a terminal response, but ACA found a substantive "
            "target-file diff. Accepting the worker diff for normal integration review and verification.\n"
        )
        log_path.write_text(log_path.read_text(encoding="utf-8") + message, encoding="utf-8")
        result["stdout"] = f"{stdout_text}{message}"
        result["returncode"] = 0
        result["recovered_success"] = True
        result["recovered_from_engine_dispatch_partial_diff"] = True
        result["recovered_failure_reason"] = result.get("failure_reason") or reason
        result.setdefault("warnings", []).append("engine_dispatch_failed_partial_diff_salvaged")
        result.pop("failure_reason", None)
        result.pop("blocker_kind", None)
        result.pop("recovery_action", None)
        result["changed_files"] = changed_files
        result["diff_stat"] = diff_text
        return result
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


def _recover_nonzero_result_if_diff_satisfies_subtask(
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    subtask: dict[str, Any],
    *,
    require_filesystem_changes: bool,
    reason: str,
) -> dict[str, Any]:
    if result.get("returncode", 0) == 0:
        return result
    diff_text = git_diff_stat(worktree).strip()
    if not diff_text:
        return result
    requires_real_diff = require_filesystem_changes or _subtask_requires_real_diff(subtask)
    if not _diff_satisfies_targeted_subtask(
        subtask,
        requires_real_diff=requires_real_diff,
        targets_exist=_target_files_exist(worktree, subtask),
        diff_touches_targets=_diff_touches_target_files(worktree, subtask),
        diff_touches_substantive_targets=_diff_touches_substantive_target_files(worktree, subtask),
    ) and not _diff_touches_nearby_test_files(worktree, subtask):
        return result
    return _recover_nonzero_result_with_diff(result, log_path, worktree, reason=reason)


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


def _result_is_terminalizable_engine_stall(result: dict[str, Any]) -> bool:
    reason = str(result.get("failure_reason") or "").upper()
    blocker = str(result.get("blocker_kind") or "").lower()
    stdout = str(result.get("stdout") or "").upper()
    return (
        "ENGINE_TOOL_LOOP_STALLED" in reason
        or "ENGINE_PROMPT_TIMEOUT" in reason
        or blocker == "engine_tool_loop_stalled"
        or blocker == "engine_prompt_timeout"
        or "ENGINE_TOOL_LOOP_STALLED" in stdout
        or "ENGINE_PROMPT_TIMEOUT" in stdout
    )


def _terminalize_worker_after_tool_loop(
    cfg: ResolvedConfig,
    result: dict[str, Any],
    log_path: Path,
    worktree: Path,
    subtask: dict[str, Any],
    *,
    role: str,
    provider: str,
    model: str,
    require_filesystem_changes: bool,
) -> dict[str, Any]:
    if result.get("returncode", 0) == 0 or not _result_is_terminalizable_engine_stall(result):
        return result
    engine_meta = result.get("engine") if isinstance(result.get("engine"), dict) else {}
    if engine_meta.get("partial_diff_recovery_deferred"):
        return result
    changed_files = _worktree_changed_files(worktree)
    if not changed_files:
        return result
    if not _changed_files_have_substantive_content(worktree, changed_files):
        return result
    if _corrupt_repeated_source_diff_reason(worktree):
        return result
    requires_real_diff = require_filesystem_changes or _subtask_requires_real_diff(subtask)
    if not _diff_satisfies_targeted_subtask(
        subtask,
        requires_real_diff=requires_real_diff,
        targets_exist=_target_files_exist(worktree, subtask),
        diff_touches_targets=_diff_touches_target_files(worktree, subtask),
        diff_touches_substantive_targets=_diff_touches_substantive_target_files(worktree, subtask),
    ) and not _diff_touches_nearby_test_files(worktree, subtask):
        return result
    try:
        diff_stat = git_diff_stat(worktree).strip()
        diff_excerpt = git_working_diff(worktree, max_chars=32000, max_file_chars=16000).strip()
    except TypeError:
        diff_excerpt = git_working_diff(worktree).strip()
        diff_stat = "\n".join(changed_files)
    except Exception:
        logger.debug("Failed to capture terminalization diff", exc_info=True)
        return result
    if not diff_excerpt:
        return result

    prompt = (
        "ACA detected that the previous worker run edited files but never produced a terminal assistant response.\n"
        "Do not call tools. Do not make more edits. Use only the diff excerpt below.\n"
        "Return a concise worker completion note with:\n"
        "- changed files\n"
        "- what the diff appears to implement\n"
        "- verification that was actually performed, or `verification not run` if none is visible\n"
        "- any remaining implementation blockers\n\n"
        "Do not list missing verification as a remaining blocker by itself; ACA runs a separate review/verification phase. "
        "Do list incomplete implementation, placeholder tests/files, missing required coverage, or unsafe/no-op diffs as blockers.\n\n"
        f"Subtask: {subtask.get('title') or subtask.get('id') or role}\n"
        f"Changed files:\n{chr(10).join(f'- {path}' for path in changed_files)}\n\n"
        f"Diff stat:\n{diff_stat}\n\n"
        f"Diff excerpt:\n```diff\n{diff_excerpt}\n```\n"
    )
    terminal_session_id = ""
    terminal_meta: dict[str, Any] = {}
    try:
        session_temperature = None
        if hasattr(cfg, "sampling_for_role"):
            session_temperature = cfg.sampling_for_role(role).get("temperature")
        terminal_session_id = create_tandem_session(
            cfg,
            title=f"ACA {role} terminalize",
            directory=worktree,
            provider=provider,
            model=model,
            temperature=session_temperature,
            permission_rules=None,
        )
        terminal_meta["session_id"] = terminal_session_id
        with log_path.open("a", encoding="utf-8") as terminal_log:
            terminal_response = _prompt_sync_with_connect_retries(
                cfg,
                engine_meta=terminal_meta,
                log=terminal_log,
                role=role,
                session_id=terminal_session_id,
                prompt=prompt,
                tool_allowlist=[],
                tool_mode="none",
                require_tool_use=False,
                write_required=False,
                timeout_seconds=_worker_terminalize_timeout_seconds(cfg),
            )
    except Exception as exc:
        terminal_meta["error"] = str(exc)
        engine_meta = dict(result.get("engine") or {})
        engine_meta["terminalize"] = terminal_meta
        result["engine"] = engine_meta
        logger.debug("Failed to terminalize worker tool-loop result", exc_info=True)
        return result
    text = _extract_prompt_sync_text(terminal_response)
    engine_meta = dict(result.get("engine") or {})
    terminal_meta["snapshot_path"] = _write_engine_snapshot(
        log_path,
        f"engine-terminalize-{terminal_session_id or 'unknown'}",
        terminal_response,
    )
    terminal_meta["recovered_text"] = bool(text.strip())
    engine_meta["terminalize"] = terminal_meta
    result["engine"] = engine_meta
    if not text.strip():
        return result

    note = (
        "\nENGINE_TOOL_LOOP_TERMINALIZED: ACA recovered a terminal worker note from the preserved diff "
        "using a no-tools prompt.\n"
        f"{text.strip()}\n"
    )
    with log_path.open("a", encoding="utf-8") as log:
        log.write(note)
    if _terminalized_note_reports_blockers(text):
        result["stdout"] = f"{result.get('stdout') or ''}{note}"
        result["failure_reason"] = "TERMINALIZED_WITH_REMAINING_BLOCKERS"
        result["blocker_kind"] = "worker_incomplete_diff"
        result["recovery_action"] = (
            "Inspect the preserved worker diff and rerun with a narrower repair prompt for the unmet acceptance criteria."
        )
        result["changed_files"] = changed_files
        result["engine"] = engine_meta
        return result
    if _terminalized_note_reports_no_visible_verification(text) and _changed_files_are_all_tests(changed_files):
        incomplete = _preserve_partial_worker_diff(
            dict(result),
            log_path,
            worktree,
            reason="TERMINALIZED_UNVERIFIED_TEST_ONLY_DIFF",
        )
        rejection_note = (
            "\nACA rejected the terminalized worker note because the recovered diff only changes tests "
            "and the note reports that verification was not run. The partial diff remains available "
            "for a narrower repair attempt.\n"
        )
        incomplete["stdout"] = f"{result.get('stdout') or ''}{note}{rejection_note}"
        incomplete["failure_reason"] = "TERMINALIZED_UNVERIFIED_TEST_ONLY_DIFF"
        incomplete["blocker_kind"] = "worker_incomplete_diff"
        incomplete["recovery_action"] = (
            "Rerun with a narrower prompt that either implements the required production behavior "
            "or verifies the test-only regression diff before completion."
        )
        incomplete["changed_files"] = changed_files
        incomplete["engine"] = engine_meta
        return incomplete
    recovered = dict(result)
    recovered["returncode"] = 0
    recovered["stdout"] = note
    recovered["changed_files"] = changed_files
    recovered["terminalized_after_tool_loop"] = True
    recovered["engine"] = engine_meta
    recovered.pop("failure_reason", None)
    recovered.pop("blocker_kind", None)
    recovered.pop("recovery_action", None)
    return recovered


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
            "Do not create marker files, status files, temporary files, scratch notes, or placeholder files to satisfy write-required mode.",
            "For private helper coverage, add real tests inside the source module that defines the helper; do not add placeholder integration or contract test files.",
            "When adding tests, prefer additive test modules or additive cases; do not rewrite existing tests unless the task explicitly requires changing them.",
            "Missing test coverage or missing behavior is not a blocker; implement the smallest safe improvement in one of the target files.",
            "Once a substantive diff exists, stop expanding scope; run one lightweight verification or file readback, retry a narrower readback if a tool is skipped, then return the final completion note.",
            "For Python sibling test files under `src/`, prefer `python3 -m unittest <module.path>`; use `python3 -m py_compile <changed files>` as a fallback if dependencies needed by the test command are unavailable.",
            "Do not treat missing `pytest` as a blocker when an equivalent `python3 -m unittest ...` command can exercise the changed Python test module.",
            "Finish by verifying the changed files with `ls -la`, `read`, `grep`, or the narrowest relevant test command.",
        ]
    )
    if has_pr_context:
        steps.append(
            "Do not reply with a summary until you have either produced a filesystem diff or produced a structured no-safe-changes blocker naming every inspected PR."
        )
    else:
        steps.append(
            "Do not reply with `changed_files: []` or a no-safe-changes blocker unless editing every listed target would be unsafe; name that concrete safety reason if so."
        )
        steps.append("Do not claim tool access is disallowed; this retry is sent with repository tool access required.")
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
    diff_touches_substantive_targets = _diff_touches_substantive_target_files(worktree, subtask)
    changed_files = _worktree_changed_files(worktree)
    substantive_changes = _changed_files_have_substantive_content(worktree, changed_files) if changed_files else False
    ignored_existing_targets = _ignored_existing_target_files(worktree, subtask)
    corrupt_reason = _corrupt_repeated_source_diff_reason(worktree) if diff_text and changed_files else None
    if corrupt_reason:
        return _fail_corrupt_worker_diff(result, log_path, corrupt_reason)
    self_referential_reason = (
        _self_referential_test_only_diff_reason(worktree, subtask, changed_files)
        if diff_text and changed_files
        else None
    )
    if self_referential_reason and result.get("returncode", 0) != 0:
        result = _preserve_partial_worker_diff(result, log_path, worktree, reason="SELF_REFERENTIAL_TEST_ONLY_DIFF")
    elif self_referential_reason and result.get("returncode", 0) == 0:
        result["failure_reason"] = "SELF_REFERENTIAL_TEST_ONLY_DIFF"
    if result.get("returncode", 0) != 0 and (not targets_exist or not diff_text):
        recovered, recovered_diff = _wait_for_target_files(worktree, subtask)
        if recovered:
            targets_exist = True
            diff_text = recovered_diff
            readable_targets = _readable_target_files(worktree, subtask)
            diff_touches_targets = _diff_touches_target_files(worktree, subtask)
            diff_touches_substantive_targets = _diff_touches_substantive_target_files(worktree, subtask)
    diff_satisfies_subtask = _diff_satisfies_targeted_subtask(
        subtask,
        requires_real_diff=requires_real_diff,
        targets_exist=targets_exist,
        diff_touches_targets=diff_touches_targets,
        diff_touches_substantive_targets=diff_touches_substantive_targets,
    ) or _diff_touches_nearby_test_files(worktree, subtask)
    if result.get("returncode", 0) != 0 and diff_text and diff_satisfies_subtask:
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
    if result.get("returncode", 0) == 0 and _worker_note_reports_blocked(stdout_text):
        failure_reason = failure_reason or "WORKER_REPORTED_BLOCKER"
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
    if (
        result.get("returncode", 0) != 0
        and diff_text
        and changed_files
        and requires_real_diff
        and _substantive_target_files(subtask)
        and _support_only_changed_files_for_subtask(subtask, changed_files)
        and not diff_satisfies_subtask
    ):
        if failure_reason:
            result["engine_failure_reason"] = failure_reason
        failure_reason = "TARGET_FILES_UNCHANGED"
    if result.get("returncode", 0) == 0 and requires_real_diff and (not changed_files or not substantive_changes):
        failure_reason = failure_reason or "NO_FILESYSTEM_CHANGES"
    elif result.get("returncode", 0) == 0 and not diff_text:
        failure_reason = failure_reason or "NO_FILESYSTEM_CHANGES"
    if (
        failure_reason in {"ENGINE_TOOL_LOOP_STALLED", "NO_FILESYSTEM_CHANGES"}
        and not diff_text
        and ignored_existing_targets
    ):
        result["ignored_files"] = ignored_existing_targets
        failure_reason = "IGNORED_PATH_CHANGES"
    if (
        failure_reason == "ENGINE_TOOL_LOOP_STALLED"
        and not diff_text
        and not ignored_existing_targets
    ):
        failure_reason = "ENGINE_TOOL_LOOP_STALLED_NO_DIFF"
    if (
        not failure_reason
        and result.get("returncode", 0) == 0
        and requires_real_diff
        and _subtask_targets(subtask)
        and changed_files
        and not diff_satisfies_subtask
    ):
        failure_reason = "TARGET_FILES_UNCHANGED"
    if not failure_reason:
        return result
    message = ""
    if failure_reason == "NO_FILESYSTEM_CHANGES":
        message = "Worker reported success but produced no filesystem changes in its worktree.\n"
    elif failure_reason == "TARGET_FILES_UNCHANGED":
        targets = ", ".join(f"`{path}`" for path in _subtask_targets(subtask))
        substantive_targets = ", ".join(f"`{path}`" for path in _substantive_target_files(subtask))
        changes = ", ".join(f"`{path}`" for path in changed_files)
        if _subtask_requires_substantive_target_diff(
            subtask,
            requires_real_diff=requires_real_diff,
        ):
            message = (
                "Worker produced repository changes, but none touched the substantive target files "
                "required for this test or fixture task. "
                f"Substantive targets: {substantive_targets}. Declared targets: {targets}. "
                f"Changed files: {changes}.\n"
            )
        else:
            message = (
                "Worker produced repository changes, but none touched the declared target files. "
                f"Targets: {targets}. Changed files: {changes}.\n"
            )
    elif failure_reason == "IGNORED_PATH_CHANGES":
        paths = ", ".join(f"`{path}`" for path in ignored_existing_targets)
        message = (
            "Worker edited only Git-ignored target files, so the run produced no reviewable repository diff. "
            f"Ignored files: {paths}.\n"
        )
    elif failure_reason == "SELF_REFERENTIAL_TEST_ONLY_DIFF":
        message = (
            "Worker produced a self-referential regression diff: it added source-local helper/test coverage "
            "without updating the declared test target or exercising the existing production path. "
            f"{self_referential_reason or ''}\n"
        )
    elif failure_reason not in stdout_text:
        message = f"Worker failed: {failure_reason}\n"
    if failure_reason == "WORKER_REPORTED_BLOCKER":
        message = "Worker final note reported a blocker, so ACA is treating the worker as failed.\n"
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
    elif failure_reason == "ENGINE_TOOL_LOOP_STALLED_NO_DIFF":
        result["blocker_kind"] = "engine_tool_loop_stalled_no_diff"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Inspect the engine/session messages and tool permissions; the engine made tool calls but produced no tracked diff."
            )
    elif failure_reason == "NO_FILESYSTEM_CHANGES":
        result["blocker_kind"] = "worker_no_diff"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Inspect the worker log and PR candidate context; reset the task to Backlog if another attempt is needed."
            )
    elif failure_reason == "TARGET_FILES_UNCHANGED":
        result["blocker_kind"] = "target_files_unchanged"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Retry with a narrower worker prompt that edits one of the declared target files, "
                "or update the task target files if the current targets are wrong."
            )
    elif failure_reason == "IGNORED_PATH_CHANGES":
        result["blocker_kind"] = "ignored_path_changes"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Move the requested deliverable to tracked repository files, or update the task to name tracked target files, then reset it to Backlog."
            )
    elif failure_reason == "SELF_REFERENTIAL_TEST_ONLY_DIFF":
        result["blocker_kind"] = "worker_incomplete_diff"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Reset this worker diff and retry with coverage that updates the declared test target "
                "and exercises existing production behavior."
            )
    elif failure_reason == "WORKER_REPORTED_BLOCKER":
        result["blocker_kind"] = "worker_incomplete_diff" if diff_text else "worker_reported_blocker"
        if not result.get("recovery_action"):
            result["recovery_action"] = (
                "Inspect the worker note and preserved diff, then retry with enough tool budget to verify the changed files."
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
    if diff_text and failure_reason in {"ENGINE_TOOL_LOOP_STALLED", "ENGINE_PROMPT_TIMEOUT"} and diff_satisfies_subtask:
        return _recover_tool_stall_with_diff(result, log_path, worktree, reason=failure_reason)
    if diff_text and failure_reason == "WORKER_REPORTED_BLOCKER":
        return _preserve_partial_worker_diff(result, log_path, worktree, reason=failure_reason)
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
    prompt_sync_first: bool | None = None,
    timeout_multiplier: float = 1.0,
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
                _mark_active_worker_engine_session(
                    log_path,
                    worker_id=role,
                    session_id=session_id,
                    cwd=cwd,
                )
                timeout_multiplier = max(1.0, float(timeout_multiplier or 1.0))
                prompt_sync_timeout = _scaled_prompt_sync_timeout_seconds(
                    cfg,
                    role,
                    write_required,
                    timeout_multiplier,
                )
                async_prompt_timeout = _scaled_async_prompt_timeout_seconds(
                    cfg,
                    role,
                    write_required,
                    timeout_multiplier,
                )
                async_no_text_timeout = _scaled_async_no_text_timeout_seconds(
                    cfg,
                    role,
                    write_required,
                    timeout_multiplier,
                )
                async_dispatch_timeout = min(
                    _engine_async_dispatch_timeout_seconds(cfg),
                    async_prompt_timeout,
                )
                if timeout_multiplier > 1.0:
                    engine_meta["timeout_multiplier"] = timeout_multiplier
                    engine_meta["timeouts"] = {
                        "prompt_sync_seconds": prompt_sync_timeout,
                        "async_prompt_seconds": async_prompt_timeout,
                        "async_no_text_seconds": async_no_text_timeout,
                        "async_dispatch_seconds": async_dispatch_timeout,
                    }

                def _writer(delta: str) -> None:
                    for line in delta.splitlines(keepends=True):
                        log.write(line)
                        log.flush()
                        _print_line(role, line)

                if role.startswith("worker") and write_required and _use_prompt_sync_first(cfg, prompt_sync_first):
                    engine_meta["fallback_mode"] = "prompt_sync_first"
                    failure_reason = ""
                    blocker_kind = ""
                    recovery_action = ""
                    completed = False
                    stdout_text = ""
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
                            timeout_seconds=prompt_sync_timeout,
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
                            log.write(stdout_text)
                            log.flush()
                            _print_line(role, stdout_text)
                            sync_deadline = time.monotonic() + _engine_sync_conflict_wait_seconds(cfg)
                            recovered_tool_activity = False
                            while time.monotonic() < sync_deadline:
                                recovered_text, recovery = _recover_engine_text_from_state(
                                    cfg,
                                    session_id=session_id,
                                    run_id=conflict_run_id or last_run_id,
                                    log_path=log_path,
                                )
                                recovery["run_id"] = conflict_run_id or last_run_id
                                recovery["attempt"] = engine_meta.get("retry_count", 0)
                                recovery["stream_reason"] = "prompt_sync_first_session_run_conflict"
                                engine_meta.setdefault("recovery", []).append(recovery)
                                for key in ("events_path", "messages_path"):
                                    if recovery.get(key):
                                        engine_meta[key] = recovery[key]
                                recovered_tool_activity = recovered_tool_activity or int(recovery.get("tool_part_count") or 0) > 0
                                if recovered_text.strip():
                                    stdout_text = recovered_text
                                    failure_reason = ""
                                    blocker_kind = ""
                                    completed = True
                                    break
                                time.sleep(min(max(retry_after_ms / 1000.0, 0.25), 2.0))
                            if not completed and recovered_tool_activity:
                                failure_reason = "ENGINE_TOOL_LOOP_STALLED"
                                blocker_kind = "engine_tool_loop_stalled"
                                stdout_text = (
                                    "ENGINE_TOOL_LOOP_STALLED: Tandem engine completed tool activity during "
                                    "prompt_sync recovery but produced no assistant terminal response. "
                                    f"Last engine run: {conflict_run_id or last_run_id or 'unknown'}.\n"
                                )
                        elif _engine_exception_is_timeout(exc):
                            failure_reason = "ENGINE_PROMPT_TIMEOUT"
                            blocker_kind = "engine_prompt_timeout"
                            stdout_text = (
                                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt "
                                f"did not finish within {prompt_sync_timeout:.0f}s.\n"
                            )
                        elif _engine_exception_is_connection_failure(exc):
                            stdout_text = (
                                "ENGINE_PROMPT_SYNC_CONNECT_LOST: Tandem engine prompt_sync connection was "
                                "lost after dispatch; checking the session for recovered output.\n"
                            )
                            log.write(stdout_text)
                            log.flush()
                            _print_line(role, stdout_text)
                            recovery_deadline = time.monotonic() + _engine_sync_conflict_wait_seconds(cfg)
                            recovered_tool_activity = False
                            while time.monotonic() < recovery_deadline:
                                recovered_text, recovery = _recover_engine_text_from_state(
                                    cfg,
                                    session_id=session_id,
                                    run_id=last_run_id,
                                    log_path=log_path,
                                )
                                recovery["run_id"] = last_run_id
                                recovery["attempt"] = engine_meta.get("retry_count", 0)
                                recovery["stream_reason"] = "prompt_sync_first_connection_lost"
                                engine_meta.setdefault("recovery", []).append(recovery)
                                for key in ("events_path", "messages_path"):
                                    if recovery.get(key):
                                        engine_meta[key] = recovery[key]
                                recovered_tool_activity = recovered_tool_activity or int(recovery.get("tool_part_count") or 0) > 0
                                if recovered_text.strip():
                                    stdout_text = recovered_text
                                    completed = True
                                    break
                                time.sleep(0.5)
                            if not completed and recovered_tool_activity:
                                failure_reason = "ENGINE_TOOL_LOOP_STALLED"
                                blocker_kind = "engine_tool_loop_stalled"
                                stdout_text = (
                                    "ENGINE_TOOL_LOOP_STALLED: Tandem engine completed tool activity after "
                                    "prompt_sync dispatch but produced no assistant terminal response.\n"
                                )
                            elif not completed:
                                failure_reason = "ENGINE_WORKSPACE_UNREACHABLE"
                                blocker_kind = "engine_workspace_unreachable"
                                stdout_text = (
                                    "ENGINE_WORKSPACE_UNREACHABLE: Tandem engine prompt_sync worker prompt "
                                    "lost the connection and no session output could be recovered.\n"
                                )
                        else:
                            raise
                    completed = completed or (bool(stdout_text.strip()) and not failure_reason)
                    if not completed and not failure_reason:
                        failure_reason = "ENGINE_EMPTY_RESPONSE"
                        blocker_kind = "engine_empty_response"
                        stdout_text = (
                            "ENGINE_EMPTY_RESPONSE: Tandem engine prompt_sync worker prompt "
                            "finished without assistant transcript text.\n"
                        )
                    if not completed and session_id:
                        recovered_text, recovery = _recover_engine_text_from_state(
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
                        if recovered_text.strip():
                            stdout_text = recovered_text
                            completed = True
                            failure_reason = ""
                            blocker_kind = ""
                            recovery_action = ""
                    if stdout_text and not stdout_text.endswith("\n"):
                        stdout_text += "\n"
                    if stdout_text:
                        log.write(stdout_text)
                        log.flush()
                        _print_line(role, stdout_text)
                    recover_with_async = (
                        not completed
                        and blocker_kind in {
                            "engine_prompt_timeout",
                            "engine_empty_response",
                        }
                    )
                    if recover_with_async and _skip_tool_recovery_when_partial_diff_exists(
                        role=role,
                        write_required=write_required,
                        cwd=cwd,
                        blocker_kind=blocker_kind,
                    ):
                        recover_with_async = False
                        engine_meta["partial_diff_recovery_deferred"] = True
                        recovery_action = (
                            "ACA preserved the partial worker diff instead of launching overlapping "
                            "tool-capable recovery on the same worktree."
                        )
                    if recover_with_async:
                        previous_session_id = session_id
                        engine_meta["prompt_sync_first_session_id"] = previous_session_id
                        recovery_notice = (
                            "ENGINE_PROMPT_SYNC_ASYNC_RECOVERY: prompt_sync produced tool activity or timed out "
                            "without assistant output; retrying this worker once through async streaming in a fresh session.\n"
                        )
                        log.write(recovery_notice)
                        log.flush()
                        _print_line(role, recovery_notice)
                        if previous_session_id:
                            try:
                                _call_with_timeout(
                                    lambda: delete_tandem_session(cfg, previous_session_id),
                                    timeout_seconds=5.0,
                                )
                            except Exception:
                                logger.debug("Failed to delete prompt_sync-first session before async recovery", exc_info=True)
                        session_temperature = None
                        if hasattr(cfg, "sampling_for_role"):
                            session_temperature = cfg.sampling_for_role(role).get("temperature")
                        session_id = create_tandem_session(
                            cfg,
                            title=f"ACA {role} async recovery",
                            directory=cwd,
                            provider=provider,
                            model=model,
                            temperature=session_temperature,
                            permission_rules=session_permission_rules,
                        )
                        engine_meta["session_id"] = session_id
                        engine_meta["fallback_mode"] = "prompt_sync_first_async_recovery"
                        engine_meta["run_id"] = ""
                        last_run_id = ""
                        _mark_active_worker_engine_session(
                            log_path,
                            worker_id=role,
                            session_id=session_id,
                            cwd=cwd,
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
                    if not recover_with_async:
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
                    try:
                        async_result = _call_with_timeout(
                            lambda: sdk_sessions_prompt_async(
                                cfg,
                                session_id=session_id,
                                prompt=prompt_text,
                                tool_mode=prompt_tool_mode,
                                tool_allowlist=session_tool_allowlist,
                                context_mode=None,
                                write_required=write_required,
                            ),
                            timeout_seconds=async_dispatch_timeout,
                        )
                    except TimeoutError:
                        engine_meta["retry_count"] = attempt
                        engine_meta["stream_reason"] = "dispatch_timeout"
                        engine_meta.setdefault("recovery", []).append(
                            {
                                "attempt": attempt,
                                "stream_reason": "dispatch_timeout",
                                "timeout_seconds": async_dispatch_timeout,
                            }
                        )
                        return "", False, "", "dispatch_timeout"
                    except Exception as exc:
                        conflict = _engine_session_run_conflict(exc)
                        if conflict:
                            conflict_run_id, retry_after_ms = conflict
                            engine_meta["retry_count"] = attempt
                            engine_meta["run_id"] = conflict_run_id
                            engine_meta["sync_conflict"] = {
                                "run_id": conflict_run_id,
                                "retry_after_ms": retry_after_ms,
                            }
                            return "", False, conflict_run_id, "session_run_conflict"
                        raise
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
                    _mark_active_worker_engine_session(
                        log_path,
                        worker_id=role,
                        session_id=session_id,
                        run_id=run_id,
                        cwd=cwd,
                    )
                    stream_result = (
                        sdk_stream_run_text(
                            cfg,
                            session_id,
                            run_id,
                            _writer,
                            timeout_seconds=async_prompt_timeout,
                            no_text_timeout_seconds=async_no_text_timeout,
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
                                timeout_seconds=prompt_sync_timeout,
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
                elif stream_reason in {"timeout", "dispatch_timeout"}:
                    failure_reason = "ENGINE_PROMPT_TIMEOUT"
                    blocker_kind = "engine_prompt_timeout"
                    if stream_reason == "dispatch_timeout":
                        stdout_text = (
                            "ENGINE_PROMPT_TIMEOUT: Tandem engine async prompt dispatch did not return "
                            f"a run id within {async_dispatch_timeout:.1f}s. Last engine run: "
                            f"{run_id or last_run_id or 'unknown'}.\n"
                        )
                    else:
                        stdout_text = (
                            "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt did not produce a terminal response "
                            f"within {async_prompt_timeout:.0f}s. Last engine run: "
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
                if not completed and session_id:
                    recovered_text, recovery = _recover_engine_text_from_state(
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
                    if recovered_text.strip():
                        stdout_text = recovered_text
                        completed = True
                        failure_reason = ""
                        blocker_kind = ""
                        recovery_action = ""
                        if stdout_text and not stdout_text.endswith("\n"):
                            stdout_text += "\n"
                if stdout_text:
                    log.write(stdout_text)
                    log.flush()
                    _print_line(role, stdout_text)
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
                    _clear_active_worker_engine_session(log_path, role, session_id)
                    try:
                        _call_with_timeout(lambda: delete_tandem_session(cfg, session_id), timeout_seconds=5.0)
                    except TimeoutError:
                        logger.debug("Timed out deleting tandem session %s", session_id)
                    except Exception:
                        logger.debug("Failed to delete tandem session", exc_info=True)
    return {"role": role, "returncode": 1, "stdout": "Internal Error: session-less stream requested"}


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
        "artifacts": dict(result.get("artifacts") or {}),
        "partial_diff_artifact": str(result.get("partial_diff_artifact") or ""),
        "write_required": bool(result.get("write_required", True)),
        "verified_existing": bool(result.get("verified_existing")),
        "changed_files": [path for path in changed_files if path],
        "diff_stat": diff_stat,
        **_subtask_retry_metadata(subtask),
    }


def _partial_diff_payload(result: dict[str, Any], worker_id: str, subtask: dict[str, Any]) -> dict[str, Any]:
    artifact_path = str(result.get("partial_diff_artifact") or "").strip()
    if not artifact_path and isinstance(result.get("artifacts"), dict):
        artifact_path = str(result["artifacts"].get("partial_diff") or "").strip()
    state = "none"
    if artifact_path:
        state = "accepted" if result.get("returncode") == 0 else "preserved_not_accepted"
    return {
        "worker_id": worker_id,
        "subtask_id": subtask.get("id"),
        "execution_id": str(subtask.get("_worker_execution_id") or "").strip(),
        "partial_diff_state": state,
        "partial_diff_artifact": artifact_path,
        "changed_files": list(result.get("changed_files") or []),
        "synced_files": list(result.get("synced_files") or []),
        "failure_reason": result.get("failure_reason"),
        "blocker_kind": result.get("blocker_kind"),
        "recovery_action": result.get("recovery_action"),
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
    
    execution_id = str(subtask.get("_worker_execution_id") or "").strip()

    # Create an isolated worktree for this worker/subtask ownership pair.
    worktree_path = layout["worktrees"] / _worker_execution_worktree_name(worker_id, subtask)
    worktree = create_worktree(repo_path, worktree_path)
    log_path = layout["logs"] / f"{worker_id}.log"
    carry_forward_patch = str(subtask.get("carry_forward_patch") or "").strip()
    carry_forward_patches = [
        str(path).strip()
        for path in (subtask.get("carry_forward_patches") or [])
        if str(path).strip()
    ]
    if carry_forward_patch:
        carry_forward_patches.insert(0, carry_forward_patch)
    for raw_patch_path in list(dict.fromkeys(carry_forward_patches)):
        patch_path = Path(raw_patch_path)
        if not _apply_carry_forward_patch(worktree, patch_path, log_path):
            return _carry_forward_patch_failure_result(patch_path, worker_id, subtask, log_path)
    subtask = _materialize_worker_context(worktree, subtask)
    subtask = _annotate_ignored_target_files(worktree, subtask)
    
    _prepare_worktree_targets(worktree, subtask)
    preflight_ok, preflight_detail = _worktree_preflight(cfg, worktree)
    if not preflight_ok:
        _prepare_worktree_targets(worktree, subtask)
        preflight_ok, preflight_detail = _worktree_preflight(cfg, worktree)
    
    worker_model_selection = engine_session_provider_model(cfg, "worker")
    worker_cli_provider = worker_model_selection["provider"]
    worker_model = worker_model_selection["model"]
    env = engine_env(cfg)
    config_path = None
    
    task_source = task.get("source") if isinstance(task, dict) else {}
    require_filesystem_changes = (
        isinstance(task_source, dict)
        and str(task_source.get("type") or "").strip() == "github_project"
    )

    requires_real_diff = require_filesystem_changes or _subtask_requires_real_diff(subtask)
    worktree_satisfied = bool(subtask.get("pre_satisfied")) and not requires_real_diff
    if not worktree_satisfied and not requires_real_diff:
        worktree_satisfied = _target_files_exist(worktree, subtask) and bool(_readable_target_files(worktree, subtask))
    
    write_required = requires_real_diff or not worktree_satisfied
    subtask = dict(subtask)
    subtask["write_required"] = write_required
    timeout_multiplier = _worker_timeout_multiplier(subtask)
    prompt = build_worker_prompt(run_id, worker_id, subtask, task, worktree)
    if not preflight_ok:
        prompt += (
            "\n\nPreflight warning:\n"
            f"- {preflight_detail}\n"
            "- Re-check the current directory with tools before doing any work.\n"
        )
    
    if execution_id:
        _mark_active_worker_attempt(layout, worker_id, execution_id)
    append_event(
        layout["events"],
        "worker.started",
        run_id,
        {
            "worker_id": worker_id,
            "subtask_id": subtask["id"],
            "worktree": str(worktree),
            "execution_id": execution_id,
        },
        task_id=task.get("task_id"),
        role="worker",
        repo={"path": str(repo_path)},
    )
    
    def _summarize_and_clear_current_attempt(worker_result: dict[str, Any]) -> dict[str, Any]:
        _clear_active_worker_attempt(layout, worker_id, execution_id)
        return summarize_worker_notes(worker_result, worker_id, subtask, worktree, index)

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
        timeout_multiplier=timeout_multiplier,
    )
    result["write_required"] = write_required

    if _run_has_terminal_status(layout):
        result = _late_terminal_worker_result(result, log_path=log_path, write_required=write_required)
        return _summarize_and_clear_current_attempt(result)
    if not _worker_event_attempt_is_current(
        layout,
        {"worker_id": worker_id, "execution_id": execution_id},
    ):
        result = _inactive_worker_attempt_result(
            result,
            log_path=log_path,
            write_required=write_required,
            execution_id=execution_id,
        )
        return _summarize_and_clear_current_attempt(result)
    
    # Sync artifacts after turn
    if not _run_has_terminal_status(layout):
        sync_worker_artifacts(worktree, layout["artifacts"], run_id, worker_id, layout["events"])

    result_before_terminalize = dict(result)
    result = _terminalize_worker_after_tool_loop(
        cfg,
        result,
        log_path,
        worktree,
        subtask,
        role=worker_id,
        provider=worker_cli_provider,
        model=worker_model,
        require_filesystem_changes=require_filesystem_changes,
    )
    if result.get("terminalized_after_tool_loop") and not result_before_terminalize.get("terminalized_after_tool_loop"):
        _append_worker_event_if_run_active(
            layout,
            log_path,
            "worker.terminalized_after_tool_loop",
            run_id,
            {
                "worker_id": worker_id,
                "subtask_id": subtask["id"],
                "execution_id": execution_id,
                "changed_files": list(result.get("changed_files") or []),
                "returncode": result.get("returncode"),
                "partial_diff_state": "accepted",
            },
            task_id=task.get("task_id"),
            role="worker",
            repo={"path": str(repo_path)},
        )

    result = _coerce_worker_failure(
        result,
        log_path,
        worktree,
        subtask,
        require_filesystem_changes=require_filesystem_changes,
    )
    result = _recover_nonzero_result_if_diff_satisfies_subtask(
        result,
        log_path,
        worktree,
        subtask,
        require_filesystem_changes=require_filesystem_changes,
        reason=str(result.get("failure_reason") or "late target diff"),
    )
    if result.get("partial_diff_artifact"):
        _append_worker_event_if_run_active(
            layout,
            log_path,
            "worker.partial_diff_preserved",
            run_id,
            _partial_diff_payload(result, worker_id, subtask),
            task_id=task.get("task_id"),
            role="worker",
            repo={"path": str(repo_path)},
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
            _append_worker_event_if_run_active(
                layout,
                log_path,
                "worker.pr_candidate_seeded",
                run_id,
                {
                    "worker_id": worker_id,
                    "subtask_id": subtask["id"],
                    "execution_id": execution_id,
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

    if _worker_result_should_retry(result) and not _run_has_terminal_status(layout):
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
        retry_prompt_sync_first = str(result.get("blocker_kind") or "") != "engine_tool_loop_stalled"
        _append_worker_event_if_run_active(
            layout,
            log_path,
            "worker.retry_started",
            run_id,
            {
                "worker_id": worker_id,
                "subtask_id": subtask["id"],
                "execution_id": execution_id,
                "previous_failure_reason": result.get("failure_reason"),
                "previous_blocker_kind": result.get("blocker_kind"),
                "write_required": write_required,
                "prompt_sync_first": retry_prompt_sync_first,
            },
            task_id=task.get("task_id"),
            role="worker",
            repo={"path": str(repo_path)},
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
            prompt_sync_first=retry_prompt_sync_first,
            timeout_multiplier=timeout_multiplier,
        )
        retry_result["write_required"] = write_required

        if _run_has_terminal_status(layout):
            retry_result = _late_terminal_worker_result(
                retry_result,
                log_path=log_path,
                write_required=write_required,
            )
            return _summarize_and_clear_current_attempt(retry_result)
        
        # Sync artifacts after retry turn
        if not _run_has_terminal_status(layout):
            sync_worker_artifacts(worktree, layout["artifacts"], run_id, worker_id, layout["events"])

        retry_before_terminalize = dict(retry_result)
        retry_result = _terminalize_worker_after_tool_loop(
            cfg,
            retry_result,
            log_path,
            worktree,
            subtask,
            role=worker_id,
            provider=worker_cli_provider,
            model=worker_model,
            require_filesystem_changes=require_filesystem_changes,
        )
        if retry_result.get("terminalized_after_tool_loop") and not retry_before_terminalize.get("terminalized_after_tool_loop"):
            _append_worker_event_if_run_active(
                layout,
                log_path,
                "worker.terminalized_after_tool_loop",
                run_id,
                {
                    "worker_id": worker_id,
                    "subtask_id": subtask["id"],
                    "execution_id": execution_id,
                    "changed_files": list(retry_result.get("changed_files") or []),
                    "returncode": retry_result.get("returncode"),
                    "partial_diff_state": "accepted",
                },
                task_id=task.get("task_id"),
                role="worker",
                repo={"path": str(repo_path)},
            )

        retry_result = _coerce_worker_failure(
            retry_result,
            log_path,
            worktree,
            subtask,
            require_filesystem_changes=require_filesystem_changes,
        )
        if retry_result["returncode"] != 0:
            retry_result = _recover_nonzero_result_if_diff_satisfies_subtask(
                retry_result,
                log_path,
                worktree,
                subtask,
                require_filesystem_changes=require_filesystem_changes,
                reason=str(retry_result.get("failure_reason") or "retry produced diff"),
            )
        if retry_result.get("partial_diff_artifact"):
            _append_worker_event_if_run_active(
                layout,
                log_path,
                "worker.partial_diff_preserved",
                run_id,
                _partial_diff_payload(retry_result, worker_id, subtask),
                task_id=task.get("task_id"),
                role="worker",
                repo={"path": str(repo_path)},
            )
        _append_worker_event_if_run_active(
            layout,
            log_path,
            "worker.retry_completed",
            run_id,
            {
                **_partial_diff_payload(retry_result, worker_id, subtask),
                "returncode": retry_result.get("returncode"),
            },
            task_id=task.get("task_id"),
            role="worker",
            repo={"path": str(repo_path)},
        )
        result = retry_result

    if _run_has_terminal_status(layout):
        result = _late_terminal_worker_result(result, log_path=log_path, write_required=write_required)
        return _summarize_and_clear_current_attempt(result)

    if not _worker_event_attempt_is_current(
        layout,
        {"worker_id": worker_id, "execution_id": execution_id},
    ):
        result = _inactive_worker_attempt_result(
            result,
            log_path=log_path,
            write_required=write_required,
            execution_id=execution_id,
        )
        return _summarize_and_clear_current_attempt(result)

    if result["returncode"] == 0:
        synced = sync_worktree_changes(worktree, repo_path)
        if isinstance(synced, list):
            synced_files = [str(path) for path in synced if str(path).strip()]
            if synced_files:
                result["synced_files"] = synced_files
                existing_changed = [str(path) for path in (result.get("changed_files") or []) if str(path).strip()]
                result["changed_files"] = list(dict.fromkeys([*existing_changed, *synced_files]))

    _append_worker_event_if_run_active(
        layout,
        log_path,
        "worker.completed" if result["returncode"] == 0 else "worker.failed",
        run_id,
        {
            "worker_id": worker_id,
            "subtask_id": subtask["id"],
            "execution_id": execution_id,
            "returncode": result["returncode"],
            **_partial_diff_payload(result, worker_id, subtask),
            "failure_reason": result.get("failure_reason"),
            "blocker_kind": result.get("blocker_kind"),
            "recovery_action": result.get("recovery_action"),
            "engine": result.get("engine"),
        },
        task_id=task.get("task_id"),
        role="worker",
        repo={"path": str(repo_path)},
    )

    return _summarize_and_clear_current_attempt(result)
