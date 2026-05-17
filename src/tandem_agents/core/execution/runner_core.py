from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from src.tandem_agents.core.repository.board import card_to_task, claim_card, move_card, save_board, select_card
from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
from src.tandem_agents.core.coordination.coordination import CoordinationStore, default_host_id, default_worker_id
from src.tandem_agents.core.shutdown import ShutdownHandler
from src.tandem_agents.core.engine.coder_backend import (
    build_coder_summary,
    coder_backend_mode,
    coder_workflow_supported,
    execute_coder_run,
)
from src.tandem_agents.core.engine.engine import checkout_run_branch, commit_repository_changes, effective_tandem_provider, engine_env, engine_health, ensure_engine, git_diff_stat, push_repository_changes, resolve_repository, task_run_branch_name, write_provider_override_config
from src.tandem_agents.core.integrations.github_mcp import (
    add_issue_comment,
    build_issue_comment_body,
    ensure_github_mcp_connected,
    ensure_github_mcp_disconnected,
    github_project_status_name_for_outcome,
    github_project_status_name_for_task_state,
    get_mcp_server,
    get_pull_request,
    github_mcp_scope,
    github_remote_sync_mode,
    list_pull_requests,
    update_project_item_status,
)
from src.tandem_agents.core.repository.repository import repository_binding_issues
from src.tandem_agents.core.scheduling.outbox_dispatcher import dispatch_outbox_tick
from src.tandem_agents.core.scheduling.coder_supervisor import apply_coder_result
from src.tandem_agents.core.verification.review_policy import evaluate_review_policy
from src.tandem_agents.core.verification.verification_policy import evaluate_verification_policy, review_blocker_message, test_blocker_message
from src.tandem_agents.core.engine.prompts import build_integration_prompt, build_manager_prompt, build_qa_prompt, build_review_prompt, build_test_prompt, derive_subtasks
from src.tandem_agents.core.repository.repo_truth import (
    collect_expected_repo_files,
    deterministic_repo_validation,
    discover_repo_files,
    extract_command_checks,
    file_is_readable,
    repo_context_summary,
    repo_validation_blocker_message,
    subtask_satisfied,
)
from src.tandem_agents.core.execution.run_lifecycle import (
    block_run,
    build_provider_config_dict,
    build_swarm_config_dict,
    make_run_result,
)
from src.tandem_agents.core.phases.engine_check import (
    check_engine_at_startup,
    check_engine_health,
    resolve_repo_after_checkout,
)
from src.tandem_agents.core.phases.planning import (
    pre_screen_subtasks,
    run_manager_prompt,
)
from src.tandem_agents.core.phases.review_verify import run_review_and_test
from src.tandem_agents.core.phases.finalize import finalize_completed_run
from src.tandem_agents.core.phases.github_sync import (
    connect_for_intake,
    disconnect_for_coding,
    sync_claim_status,
    finalize_sync,
)
from src.tandem_agents.core.phases.task_intake import run_task_intake
from src.tandem_agents.core.phases.worker_dispatch import dispatch_workers
from src.tandem_agents.core.phases.repair import (
    RepairDecision,
    build_retry_feedback,
    check_no_diff,
    check_no_verifiable_proof,
)
from src.tandem_agents.core.phases.context import RunContext as _PhaseRunContext
from src.tandem_agents.runtime.run_output import build_blocked_summary, build_completed_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot, write_diff_snapshot
from src.tandem_agents.runtime.artifact_store import configure_artifact_store_root
from src.tandem_agents.runtime.runstate import append_event, ensure_layout, initial_blackboard, initial_status, load_status, new_run_id, save_blackboard, write_status
from src.tandem_agents.runtime.task_sources import invalidate_cached_github_project_board_snapshot, normalize_task
from src.tandem_agents.utils.utils import slugify
from src.tandem_agents.core.execution.worker import run_worker_subtask, stream_tandem_prompt, sync_worker_artifacts
from src.tandem_agents.core.engine.tandem_client_sdk import (
    sdk_agent_teams_list_approvals,
    sdk_agent_teams_approve_spawn,
    sdk_list_permissions,
    sdk_reply_permission,
)

logger = logging.getLogger("aca.runner_core")


def _extract_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    fence = re.search(r"```json\s*(.*?)\s*```", text, re.S | re.I)
    if fence:
        candidates.append(fence.group(1).strip())
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1].strip())
    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except Exception:
            logger.debug("Failed to parse candidate JSON in _extract_json", exc_info=True)
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _wait_for_engine(cfg: ResolvedConfig, timeout: float = 90.0, poll_interval: float = 5.0) -> None:
    """Block until the tandem engine is healthy, or raise after timeout."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            engine_health(cfg, timeout=5.0)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(poll_interval)
    raise RuntimeError(f"Tandem engine did not recover within {timeout}s: {last_exc}")


def _append_blackboard_note(blackboard: dict[str, Any], message: str) -> None:
    blackboard.setdefault("notes", []).append(message)


def _record_coding_run_contract(blackboard: dict[str, Any], contract: Any) -> None:
    blackboard["coding_run_contract"] = contract.as_dict()
    if getattr(contract, "code_editing", False):
        note = "Coding run contract: diff review and minimal verification are required before handoff."
        notes = blackboard.setdefault("notes", [])
        if note not in notes:
            notes.append(note)


def _record_review_policy(blackboard: dict[str, Any], cfg: ResolvedConfig) -> None:
    decision = evaluate_review_policy(cfg)
    blackboard["review_policy"] = decision.as_dict()
    note = "Review policy: human review gate required before merge."
    if decision.blocker:
        note = f"Review policy: {decision.blocker}"
    notes = blackboard.setdefault("notes", [])
    if note not in notes:
        notes.append(note)


def _coordination_store(cfg: ResolvedConfig) -> CoordinationStore:
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    return store


def _dispatch_outbox_now(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    return dispatch_outbox_tick(cfg, coordination=coordination, limit=limit)


def _task_claim_identity(cfg: ResolvedConfig, task: dict[str, Any]) -> dict[str, str]:
    source = task.get("source") or {}
    role = "coordinator"
    worker_id = default_worker_id(cfg)
    host_id = default_host_id(cfg)
    if str(cfg.env.get("ACA_COORDINATION_ROLE") or "").strip():
        role = str(cfg.env.get("ACA_COORDINATION_ROLE") or "").strip()
    return {"worker_id": worker_id, "host_id": host_id, "role": role, "source_type": str(source.get("type") or cfg.task_source.type or "")}


COORDINATION_LOST_THRESHOLD = 3


def _touch_coordination(
    coordination: CoordinationStore,
    *,
    run_id: str,
    lease_id: str | None,
    lease_ttl_seconds: int,
    status: str | None = None,
    phase: str | None = None,
    error: str | None = None,
    completed: bool = False,
    ctx: "_PhaseRunContext | None" = None,
) -> bool:
    """Heartbeat the lease and update the run row.

    Returns True if the heartbeat succeeded (lease still active) or was not
    attempted (lease_id is None). Returns False if the heartbeat missed —
    callers passing ``ctx`` will see ``ctx.consecutive_heartbeat_misses``
    incremented; on the COORDINATION_LOST_THRESHOLD-th consecutive miss
    ``ctx.coordination_lost`` is flipped True so the next phase boundary can
    block the run with a clear blocker.
    """
    heartbeat_ok = True
    if lease_id:
        result = coordination.heartbeat_lease(lease_id, lease_ttl_seconds=lease_ttl_seconds)
        if result is None:
            heartbeat_ok = False
            if ctx is not None:
                ctx.consecutive_heartbeat_misses = int(ctx.consecutive_heartbeat_misses or 0) + 1
                if (
                    ctx.consecutive_heartbeat_misses >= COORDINATION_LOST_THRESHOLD
                    and not ctx.coordination_lost
                ):
                    ctx.coordination_lost = True
                    try:
                        from src.tandem_agents.runtime.runstate import append_event

                        append_event(
                            ctx.layout["events"],
                            "coordination_lost",
                            ctx.run_id,
                            {
                                "lease_id": lease_id,
                                "consecutive_misses": ctx.consecutive_heartbeat_misses,
                            },
                        )
                    except Exception:
                        # Don't let event logging failure mask the real problem.
                        pass
        elif ctx is not None:
            ctx.consecutive_heartbeat_misses = 0
    coordination.update_run(
        run_id,
        status=status,
        phase=phase,
        error=error,
        completed=completed,
    )
    return heartbeat_ok


def _coordination_task_context(status: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    coordination = dict(status.get("coordination") or {})
    return (
        coordination.get("task_key"),
        coordination.get("lease_id"),
        coordination.get("worker_id"),
        coordination.get("host_id"),
        coordination.get("lease_expires_at_ms"),
    )


def _move_task_card_if_present(
    board: dict[str, Any],
    task: dict[str, Any],
    lane: str,
    actor: str,
    note: str,
) -> None:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return
    try:
        move_card(board, task_id, lane, actor, note)
    except Exception:
        logger.warning(f"Failed to move card {task_id} to {lane}", exc_info=True)
        return


def _normalize_manager_subtasks(
    task: dict[str, Any],
    raw_subtasks: list[dict[str, Any]],
    repo_path: str,
    discovered_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    repo_prefix = str(Path(repo_path)).rstrip("/") + "/"
    for index, item in enumerate(raw_subtasks, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or f"Subtask {index}").strip()
        description = str(item.get("description") or "").strip()
        goal = str(item.get("goal") or description or title or task.get("title") or f"Subtask {index}").strip()
        acceptance = item.get("acceptance_criteria")
        if not acceptance:
            acceptance = item.get("acceptance")
        if not acceptance:
            acceptance = item.get("acceptance_checklist")
        if not acceptance:
            acceptance = item.get("validation")
        acceptance_criteria = [str(entry).strip() for entry in _as_list(acceptance) if str(entry).strip()]
        raw_files = [str(entry).strip() for entry in _as_list(item.get("files")) if str(entry).strip()]
        raw_target_files = [str(entry).strip() for entry in _as_list(item.get("target_files")) if str(entry).strip()]
        verification_commands = [
            str(entry).strip()
            for entry in _as_list(item.get("verification_commands") or task.get("verification_commands"))
            if str(entry).strip()
        ]
        deliverables = [
            str(entry).strip()
            for entry in _as_list(item.get("deliverables") or task.get("deliverables"))
            if str(entry).strip()
        ]
        dependencies = [
            str(entry).strip()
            for entry in _as_list(item.get("dependencies") or task.get("dependencies"))
            if str(entry).strip()
        ]
        in_scope = [
            str(entry).strip()
            for entry in _as_list(item.get("in_scope") or task.get("in_scope"))
            if str(entry).strip()
        ]
        out_of_scope = [
            str(entry).strip()
            for entry in _as_list(item.get("out_of_scope") or task.get("out_of_scope"))
            if str(entry).strip()
        ]
        normalized_files: list[str] = []
        for entry in raw_files:
            if entry.startswith(repo_prefix):
                normalized_files.append(entry[len(repo_prefix) :])
            elif entry.startswith("/"):
                normalized_files.append(Path(entry).name)
            else:
                normalized_files.append(entry)
        normalized_target_files: list[str] = []
        for entry in raw_target_files:
            if entry.startswith(repo_prefix):
                normalized_target_files.append(entry[len(repo_prefix) :])
            elif entry.startswith("/"):
                normalized_target_files.append(Path(entry).name)
            else:
                normalized_target_files.append(entry)
        if not normalized_files and normalized_target_files:
            normalized_files = list(normalized_target_files)
        if not normalized_target_files and normalized_files:
            normalized_target_files = list(normalized_files)
        normalized.append(
            {
                "id": str(item.get("id") or item.get("subtask_id") or f"subtask-{index}").strip(),
                "title": title,
                "goal": goal,
                "description": description,
                "acceptance_criteria": acceptance_criteria,
                "deliverables": deliverables,
                "files": normalized_files,
                "target_files": normalized_target_files,
                "verification_commands": verification_commands,
                "dependencies": dependencies,
                "program_goal": str(item.get("program_goal") or task.get("program_goal") or "").strip() or None,
                "local_goal": str(item.get("local_goal") or task.get("local_goal") or goal).strip(),
                "in_scope": in_scope,
                "out_of_scope": out_of_scope,
                "status": str(item.get("status") or "pending").strip(),
            }
        )
    if normalized:
        if discovered_files:
            chunks = [discovered_files[i::len(normalized)] for i in range(len(normalized))]
            for index, item in enumerate(normalized):
                if item.get("files"):
                    continue
                item["files"] = chunks[index] or list(discovered_files)
        return normalized
    fallback = derive_subtasks(task, 1)
    if discovered_files and fallback:
        fallback[0]["files"] = list(discovered_files)
    return fallback


def _prepare_subtasks_with_discovery(
    task: dict[str, Any],
    manager_plan: dict[str, Any],
    repo_path: Path,
    max_workers: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    discovered_files = discover_repo_files(repo_path, task, limit=12)
    subtasks = _normalize_manager_subtasks(
        task,
        list(manager_plan.get("subtasks") or []),
        str(repo_path),
        discovered_files,
    )
    if not subtasks:
        subtasks = derive_subtasks(task, max_workers)
        if discovered_files:
            subtasks[0]["files"] = list(discovered_files)
    return discovered_files, subtasks


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _collect_expected_repo_files(subtasks: list[dict[str, Any]]) -> list[str]:
    return collect_expected_repo_files(subtasks)


def _upsert_worker_result(collection: list[dict[str, Any]], result: dict[str, Any]) -> None:
    identity = str(result.get("subtask_id") or result.get("worker_id") or "").strip()
    if not identity:
        collection.append(result)
        return
    for index, existing in enumerate(collection):
        existing_identity = str(existing.get("subtask_id") or existing.get("worker_id") or "").strip()
        if existing_identity == identity:
            collection[index] = result
            return
    collection.append(result)


def _record_worker_result(
    blackboard: dict[str, Any],
    worker_results: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    _upsert_worker_result(worker_results, result)
    _upsert_worker_result(blackboard.setdefault("workers", []), result)


def _worker_result_metrics(worker_results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "completed_workers": 0,
        "failed_workers": 0,
        "skipped_workers": 0,
        "tolerated_workers": 0,
    }
    for result in worker_results:
        status = _normalized_text(result.get("status"))
        if status == "completed":
            counts["completed_workers"] += 1
        elif status == "failed":
            counts["failed_workers"] += 1
        elif status == "skipped_existing":
            counts["skipped_workers"] += 1
        elif status == "tolerated_failure":
            counts["tolerated_workers"] += 1
    return counts


def _deterministic_repo_validation(repo_path: Path, expected_files: list[str]) -> dict[str, Any]:
    return deterministic_repo_validation(repo_path, expected_files)


def _repo_validation_blocker_message(repo_validation: dict[str, Any]) -> str | None:
    return repo_validation_blocker_message(repo_validation)


def _has_verifiable_worker_success(worker_results: list[dict[str, Any]]) -> bool:
    for result in worker_results:
        status = _normalized_text(result.get("status"))
        if status in {"skipped_existing", "tolerated_failure"}:
            return True
        if result.get("verified_existing"):
            return True
    return False


def _execute_local_worker_pool(
    cfg: ResolvedConfig,
    run_id: str,
    repo_path: Path,
    run_dir: Path,
    task: dict[str, Any],
    pending_subtasks: list[dict[str, Any]],
    worker_limit: int,
    *,
    worker_runner: Callable[
        [ResolvedConfig, str, Path, Path, dict[str, Any], dict[str, Any], str, int],
        dict[str, Any],
    ] = run_worker_subtask,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    if not pending_subtasks:
        return []
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, worker_limit)) as executor:
        futures = {
            executor.submit(worker_runner, cfg, run_id, repo_path, run_dir, task, subtask, f"worker-{index}", index): (
                index,
                subtask,
                f"worker-{index}",
            )
            for index, subtask in enumerate(pending_subtasks, start=1)
        }
        for future in as_completed(futures):
            index, subtask, worker_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "worker_id": worker_id,
                    "subtask_index": index,
                    "subtask_id": subtask["id"],
                    "title": subtask["title"],
                    "status": "failed",
                    "returncode": 1,
                    "worktree": "",
                    "log_path": "",
                    "output_excerpt": f"Worker execution raised an exception: {exc}",
                    "write_required": bool(subtask.get("write_required", True)),
                    "verified_existing": False,
                }
            if not isinstance(result, dict):
                result = {}
            result.setdefault("worker_id", worker_id)
            result.setdefault("subtask_index", index)
            result.setdefault("subtask_id", subtask["id"])
            result.setdefault("title", subtask["title"])
            result.setdefault("status", "failed" if result.get("returncode", 1) else "completed")
            result.setdefault("returncode", 0 if _normalized_text(result.get("status")) == "completed" else 1)
            result.setdefault("write_required", bool(subtask.get("write_required", True)))
            result.setdefault("verified_existing", False)
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results


def _all_subtasks_verified_existing(
    subtasks: list[dict[str, Any]],
    worker_results: list[dict[str, Any]],
    repo_validation: dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
) -> bool:
    if not subtasks or not worker_results:
        return False
    source = (task or {}).get("source") if isinstance(task, dict) else {}
    if isinstance(source, dict) and str(source.get("type") or "").strip() == "github_project":
        return False
    if repo_validation is not None and not repo_validation.get("ok"):
        return False
    status_by_subtask_id: dict[str, str] = {}
    for result in worker_results:
        subtask_id = str(result.get("subtask_id") or "").strip()
        if not subtask_id:
            continue
        status_by_subtask_id[subtask_id] = _normalized_text(result.get("status"))
    if len(status_by_subtask_id) < len(subtasks):
        return False
    return all(
        status_by_subtask_id.get(str(subtask.get("id") or "").strip())
        in {"skipped_existing", "tolerated_failure"}
        for subtask in subtasks
    )


def _review_blocker_message(
    review_result: dict[str, Any],
    repo_validation: dict[str, Any] | None = None,
) -> str | None:
    return review_blocker_message(review_result, repo_validation=repo_validation)


def _test_blocker_message(test_result: dict[str, Any], repo_validation: dict[str, Any] | None = None) -> str | None:
    return test_blocker_message(test_result, repo_validation=repo_validation)


def _init_github_mcp_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    scope: str,
    remote_sync: str,
) -> dict[str, Any]:
    status["github_mcp"] = {
        "scope": scope,
        "remote_sync": remote_sync,
        "connected": None,
        "last_action": "initialized",
        "warnings": [],
    }
    write_status(layout["status"], status)
    return status


def _update_github_mcp_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    connected: bool | None,
    last_action: str,
    warning: str | None = None,
) -> dict[str, Any]:
    github_state = status.setdefault(
        "github_mcp",
        {"scope": "none", "remote_sync": "off", "connected": None, "last_action": "initialized", "warnings": []},
    )
    github_state["connected"] = connected
    github_state["last_action"] = last_action
    if warning:
        github_state.setdefault("warnings", []).append(warning)
    write_status(layout["status"], status)
    return status


def _record_github_warning(
    *,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any] | None,
    message: str,
) -> None:
    append_event(layout["events"], "github_mcp.warning", run_id, {"message": message})
    _update_github_mcp_status(status, layout, connected=None, last_action="warning", warning=message)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"GitHub MCP warning: {message}")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)


def _connect_github_for_phase(
    *,
    cfg: ResolvedConfig,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any] | None,
    blackboard: dict[str, Any] | None,
    event_type: str,
    required: bool,
) -> bool:
    try:
        ensure_github_mcp_connected(cfg)
    except Exception as exc:
        if required:
            raise
        if status is not None:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message=str(exc),
            )
        return False
    append_event(layout["events"], event_type, run_id, {"connected": True})
    if status is not None:
        _update_github_mcp_status(status, layout, connected=True, last_action=event_type)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"GitHub MCP connected for phase `{event_type}`.")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)
    return True


def _disconnect_github_for_coding(
    *,
    cfg: ResolvedConfig,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any] | None,
    blackboard: dict[str, Any] | None,
    event_type: str,
) -> None:
    server = get_mcp_server(cfg, "github")
    if not server or not server.get("connected"):
        return
    try:
        ensure_github_mcp_disconnected(cfg)
    except Exception as exc:
        if status is not None:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message=str(exc),
            )
        return
    append_event(layout["events"], event_type, run_id, {"connected": False})
    if status is not None:
        _update_github_mcp_status(status, layout, connected=False, last_action=event_type)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"GitHub MCP disconnected for phase `{event_type}`.")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)


def _sync_github_claim_status(
    *,
    cfg: ResolvedConfig,
    task: dict[str, Any],
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any],
    remote_sync: str,
    coordination: CoordinationStore | None = None,
) -> None:
    if remote_sync == "off":
        return
    if coordination is not None:
        coordination.enqueue_outbox(
            kind="github_project.status_update",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "target_status": github_project_status_name_for_task_state("active"),
                "task": task,
            },
            dedupe_key=f"{run_id}:github:claim",
        )
        summary = _dispatch_outbox_now(cfg, coordination, limit=25)
        terminal_failure = False
        for result in summary.get("items") or []:
            payload = dict(result.get("payload") or {})
            if str(payload.get("run_id") or "") != run_id:
                continue
            if str(result.get("kind") or "") != "github_project.status_update":
                continue
            if str(result.get("status") or "").strip().lower() != "dispatched":
                terminal_failure = terminal_failure or bool(result.get("terminal"))
                continue
            append_event(layout["events"], "github_project.status_updated", run_id, {"status": payload.get("target_status") or github_project_status_name_for_task_state("active")})
            source = task.get("source") or {}
            owner = str(source.get("owner") or "").strip()
            project = source.get("project")
            if owner and project not in (None, ""):
                try:
                    invalidate_cached_github_project_board_snapshot(cfg, owner, int(project))
                except Exception:
                    logger.debug("Failed to invalidate GitHub project board snapshot", exc_info=True)
            _append_blackboard_note(blackboard, f"GitHub Project status updated to `{payload.get('target_status') or github_project_status_name_for_task_state('active')}`.")
        if terminal_failure:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message="GitHub Project claim status could not be dispatched from the outbox.",
            )
        return
    target_status = github_project_status_name_for_task_state("active")
    warning = update_project_item_status(cfg, task, target_status)
    if warning:
        _record_github_warning(run_id=run_id, layout=layout, status=status, blackboard=blackboard, message=warning)
        return
    append_event(layout["events"], "github_project.status_updated", run_id, {"status": target_status})
    source = task.get("source") or {}
    owner = str(source.get("owner") or "").strip()
    project = source.get("project")
    if owner and project not in (None, ""):
        try:
            invalidate_cached_github_project_board_snapshot(cfg, owner, int(project))
        except Exception:
            logger.debug("Failed to invalidate GitHub project board snapshot", exc_info=True)
    _append_blackboard_note(blackboard, f"GitHub Project status updated to `{target_status}`.")
    save_blackboard(layout["blackboard"], blackboard)
    write_blackboard_snapshot(layout["run_dir"], blackboard)


def _finalize_github_sync(
    *,
    cfg: ResolvedConfig,
    task: dict[str, Any],
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any] | None,
    outcome: str,
    summary: str,
    diff_snapshot: str | None = None,
    review_returncode: int | None = None,
    test_returncode: int | None = None,
    coordination: CoordinationStore | None = None,
) -> bool:
    """Enqueue + dispatch GitHub finalize-status / comment outbox events.

    Returns True if a terminal outbox failure occurred. Callers that complete
    a successful run (outcome="completed") should treat True as a hard error
    and block the run with kind="github_sync_failed" — otherwise the operator
    sees a green run while the GitHub board still shows In progress.

    Non-ship callers (outcome != "completed") can ignore the return value:
    a terminal sync failure on a blocked run is not interesting because the
    task is already in a non-completion state.
    """
    source_type = str((task.get("source") or {}).get("type") or cfg.task_source.type)
    remote_sync = github_remote_sync_mode(cfg, source_type)
    scope = github_mcp_scope(cfg, source_type)
    if remote_sync == "off" or scope not in {"intake_finalize", "always"}:
        return False
    if coordination is not None:
        target_status = github_project_status_name_for_outcome(outcome)
        coordination.enqueue_outbox(
            kind="github_project.status_update",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "outcome": outcome,
                "summary": summary,
                "target_status": target_status,
                "task": task,
            },
            dedupe_key=f"{run_id}:github:finalize-status",
        )
        if remote_sync == "status_comment":
            comment_body = build_issue_comment_body(
                run_id=run_id,
                task_title=task.get("title") or "GitHub task",
                outcome=outcome,
                summary=summary,
                diff_snapshot=diff_snapshot,
                review_returncode=review_returncode,
                test_returncode=test_returncode,
            )
            coordination.enqueue_outbox(
                kind="github_issue.comment",
                aggregate_type="task",
                aggregate_id=str(task.get("task_id") or run_id),
                payload={
                    "run_id": run_id,
                    "outcome": outcome,
                    "summary": summary,
                    "diff_snapshot": diff_snapshot,
                    "review_returncode": review_returncode,
                    "test_returncode": test_returncode,
                    "body": comment_body,
                    "task": task,
                },
                dedupe_key=f"{run_id}:github:finalize-comment",
            )
        summary_result = _dispatch_outbox_now(cfg, coordination, limit=25)
        terminal_failure = False
        for result in summary_result.get("items") or []:
            payload = dict(result.get("payload") or {})
            if str(payload.get("run_id") or "") != run_id:
                continue
            kind = str(result.get("kind") or "").strip()
            if str(result.get("status") or "").strip().lower() != "dispatched":
                terminal_failure = terminal_failure or bool(result.get("terminal"))
                continue
            if kind == "github_project.status_update" and str(result.get("status") or "").strip().lower() == "dispatched":
                target_status = payload.get("target_status") or github_project_status_name_for_outcome(outcome)
                append_event(layout["events"], "github_project.status_updated", run_id, {"status": target_status})
                source = task.get("source") or {}
                owner = str(source.get("owner") or "").strip()
                project = source.get("project")
                if owner and project not in (None, ""):
                    try:
                        invalidate_cached_github_project_board_snapshot(cfg, owner, int(project))
                    except Exception:
                        logger.debug("Failed to invalidate GitHub project board snapshot", exc_info=True)
                if blackboard is not None:
                    _append_blackboard_note(blackboard, f"GitHub Project status updated to `{target_status}`.")
            elif kind == "github_issue.comment":
                append_event(layout["events"], "github_project.comment_added", run_id, {"outcome": outcome})
                if blackboard is not None:
                    _append_blackboard_note(blackboard, "GitHub issue summary comment added.")
        if terminal_failure:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message="GitHub finalize sync could not be fully dispatched from the outbox.",
            )
        if blackboard is not None:
            _append_blackboard_note(blackboard, "GitHub sync enqueued through the coordination outbox.")
            save_blackboard(layout["blackboard"], blackboard)
            write_blackboard_snapshot(layout["run_dir"], blackboard)
        if scope != "always":
            _disconnect_github_for_coding(
                cfg=cfg,
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                event_type="github_mcp.disconnected_after_finalize",
            )
        return terminal_failure
    if not _connect_github_for_phase(
        cfg=cfg,
        run_id=run_id,
        layout=layout,
        status=status,
        blackboard=blackboard,
        event_type="github_mcp.connected_for_finalize",
        required=False,
    ):
        return False
    if blackboard is not None:
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)
    if scope != "always":
        _disconnect_github_for_coding(
            cfg=cfg,
            run_id=run_id,
            layout=layout,
            status=status,
            blackboard=blackboard,
            event_type="github_mcp.disconnected_after_finalize",
        )
    return False


def _role_provider_override_config(
    *,
    cfg: ResolvedConfig,
    layout: dict[str, Path],
    role: str,
    provider: str,
    model: str,
) -> Path | None:
    artifacts_dir = layout["run_dir"] / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return write_provider_override_config(
        cfg=cfg,
        provider=provider,
        model=model,
        output_path=artifacts_dir / f"{role}-provider-config.json",
    )


def _auto_approve_loop(cfg: ResolvedConfig, stop_event: threading.Event) -> None:
    """Background thread to auto-approve Tandem permissions and agent spawn requests."""
    seen_approvals: set[str] = set()
    seen_permissions: set[str] = set()

    while not stop_event.is_set():
        try:
            # 1. Handle spawn approvals
            approvals_payload = sdk_agent_teams_list_approvals(cfg)
            items = (approvals_payload.get("approvals") or []) if isinstance(approvals_payload, dict) else []
            for ap in items:
                ap_id = str(ap.get("approval_id") or ap.get("id") or "")
                status = str(ap.get("status") or "").strip().lower()
                if ap_id and status == "pending" and ap_id not in seen_approvals:
                    try:
                        sdk_agent_teams_approve_spawn(cfg, ap_id, reason="ACA auto-approve spawn")
                        seen_approvals.add(ap_id)
                    except Exception:
                        logger.warning("Failed to auto-approve spawn %s", ap_id, exc_info=True)

            # 2. Handle general permissions (bash, write, etc)
            permissions_payload = sdk_list_permissions(cfg)
            perms = (permissions_payload.get("permissions") or []) if isinstance(permissions_payload, dict) else []
            for perm in perms:
                request_id = str(perm.get("request_id") or perm.get("id") or "")
                status = str(perm.get("status") or "").strip().lower()
                if request_id and status == "pending" and request_id not in seen_permissions:
                    try:
                        sdk_reply_permission(cfg, request_id, "allow")
                        seen_permissions.add(request_id)
                    except Exception:
                        logger.warning("Failed to auto-approve permission %s", request_id, exc_info=True)
        except Exception:
            logger.debug("Auto-approve loop tick failed", exc_info=True)
        time.sleep(1.0)


def run_qa(cfg: ResolvedConfig, pr_number: int) -> dict[str, Any]:
    """Specialized run mode to audit an existing Pull Request."""
    run_id = cfg.env.get("ACA_RUN_ID") or new_run_id(prefix="qa")
    output_root = cfg.output_root()
    run_dir = output_root / run_id
    configure_artifact_store_root(cfg.artifact_store_root())
    layout = ensure_layout(run_dir)
    
    append_event(layout["events"], "qa.started", run_id, {"pr_number": pr_number})
    
    # 1. Resolve Repo
    repo = resolve_repository(cfg)
    repo_path = Path(repo["path"])
    
    # 2. Fetch PR Info via GitHub MCP
    ensure_github_mcp_connected(cfg)
    slug = cfg.repository.slug
    owner, repo_name = slug.split("/", 1)
    pr_info = get_pull_request(cfg, owner, repo_name, pr_number)
    
    head_branch = pr_info["head"]["ref"]
    append_event(layout["events"], "qa.pr_fetched", run_id, {"branch": head_branch, "title": pr_info["title"]})
    
    # 3. Checkout PR Branch
    run_command(["git", "-C", str(repo_path), "fetch", "origin", head_branch], env=cfg.env)
    run_command(["git", "-C", str(repo_path), "checkout", head_branch], env=cfg.env)
    
    # 4. Get Diff against Base
    base_branch = pr_info["base"]["ref"]
    diff_result = run_command(["git", "-C", str(repo_path), "diff", f"origin/{base_branch}...HEAD"], env=cfg.env)
    diff_text = diff_result.stdout
    
    # 5. Execute QA Agent
    qa_prompt = build_qa_prompt(
        run_id=run_id,
        task={"title": pr_info["title"], "description": pr_info["body"], "acceptance_criteria": []},
        pr_info=pr_info,
        diff=diff_text
    )
    
    qa_provider, qa_model = cfg.provider_for_role("reviewer")
    qa_cli_provider = effective_tandem_provider(qa_provider, cfg)
    
    result = stream_tandem_prompt(
        cfg,
        role="qa-agent",
        prompt=qa_prompt,
        cwd=repo_path,
        provider=qa_cli_provider,
        model=qa_model,
        env=engine_env(cfg),
        log_path=layout["logs"] / "qa-agent.log",
        require_tool_use=True,
    )
    
    # Sync artifacts (like browser screenshots)
    sync_worker_artifacts(repo_path, layout["artifacts"], run_id, "qa-agent", layout["events"])
    
    # 6. Finalize
    status = initial_status(run_id, {"title": f"QA Audit: PR #{pr_number}"}, repo, {"version": "qa"}, {"id": qa_provider, "model": qa_model}, {}, run_dir)
    status["run"]["status"] = "completed" if result["returncode"] == 0 else "failed"
    write_status(layout["status"], status)
    
    blackboard = initial_blackboard(run_id, {"title": f"QA Audit: PR #{pr_number}"}, repo, {}, {}, {})
    blackboard["qa_result"] = result["stdout"]
    save_blackboard(layout["blackboard"], blackboard)
    
    append_event(layout["events"], "qa.completed", run_id, {"returncode": result["returncode"]})
    
    return {"run_id": run_id, "status": status, "result": result}


def run_once(cfg: ResolvedConfig) -> dict[str, Any]:
    run_id = cfg.env.get("ACA_RUN_ID") or new_run_id()
    output_root = cfg.output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_id
    configure_artifact_store_root(cfg.artifact_store_root())
    layout = ensure_layout(run_dir)
    coordination = _coordination_store(cfg)

    append_event(layout["events"], "run.started", run_id, {"run_dir": str(run_dir)})

    engine, blocked = check_engine_at_startup(cfg, run_id, run_dir, layout)
    if blocked is not None:
        return blocked

    # Start background auto-approval loop
    stop_approvals = threading.Event()
    approval_thread = threading.Thread(
        target=_auto_approve_loop,
        args=(cfg, stop_approvals),
        daemon=True,
    )
    approval_thread.start()

    shutdown_handler = ShutdownHandler()
    shutdown_handler.hook()

    try:
        return _run_once_internal(cfg, run_id, run_dir, layout, coordination)
    finally:
        shutdown_handler.unhook()
        stop_approvals.set()
        approval_thread.join(timeout=2.0)


def _run_once_internal(
    cfg: ResolvedConfig,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    coordination: CoordinationStore,
) -> dict[str, Any]:
    """Wrapper around _run_once_internal_impl that guarantees:
      1. The coordination lease is released on every exit path (success,
         blocked, or uncaught exception). Without this, a crash leaves the
         lease alive until TTL expires and blocks any other worker from
         picking up the same task.
      2. Uncaught exceptions get logged with structured context and produce
         a standard blocked-run result instead of bubbling out of run_once.
    """
    refs: dict[str, Any] = {"ctx": None}
    crashed_exc: Exception | None = None
    result: dict[str, Any] | None = None

    try:
        result = _run_once_internal_impl(cfg, run_id, run_dir, layout, coordination, refs)
        return result
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        crashed_exc = exc
        ctx_local = refs.get("ctx")
        phase_str = "unknown"
        if ctx_local and getattr(ctx_local, "status", None):
            phase_str = str(ctx_local.status.get("phase") or "unknown")
        logger.exception(
            "Unhandled exception in run_once (run_id=%s, phase=%s, lease_id=%s)",
            run_id,
            phase_str,
            getattr(ctx_local, "lease_id", None),
        )
        try:
            return block_run(
                run_id=run_id,
                run_dir=run_dir,
                layout=layout,
                cfg=cfg,
                task=getattr(ctx_local, "task", None) if ctx_local else None,
                repo=getattr(ctx_local, "repo", None) if ctx_local else None,
                engine=getattr(ctx_local, "engine", {}) if ctx_local else {},
                phase=phase_str,
                kind="internal_error",
                message=f"Unhandled exception: {exc}",
                phase_detail=str(exc),
                coordination=coordination,
                existing_status=getattr(ctx_local, "status", None) if ctx_local else None,
            )
        except Exception:
            logger.exception("Failed to write blocked-on-crash status (run_id=%s)", run_id)
            return {
                "run_id": run_id,
                "status": {
                    "run_status": "blocked",
                    "blocker": {"kind": "internal_error", "message": str(exc)},
                },
                "layout": {k: str(v) for k, v in layout.items()},
            }
    finally:
        ctx_final = refs.get("ctx")
        if ctx_final is not None and getattr(ctx_final, "lease_id", None):
            try:
                if crashed_exc is not None:
                    release_status = "failed"
                    release_reason = f"crashed: {crashed_exc}"
                else:
                    run_status_str = ""
                    try:
                        run_status_str = (ctx_final.status or {}).get("run_status") or ""
                    except Exception:
                        pass
                    if run_status_str == "blocked":
                        release_status = "blocked"
                        release_reason = "run blocked"
                    elif run_status_str == "completed":
                        release_status = "completed"
                        release_reason = "run completed"
                    else:
                        release_status = "completed"
                        release_reason = f"run finished (status={run_status_str or 'unknown'})"
                ctx_final.coordination.release_lease(
                    str(ctx_final.lease_id),
                    status=release_status,
                    reason=release_reason,
                )
            except Exception:
                logger.exception(
                    "Failed to release lease %s in finally (run_id=%s)",
                    ctx_final.lease_id,
                    run_id,
                )


def _run_once_internal_impl(
    cfg: ResolvedConfig,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    coordination: CoordinationStore,
    refs: dict[str, Any],
) -> dict[str, Any]:
    """Thin orchestrator for a single ACA coding run.

    Each logical phase is handled by a dedicated phase module in
    ``src/tandem_agents/core/phases/``.  The RunContext object carries all shared
    mutable state so phase functions have clean single-argument signatures.

    Phase order
    -----------
    1. Engine health + repository binding     (engine_check)
    2. Task intake + coordination claim       (task_intake)
    3. Coder-backend fast path               (inline — short-circuits before planning)
    4. Repair loop  (max_loops iterations):
       a. Manager prompt + subtask planning  (planning)
       b. Subtask pre-screening             (planning)
       c. Worker dispatch                   (worker_dispatch)
       d. Integration prompt                (inline — runner_core private helpers)
       e. No-diff / no-proof repair check   (repair)
       f. Review + verification             (review_verify)
       g. Retry or finalize                 (repair / finalize)
    """
    # ------------------------------------------------------------------
    # Phase 1: Engine health + repository binding
    # ------------------------------------------------------------------
    engine, blocked = check_engine_health(cfg, run_id, run_dir, layout)
    if blocked is not None:
        return blocked

    # resolve_repository() called inside check_engine_health already resolves
    # and returns the repo — retrieve it via a lightweight re-ping (no disk I/O).
    repo = resolve_repository(cfg)
    append_event(layout["events"], "repo.resolved", run_id, {"path": repo["path"], "branch": repo.get("branch")})

    # ------------------------------------------------------------------
    # Phase 2: Task intake + coordination claim
    # ------------------------------------------------------------------
    ctx = _PhaseRunContext(
        run_id=run_id,
        run_dir=run_dir,
        layout=layout,
        cfg=cfg,
        coordination=coordination,
        engine=engine,
        repo=repo,
    )
    # Register ctx with the wrapper so its finally can release the lease on
    # every exit path (including uncaught exceptions). See _run_once_internal.
    refs["ctx"] = ctx

    blocked = run_task_intake(ctx)
    if blocked is not None:
        return blocked

    # GitHub sync: claim status
    sync_claim_status(ctx)

    # ------------------------------------------------------------------
    # Phase 3: Coder-backend fast path
    # ------------------------------------------------------------------
    if ctx.execution_backend == "legacy" and ctx.source_scope != "always":
        disconnect_for_coding(ctx)

    if ctx.execution_backend == "coder":
        return _run_coder_backend(ctx)

    # ------------------------------------------------------------------
    # Phase 4: Repair loop
    # ------------------------------------------------------------------
    max_loops = getattr(cfg.swarm, "max_retries", 1) + 1
    previous_feedback: str | None = None

    for attempt in range(max_loops):
        # If coordination has been lost (3+ consecutive heartbeat misses) we
        # must not continue mutating run state on a dead lease — another
        # worker may have already reclaimed the task. Block early so the
        # operator sees a clear blocker and the reaper / reclaim logic can
        # take over cleanly.
        if ctx.coordination_lost:
            return block_run(
                run_id=run_id,
                run_dir=run_dir,
                layout=layout,
                cfg=cfg,
                task=ctx.task,
                repo=ctx.repo,
                engine=ctx.engine,
                phase="coordination",
                kind="coordination_lost",
                message=(
                    f"Lost coordination lease after {ctx.consecutive_heartbeat_misses} "
                    "consecutive heartbeat misses. Another worker may have reclaimed the task."
                ),
                phase_detail="lease heartbeat repeatedly missed",
                coordination=coordination,
                existing_status=ctx.status,
            )
        if attempt > 0:
            ctx.status = set_status(
                ctx.status, layout, phase="planning",
                phase_detail=f"Retrying (attempt {attempt + 1})"
            )
            append_event(layout["events"], "run.retry", run_id, {"attempt": attempt + 1})

        # 4a. Manager prompt
        setattr(ctx, "_previous_feedback", previous_feedback)
        manager_result = run_manager_prompt(ctx)
        if manager_result["returncode"] != 0:
            append_event(
                layout["events"], "manager.failed", run_id,
                {"returncode": manager_result["returncode"]},
                task_id=ctx.task.get("task_id"), role="manager", repo={"path": ctx.repo.get("path")},
            )
            ctx.status = set_status(
                ctx.status, layout, phase="planning",
                phase_detail="manager planning failed", run_status="blocked",
                blocker=(True, "manager", "Manager planning failed", "manager"),
            )
            _touch_coordination(
                coordination, run_id=run_id, lease_id=ctx.lease_id,
                lease_ttl_seconds=cfg.coordination.lease_ttl_seconds,
                status="blocked", phase="planning", error="Manager planning failed",
            )

        # 4b. Pre-screen subtasks
        ctx.worker_results = []
        all_pre_satisfied = pre_screen_subtasks(ctx)
        write_status(layout["status"], ctx.status)

        if not ctx.planned_subtasks and not any(s.get("files") for s in (ctx.planned_subtasks or [])):
            return _block_no_targets(ctx)

        if ctx.status["run"]["status"] == "blocked":
            return _block_manager_failed(ctx)

        # 4c. Early-exit if everything already satisfied
        if all_pre_satisfied:
            return _complete_pre_satisfied(ctx)

        # 4d. Worker dispatch
        ctx.status["metrics"]["planned_workers"] = len(ctx.planned_subtasks)
        ctx.status["metrics"].setdefault("skipped_workers", 0)
        ctx.status["metrics"].setdefault("tolerated_workers", 0)
        write_status(layout["status"], ctx.status)

        dispatch_workers(ctx)

        if ctx.status["metrics"]["failed_workers"]:
            repo_blocker = _repo_validation_blocker_message(ctx.repo_validation)
            if repo_blocker:
                return _block_worker_failure(ctx)
            _append_blackboard_note(
                ctx.blackboard,
                "Worker failures were tolerated because the expected repository files were present after sync.",
            )
            save_blackboard(layout["blackboard"], ctx.blackboard)
            write_blackboard_snapshot(run_dir, ctx.blackboard)

        # 4e. Integration prompt  (still inline — small and tightly coupled to result handling)
        integration_result = _run_integration_prompt(ctx)
        append_event(
            layout["events"],
            "manager.completed" if integration_result["returncode"] == 0 else "manager.failed",
            run_id, {"stage": "integration", "returncode": integration_result["returncode"]},
            task_id=ctx.task.get("task_id"), role="manager", repo={"path": ctx.repo.get("path")},
        )

        if integration_result["returncode"] != 0:
            return _block_integration_failed(ctx)

        # 4f. No-diff / no-proof repair check
        ctx.pending_diff_snapshot = git_diff_stat(ctx.repo_path)
        if not ctx.pending_diff_snapshot.strip() and not ctx.repo_validation.get("ok"):
            decision = check_no_diff(ctx, attempt, max_loops)
            if decision.action == "retry":
                previous_feedback = decision.feedback
                continue
            return _block_from_decision(ctx, decision)

        if not ctx.pending_diff_snapshot.strip() and ctx.repo_validation.get("ok"):
            decision = check_no_verifiable_proof(ctx, attempt, max_loops)
            if decision.action == "retry":
                previous_feedback = decision.feedback
                continue
            if decision.action == "block":
                return _block_from_decision(ctx, decision)
            # "continue" => fall through to review

        # 4g. Review + verification
        verification = run_review_and_test(ctx)

        if verification.should_retry:
            if attempt < max_loops - 1:
                previous_feedback = build_retry_feedback(ctx, attempt, verification)
                continue
            return _block_verification_failed(ctx, verification)

        # 4h. Finalize (happy path)
        return finalize_completed_run(ctx)

    # Should not reach here — loop always returns
    return _block_from_decision(
        ctx,
        RepairDecision(
            action="block",
            message="Run exceeded maximum retry loop count without completing.",
            kind="max_retries",
            phase="handoff",
        ),
    )




# ---------------------------------------------------------------------------
# _run_once_internal helper functions
# These support the thin orchestrator above. Each extracts one cohesive
# terminal path or sub-step so the main loop stays readable.
# ---------------------------------------------------------------------------

def _run_integration_prompt(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Run the integration prompt and return the raw stream result."""
    integration_prompt = build_integration_prompt(ctx.run_id, ctx.task, ctx.worker_results)
    integration_provider, integration_model = ctx.cfg.provider_for_role("manager")
    integration_cli_provider = effective_tandem_provider(integration_provider, ctx.cfg)
    _role_provider_override_config(
        cfg=ctx.cfg,
        layout=ctx.layout,
        role="integration",
        provider=integration_cli_provider,
        model=integration_model,
    )
    return stream_tandem_prompt(
        ctx.cfg,
        role="integration",
        prompt=integration_prompt,
        cwd=ctx.repo_path,
        provider=integration_cli_provider,
        model=integration_model,
        env=engine_env(ctx.cfg),
        log_path=ctx.layout["logs"] / "manager-integration.log",
        config_path=None,
    )


def _run_coder_backend(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Handle the coder-backend execution path (fast-exit branch)."""
    from src.tandem_agents.core.engine.coder_backend import coder_workflow_supported, execute_coder_run
    from src.tandem_agents.runtime.run_output import (
        build_coder_summary,
        build_blocked_summary,
        save_run_text,
        set_status,
        write_blackboard_snapshot,
        write_board_snapshot,
    )
    from src.tandem_agents.core.repository.board import save_board

    board = ctx.board
    board_path = ctx.board_path

    if not coder_workflow_supported(ctx.task, ctx.repo):
        _reason = "Coder backend only supports GitHub Project tasks backed by a linked issue."
        ctx.status = set_status(
            ctx.status, ctx.layout,
            phase="coder_execution",
            phase_detail="coder backend does not support this task shape",
            run_status="blocked",
            blocker=(True, "coder", _reason, "manager"),
            run_completed=True,
        )
        save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=_reason))
        _finalize_github_sync(
            cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
            status=ctx.status, blackboard=ctx.blackboard,
            outcome="blocked", summary=_reason, coordination=ctx.coordination,
        )
        if ctx.lease_id:
            ctx.coordination.release_lease(str(ctx.lease_id), status="blocked", reason="coder backend unsupported")
        append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "coder_unsupported"})
        return ctx.make_result()

    ctx.status = set_status(ctx.status, ctx.layout, phase="coder_execution", phase_role="worker", run_status="running")
    _touch_coordination(
        ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running", phase="coder_execution",
        ctx=ctx,
    )
    task_key, lease_id, worker_id, host_id, lease_expires_at_ms = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id and lease_expires_at_ms is not None:
        ctx.coordination.mark_task_active(
            task_key, run_id=ctx.run_id, lease_id=lease_id,
            worker_id=worker_id, host_id=host_id,
            lease_expires_at_ms=int(lease_expires_at_ms), reason="coder execution started",
        )
    append_event(ctx.layout["events"], "coder.started", ctx.run_id,
                 {"workflow_mode": "issue_fix"}, task_id=ctx.task.get("task_id"),
                 role="manager", repo={"path": ctx.repo.get("path")})

    try:
        coder_result = execute_coder_run(ctx.cfg, run_id=ctx.run_id, repo=ctx.repo, task=ctx.task)
    except Exception as exc:
        detail = str(exc).strip() or repr(exc)
        ctx.status = set_status(
            ctx.status, ctx.layout, phase="coder_execution", phase_detail=detail,
            run_status="blocked", blocker=(True, "coder", detail, "manager"), run_completed=True,
        )
        _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                            lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                            status="blocked", phase="coder_execution", error=detail, completed=True)
        _move_task_card_if_present(board, ctx.task, "blocked", "manager", "coder execution failure")
        save_board(board_path, board)
        write_board_snapshot(ctx.run_dir, board)
        save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=detail))
        _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                              status=ctx.status, blackboard=ctx.blackboard,
                              outcome="blocked", summary=detail, coordination=ctx.coordination)
        if ctx.lease_id:
            ctx.coordination.release_lease(str(ctx.lease_id), status="blocked", reason="coder execution failed")
        append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "coder"})
        return ctx.make_result()

    ctx.blackboard["coder_run"] = coder_result.get("coder_run") or {}
    ctx.blackboard["artifacts"] = coder_result.get("artifacts") or []
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    append_event(ctx.layout["events"], "coder.completed", ctx.run_id,
                 {"status": coder_result.get("status"), "phase": coder_result.get("phase"),
                  "artifact_count": len(coder_result.get("artifacts") or [])},
                 task_id=ctx.task.get("task_id"), role="manager", repo={"path": ctx.repo.get("path")})

    apply_coder_result(
        ctx.cfg,
        ctx.coordination,
        run_id=ctx.run_id,
        coder_result=coder_result,
        status_payload=ctx.status,
        blackboard=ctx.blackboard,
    )
    ctx.status = load_status(ctx.layout["status"])
    return ctx.make_result()


def _block_no_targets(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when the manager produced no subtask targets."""
    from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    from src.tandem_agents.core.repository.board import save_board

    msg = "Manager planning produced no subtasks and ACA could not infer a credible repo target set."
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="planning",
        phase_detail="no credible repository target set could be inferred",
        run_status="blocked",
        blocker=(True, "manager", msg, "manager"),
        metrics={"planned_workers": len(ctx.planned_subtasks), "completed_workers": 0,
                 "failed_workers": 0, "skipped_workers": 0, "tolerated_workers": 0},
        run_completed=True,
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="planning", error=msg, completed=True)
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", "no repository target set")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=msg))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard, outcome="blocked", summary=msg)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "no_targets"})
    return ctx.make_result()


def _block_manager_failed(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when manager planning returned a non-zero exit."""
    from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text

    blocker = dict(ctx.status.get("blocker") or {})
    msg = str(
        blocker.get("message")
        or ctx.status.get("phase", {}).get("detail")
        or "Manager planning failed for task."
    ).strip()
    kind = str(blocker.get("kind") or "manager").strip() or "manager"
    phase_detail = str(ctx.status.get("phase", {}).get("detail") or msg).strip()
    save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=msg))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=msg)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": kind, "detail": phase_detail})
    return ctx.make_result()


def _complete_pre_satisfied(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a completed-run result when all subtasks were pre-satisfied."""
    from src.tandem_agents.core.engine.engine import git_diff_stat
    from src.tandem_agents.runtime.run_output import (
        build_completed_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    _append_blackboard_note(ctx.blackboard, "Repository already satisfied the expected files; skipping worker execution.")
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    diff = git_diff_stat(ctx.repo_path)
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="handoff",
        phase_detail="repository already satisfied task",
        run_status="completed", run_completed=True,
        metrics={**_worker_result_metrics(ctx.worker_results),
                 "planned_workers": len(ctx.planned_subtasks), "tests_passed": True},
    )
    save_run_text(ctx.layout["summary"], build_completed_summary(
        run_id=ctx.run_id, task_title=ctx.task["title"], repo_path=ctx.repo.get("path"),
        engine_label=ctx.engine.get("version") or ctx.engine.get("status") or "unknown",
        provider_id=ctx.cfg.provider.id, provider_model=ctx.cfg.provider.model,
        worker_results=ctx.worker_results, review_returncode=0, test_returncode=0, diff_snapshot=diff,
    ))
    sync_failed = _finalize_github_sync(
        cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
        status=ctx.status, blackboard=ctx.blackboard,
        outcome="completed", summary="Repository already satisfied the requested task.",
        diff_snapshot=diff, review_returncode=0, test_returncode=0,
    )
    if sync_failed:
        return block_run(
            run_id=ctx.run_id, run_dir=ctx.run_dir, layout=ctx.layout, cfg=ctx.cfg,
            task=ctx.task, repo=ctx.repo, engine=ctx.engine,
            phase="handoff",
            kind="github_sync_failed",
            message=(
                "Run was successful locally but the GitHub finalize sync hit a terminal "
                "outbox failure. The remote board will not show the completed status; "
                "investigate with `aca lease list` and the GitHub MCP logs."
            ),
            phase_detail="github finalize outbox dispatch hit terminal failure",
            coordination=ctx.coordination,
            existing_status=ctx.status,
        )
    task_key, lease_id, worker_id, host_id, _ = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id:
        ctx.coordination.mark_task_done(task_key, run_id=ctx.run_id, lease_id=lease_id,
                                        worker_id=worker_id, host_id=host_id,
                                        reason="repository already satisfied task")
    append_event(ctx.layout["events"], "run.completed", ctx.run_id, {"kind": "verified_existing"})
    return ctx.make_result()


def _block_worker_failure(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when one or more workers failed critically."""
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    ctx.status = set_status(
        ctx.status, ctx.layout, phase="worker_execution",
        phase_detail="one or more workers failed", run_status="blocked",
        blocker=(True, "worker", "One or more workers failed", "worker"),
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="worker_execution", error="One or more workers failed")
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", "worker failure")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"], message="One or more workers failed.",
        worker_results=ctx.worker_results,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary="One or more worker executions failed.")
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "worker"})
    return ctx.make_result()


def _block_integration_failed(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when the integration prompt failed."""
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    ctx.status = set_status(
        ctx.status, ctx.layout, phase="handoff",
        phase_detail="integration failed", run_status="blocked",
        blocker=(True, "manager", "Integration prompt failed", "manager"),
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="handoff", error="Integration prompt failed")
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", "integration failure")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"], message="Integration prompt failed after worker completion.",
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary="Integration prompt failed after worker completion.",
                          review_returncode=None, test_returncode=None)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "integration"})
    return ctx.make_result()


def _block_verification_failed(ctx: "_PhaseRunContext", verification: Any) -> dict[str, Any]:
    """Return a blocked-run result when verification failed after all retries."""
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    failure_category = str(getattr(verification, "failure_category", None) or verification.outcome).strip() or verification.outcome
    label = failure_category.replace("_", "-")
    blocker_msg = verification.validation_blocker or "Review or test failed"
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="handoff",
        phase_detail=f"{label}: {blocker_msg}",
        run_status="blocked",
        blocker=(True, failure_category, blocker_msg, "reviewer"),
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="handoff", error=blocker_msg)
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", f"{label} validation failure")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"],
        message=f"{label}: {blocker_msg}",
        worker_results=ctx.worker_results,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=f"{label}: {blocker_msg}",
                          diff_snapshot=ctx.pending_diff_snapshot,
                          review_returncode=ctx.review_result.get("returncode"),
                          test_returncode=ctx.test_result.get("returncode"))
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": failure_category})
    return ctx.make_result()


def _block_from_decision(ctx: "_PhaseRunContext", decision: "RepairDecision") -> dict[str, Any]:
    """Convert a RepairDecision(action='block') into a blocked-run result dict."""
    from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status, write_blackboard_snapshot
    from src.tandem_agents.runtime.runstate import save_blackboard

    msg = decision.message or "Run blocked."
    ctx.status = set_status(
        ctx.status, ctx.layout, phase=decision.phase,
        phase_detail=msg, run_status="blocked",
        blocker=(True, decision.kind or "unknown", msg, "manager"),
        run_completed=True,
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase=decision.phase, error=msg, completed=True)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"], message=msg, worker_results=ctx.worker_results,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=msg,
                          review_returncode=None, test_returncode=None)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": decision.kind or "unknown"})
    return ctx.make_result()
