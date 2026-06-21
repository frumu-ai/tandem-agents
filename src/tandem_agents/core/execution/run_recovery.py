from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.engine.engine import delete_tandem_session
from src.tandem_agents.core.execution import runner_core as _rc
from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.phases.finalize import finalize_completed_run
from src.tandem_agents.core.phases.review_verify import _run_engine_command_checks
from src.tandem_agents.core.repository.board import load_board
from src.tandem_agents.core.repository.repo_truth import (
    deterministic_repo_validation,
    extract_command_checks,
    filter_executable_command_checks,
    infer_command_checks,
)
from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
from src.tandem_agents.core.verification.verification_policy import evaluate_verification_policy
from src.tandem_agents.runtime.artifact_store import configure_artifact_store_root
from src.tandem_agents.runtime.run_output import write_blackboard_snapshot
from src.tandem_agents.runtime.runstate import (
    append_event,
    ensure_layout,
    load_blackboard,
    load_status,
    save_blackboard,
    write_status,
)
from src.tandem_agents.utils.utils import atomic_write_json

logger = logging.getLogger("aca.execution.run_recovery")


def recover_restart_orphaned_runs(cfg: ResolvedConfig, *, limit: int = 20) -> list[dict[str, Any]]:
    """Recover runs orphaned after worker completion but before finalization.

    ACA can be restarted while a run is between worker sync and PR creation. The
    run directory has the authoritative worker diff, but the API process loses
    the in-memory thread state. This helper finds those durable runs and resumes
    the deterministic verification/finalize path without asking the worker model
    to redo the task.
    """

    configure_artifact_store_root(cfg.artifact_store_root())
    recovered: list[dict[str, Any]] = []
    for run_dir in _recent_run_dirs(cfg.output_root(), limit=limit):
        result = recover_restart_orphaned_run(cfg, run_dir)
        if result.get("recovered"):
            recovered.append(result)
    return recovered


def cleanup_terminal_orphaned_engine_sessions(
    cfg: ResolvedConfig,
    *,
    limit: int = 50,
    max_sessions: int | None = None,
    timeout_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Cancel active engine sessions left behind by terminal ACA runs.

    A worker may time out or block while its Tandem engine session is still
    doing tool work. If ACA then restarts or clears the task claim, those
    sessions can keep consuming engine capacity and make `/session` readiness
    probes time out. This cleanup is intentionally limited to terminal run
    directories and records failed deletes back into the run's durable marker.
    """

    remaining_sessions = _orphan_cleanup_max_sessions(cfg) if max_sessions is None else max(0, int(max_sessions))
    if remaining_sessions <= 0:
        return []
    delete_timeout = (
        _orphan_cleanup_timeout_seconds(cfg)
        if timeout_seconds is None
        else max(0.5, min(10.0, float(timeout_seconds)))
    )
    cleaned: list[dict[str, Any]] = []
    for run_dir in _recent_run_dirs(cfg.output_root(), limit=limit):
        result = cleanup_terminal_orphaned_engine_sessions_for_run(
            cfg,
            run_dir,
            max_sessions=remaining_sessions,
            timeout_seconds=delete_timeout,
        )
        if result.get("sessions"):
            cleaned.append(result)
            remaining_sessions -= len(result.get("sessions") or [])
            if remaining_sessions <= 0:
                break
    return cleaned


def cleanup_terminal_orphaned_engine_sessions_for_run(
    cfg: ResolvedConfig,
    run_dir: Path,
    *,
    max_sessions: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    run_id = run_dir.name
    status_path = run_dir / "status.json"
    if not status_path.exists():
        return {"run_id": run_id, "sessions": []}
    try:
        status = load_status(status_path)
    except Exception as exc:
        return {"run_id": run_id, "sessions": [], "reason": f"state_load_failed:{exc}"}
    run_status = str(_dict(status.get("run")).get("status") or "").strip().lower()
    if run_status not in {"blocked", "failed", "completed", "cancelled", "canceled"}:
        return {"run_id": run_id, "sessions": []}

    layout = ensure_layout(run_dir)
    events = _load_events(layout["events"])
    already_cancelled = _already_cancelled_session_ids(events)
    blackboard: dict[str, Any] = {}
    if layout["blackboard"].exists():
        try:
            blackboard = load_blackboard(layout["blackboard"])
        except Exception:
            blackboard = {}
    sessions = _terminal_engine_sessions(run_dir, status, blackboard)
    if max_sessions is not None:
        sessions = sessions[: max(0, int(max_sessions))]
    if not sessions:
        return {"run_id": run_id, "sessions": []}

    task = _dict(status.get("task")) or _dict(blackboard.get("task"))
    repo = _dict(status.get("repo")) or _dict(blackboard.get("repo"))
    results: list[dict[str, Any]] = []
    for session in sessions:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id or session_id in already_cancelled:
            continue
        worker_id = str(session.get("worker_id") or "worker-1").strip() or "worker-1"
        ok, error = _delete_tandem_session_with_timeout(
            cfg,
            session_id,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else _orphan_cleanup_timeout_seconds(cfg),
        )
        event_type = "run.orphan_engine_session_cancelled" if ok else "run.orphan_engine_session_cancel_failed"
        append_event(
            layout["events"],
            event_type,
            run_id,
            {
                "worker_id": worker_id,
                "session_id": session_id,
                "engine_run_id": str(session.get("run_id") or "").strip(),
                "reason": "terminal_run_orphan_cleanup",
                "error": error,
            },
            task_id=task.get("task_id"),
            role="worker",
            repo={"path": repo.get("path")},
        )
        if ok:
            _remove_active_session_marker(run_dir, worker_id, session_id)
        else:
            _write_active_session_cleanup_failure(run_dir, worker_id, session, error)
        results.append(
            {
                "worker_id": worker_id,
                "session_id": session_id,
                "ok": ok,
                "error": error,
            }
        )
    return {"run_id": run_id, "sessions": results}


def recover_restart_orphaned_run(cfg: ResolvedConfig, run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    run_id = run_dir.name
    status_path = run_dir / "status.json"
    blackboard_path = run_dir / "blackboard.yaml"
    if not status_path.exists() or not blackboard_path.exists():
        return {"run_id": run_id, "recovered": False, "reason": "missing_state"}
    try:
        status = load_status(status_path)
        blackboard = load_blackboard(blackboard_path)
    except Exception as exc:
        logger.debug("Could not load recovery state for %s", run_id, exc_info=True)
        return {"run_id": run_id, "recovered": False, "reason": f"state_load_failed:{exc}"}

    eligible, reason = _restart_orphan_is_recoverable(status, blackboard, run_dir)
    if not eligible:
        return {"run_id": run_id, "recovered": False, "reason": reason}

    layout = ensure_layout(run_dir)
    task = _dict(status.get("task")) or _dict(blackboard.get("task"))
    repo = _dict(status.get("repo")) or _dict(blackboard.get("repo"))
    repo_path = Path(str(repo.get("path") or ""))
    if not repo_path.exists():
        return {"run_id": run_id, "recovered": False, "reason": "repo_path_missing"}

    cfg_for_run = _config_for_recovered_run(cfg, status, task, repo)
    coordination = CoordinationStore.from_config(cfg_for_run)
    coordination.ensure_schema()
    ctx = _context_from_recovered_state(
        cfg=cfg_for_run,
        coordination=coordination,
        run_id=run_id,
        run_dir=run_dir,
        layout=layout,
        status=status,
        blackboard=blackboard,
        task=task,
        repo=repo,
    )

    append_event(
        layout["events"],
        "run.recovery_started",
        run_id,
        {"reason": "restart_orphan_after_worker_completion"},
        task_id=task.get("task_id"),
        role="manager",
        repo={"path": repo.get("path")},
    )
    try:
        _record_recovered_verification(ctx)
        result = finalize_completed_run(ctx)
    except Exception as exc:  # noqa: BLE001 - recovery must not break API startup
        logger.exception("Restart-orphan recovery failed for %s", run_id)
        append_event(
            layout["events"],
            "run.recovery_failed",
            run_id,
            {"reason": str(exc)[:1000]},
            task_id=task.get("task_id"),
            role="manager",
            repo={"path": repo.get("path")},
        )
        return {"run_id": run_id, "recovered": False, "reason": f"recovery_failed:{exc}"}
    return {
        "run_id": run_id,
        "recovered": True,
        "status": _dict(result.get("status")).get("run", {}).get("status"),
        "pull_request": _dict(result.get("status")).get("pull_request"),
    }


def _restart_orphan_is_recoverable(
    status: dict[str, Any],
    blackboard: dict[str, Any],
    run_dir: Path,
) -> tuple[bool, str]:
    run_status = str(_dict(status.get("run")).get("status") or "").strip().lower()
    if run_status not in {"created", "running"}:
        return False, "not_active"
    if _dict(status.get("blocker")).get("active") is True:
        return False, "blocked"
    events = _load_events(run_dir / "events.jsonl")
    event_types = [str(event.get("type") or "") for event in events]
    if "run.completed" in event_types or "run.blocked" in event_types:
        return False, "already_terminal"
    if "github_pull_request.created" in event_types:
        return False, "already_has_pr"
    workers = _list_of_dicts(blackboard.get("workers"))
    if not workers:
        return False, "missing_workers"
    if any(str(worker.get("status") or "").strip().lower() != "completed" for worker in workers):
        return False, "worker_not_completed"
    if int(_dict(status.get("metrics")).get("failed_workers") or 0) > 0:
        return False, "failed_workers"
    changed_files = _changed_files_from_workers(workers)
    if not changed_files:
        return False, "missing_changed_files"
    return True, "recoverable"


def _context_from_recovered_state(
    *,
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any],
    task: dict[str, Any],
    repo: dict[str, Any],
) -> RunContext:
    source = _dict(task.get("source"))
    board_path = Path(str(source.get("board_path") or layout["board"]))
    board = load_board(board_path)
    workers = _list_of_dicts(blackboard.get("workers"))
    planned_subtasks = _list_of_dicts(blackboard.get("subtasks")) or _list_of_dicts(_dict(blackboard.get("manager_plan")).get("subtasks"))
    return RunContext(
        run_id=run_id,
        run_dir=run_dir,
        layout=layout,
        cfg=cfg,
        coordination=coordination,
        engine=_dict(status.get("engine")) or _dict(blackboard.get("engine")),
        repo=repo,
        task=task,
        board=board,
        board_path=board_path,
        branch_name=str(repo.get("branch") or ""),
        status=status,
        blackboard=blackboard,
        source_type=str(source.get("type") or cfg.task_source.type),
        source_scope="intake_finalize",
        remote_sync="rich",
        execution_backend=str(task.get("execution_kind") or "legacy"),
        worker_results=workers,
        planned_subtasks=planned_subtasks,
        pending_subtasks=[],
        expected_repo_files=_string_list(blackboard.get("expected_repo_files")),
        repo_validation=_dict(blackboard.get("repo_validation")),
        review_result=_dict(blackboard.get("review")),
        test_result=_dict(blackboard.get("test")),
        manager_plan=_dict(blackboard.get("manager_plan")),
    )


def _record_recovered_verification(ctx: RunContext) -> None:
    changed_files = _changed_files_from_workers(ctx.worker_results)
    expected_files = _rc._validation_expected_repo_files(
        ctx.repo_path,
        list(ctx.expected_repo_files or []),
        changed_files,
    )
    ctx.expected_repo_files = expected_files
    commands = _recovered_verification_commands(ctx, changed_files)
    repo_validation = deterministic_repo_validation(ctx.repo_path, expected_files, command_checks=[])
    if commands:
        command_results = _run_engine_command_checks(ctx.cfg, ctx.repo_path, commands)
        command_failures = [result for result in command_results if result.get("status") != "pass"]
        repo_validation = dict(repo_validation)
        repo_validation["command_checks"] = command_results
        repo_validation["command_failures"] = command_failures
        repo_validation["ok"] = bool(repo_validation.get("ok")) and not command_failures
    ctx.repo_validation = repo_validation
    ctx.review_result = ctx.review_result or {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "status": "pass",
                "notes": ["Recovered after ACA API restart; worker diff was already completed."],
            },
            sort_keys=True,
        ),
    }
    ctx.test_result = ctx.test_result or {
        "returncode": 0,
        "stdout": json.dumps(
            {
                "status": "pass" if repo_validation.get("ok") else "blocked",
                "results": [
                    {
                        "command": result.get("command"),
                        "status": result.get("status"),
                    }
                    for result in repo_validation.get("command_checks", [])
                    if isinstance(result, dict)
                ],
            },
            sort_keys=True,
        ),
    }
    verification = evaluate_verification_policy(
        ctx.review_result,
        ctx.test_result,
        repo_validation=ctx.repo_validation,
    )
    ctx.blackboard["expected_repo_files"] = ctx.expected_repo_files
    ctx.blackboard["repo_validation"] = ctx.repo_validation
    ctx.blackboard["review"] = ctx.review_result
    ctx.blackboard["test"] = ctx.test_result
    ctx.blackboard["verification"] = verification.as_dict()
    ctx.blackboard["recovered_after_restart"] = True
    ctx.status["repo_validation"] = ctx.repo_validation
    ctx.status["verification"] = verification.as_dict()
    ctx.status["review"] = ctx.review_result
    ctx.status["test"] = ctx.test_result
    coding_run_contract = build_coding_run_contract(
        run_id=ctx.run_id,
        task=ctx.task,
        repo_path=ctx.repo_path,
        branch_name=ctx.branch_name,
        expected_repo_files=ctx.expected_repo_files,
    )
    _rc._record_coding_run_contract(ctx.blackboard, coding_run_contract)
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)
    append_event(
        ctx.layout["events"],
        "verification.recovered_after_restart",
        ctx.run_id,
        {
            "outcome": verification.outcome,
            "commands": commands,
            "expected_files": expected_files,
        },
        task_id=ctx.task.get("task_id"),
        role="tester",
        repo={"path": ctx.repo.get("path")},
    )


def _recovered_verification_commands(ctx: RunContext, changed_files: list[str]) -> list[str]:
    commands: list[str] = []
    commands.extend(_string_list(ctx.task.get("verification_commands")))
    commands.extend(_verification_commands_from_text(str(ctx.task.get("description") or ctx.task.get("raw_issue_body") or "")))
    commands.extend(extract_command_checks(ctx.manager_plan))
    commands.extend(infer_command_checks(ctx.repo_path, changed_files, task=ctx.task))
    return filter_executable_command_checks(list(dict.fromkeys(commands)))


def _verification_commands_from_text(text: str) -> list[str]:
    commands: list[str] = []
    for match in re.finditer(r"```(?:bash|sh|shell)?\s*\n(?P<body>.*?)```", text, re.IGNORECASE | re.DOTALL):
        for line in match.group("body").splitlines():
            command = line.strip()
            if command and not command.startswith("#"):
                commands.append(command)
    return commands


def _config_for_recovered_run(
    cfg: ResolvedConfig,
    status: dict[str, Any],
    task: dict[str, Any],
    repo: dict[str, Any],
) -> ResolvedConfig:
    cfg_for_run = copy.deepcopy(cfg)
    cfg_for_run.repository.path = str(repo.get("path") or cfg.repository.path)
    cfg_for_run.repository.slug = str(repo.get("slug") or cfg.repository.slug)
    cfg_for_run.repository.clone_url = str(repo.get("clone_url") or cfg.repository.clone_url)
    cfg_for_run.repository.default_branch = str(repo.get("default_branch") or cfg.repository.default_branch or "main")
    cfg_for_run.repository.remote_name = str(repo.get("remote_name") or cfg.repository.remote_name or "origin")
    cfg_for_run.repository.credential_file = str(repo.get("credential_file") or cfg.repository.credential_file)
    source = _dict(task.get("source"))
    if source.get("type"):
        cfg_for_run.task_source.type = str(source.get("type"))
    for key in ("team", "project", "item", "url"):
        if source.get(key):
            setattr(cfg_for_run.task_source, key, str(source.get(key)))
    provider = _dict(status.get("provider"))
    if provider.get("id"):
        cfg_for_run.provider.id = str(provider.get("id"))
    if provider.get("model"):
        cfg_for_run.provider.model = str(provider.get("model"))
    return cfg_for_run


def _load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _recent_run_dirs(output_root: Path, *, limit: int) -> list[Path]:
    if not output_root.exists():
        return []
    candidates = [
        path
        for path in output_root.iterdir()
        if path.is_dir() and (path.name.startswith("run-") or path.name.startswith("sched-"))
    ]
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[: max(1, int(limit))]


def _orphan_cleanup_max_sessions(cfg: ResolvedConfig) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_ORPHAN_ENGINE_SESSION_CLEANUP_MAX_SESSIONS", "") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_ORPHAN_ENGINE_SESSION_CLEANUP_MAX_SESSIONS=%r", raw)
    return 6


def _orphan_cleanup_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ORPHAN_ENGINE_SESSION_CLEANUP_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(0.5, min(10.0, float(raw)))
        except ValueError:
            logger.warning("Ignoring invalid ACA_ORPHAN_ENGINE_SESSION_CLEANUP_TIMEOUT_SECONDS=%r", raw)
    return 2.0


def _active_worker_engine_sessions_path(run_dir: Path) -> Path:
    return run_dir / "active_worker_engine_sessions.json"


def _load_active_worker_engine_sessions(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = _active_worker_engine_sessions_path(run_dir)
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
        sessions[worker_id]["worker_id"] = worker_id
        sessions[worker_id]["session_id"] = session_id
    return sessions


def _write_active_worker_engine_sessions(run_dir: Path, sessions: dict[str, dict[str, Any]]) -> None:
    path = _active_worker_engine_sessions_path(run_dir)
    cleaned: dict[str, dict[str, Any]] = {}
    for worker_id, session in sessions.items():
        session_id = str(session.get("session_id") or "").strip()
        if str(worker_id or "").strip() and session_id:
            cleaned[str(worker_id)] = dict(session)
            cleaned[str(worker_id)]["session_id"] = session_id
    if cleaned:
        atomic_write_json(path, cleaned)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_active_session_marker(run_dir: Path, worker_id: str, session_id: str) -> None:
    sessions = _load_active_worker_engine_sessions(run_dir)
    current = sessions.get(worker_id)
    if current and str(current.get("session_id") or "").strip() == session_id:
        sessions.pop(worker_id, None)
        _write_active_worker_engine_sessions(run_dir, sessions)


def _write_active_session_cleanup_failure(
    run_dir: Path,
    worker_id: str,
    session: dict[str, Any],
    error: str,
) -> None:
    sessions = _load_active_worker_engine_sessions(run_dir)
    current = dict(sessions.get(worker_id) or session)
    current["worker_id"] = worker_id
    current["session_id"] = str(session.get("session_id") or "").strip()
    current["run_id"] = str(session.get("run_id") or current.get("run_id") or "").strip()
    current["cleanup_failed_at_ms"] = int(time.time() * 1000)
    current["cleanup_error"] = str(error or "session_delete_failed")[:500]
    sessions[worker_id] = current
    _write_active_worker_engine_sessions(run_dir, sessions)


def _already_cancelled_session_ids(events: list[dict[str, Any]]) -> set[str]:
    cancelled: set[str] = set()
    for event in events:
        event_type = str(event.get("type") or "").strip()
        if event_type not in {
            "worker.engine_cancelled",
            "manager.engine_cancelled",
            "run.orphan_engine_session_cancelled",
        }:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            cancelled.add(session_id)
    return cancelled


def _terminal_engine_sessions(
    run_dir: Path,
    status: dict[str, Any],
    blackboard: dict[str, Any],
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for worker_id, info in _load_active_worker_engine_sessions(run_dir).items():
        session = dict(info)
        session["worker_id"] = worker_id
        _append_unique_session(sessions, session)

    for detail in (
        str(_dict(status.get("blocker")).get("detail") or ""),
        str(_dict(blackboard.get("blocker")).get("detail") or ""),
    ):
        for match in re.finditer(r"session_id=([A-Za-z0-9][A-Za-z0-9_.:-]+)", detail):
            _append_unique_session(
                sessions,
                {
                    "worker_id": _worker_id_from_text(detail) or "worker-1",
                    "session_id": match.group(1),
                    "run_id": _engine_run_id_from_text(detail),
                },
            )
    for worker in _list_of_dicts(blackboard.get("workers")) + _list_of_dicts(status.get("workers")):
        if str(worker.get("status") or "").strip().lower() not in {"failed", "blocked", "cancelled", "canceled"}:
            continue
        blocker_kind = str(worker.get("blocker_kind") or worker.get("failure_reason") or "").lower()
        if "engine" not in blocker_kind:
            continue
        engine = _dict(worker.get("engine"))
        session_id = str(engine.get("session_id") or worker.get("session_id") or "").strip()
        if not session_id:
            continue
        _append_unique_session(
            sessions,
            {
                "worker_id": str(worker.get("worker_id") or "worker-1").strip() or "worker-1",
                "session_id": session_id,
                "run_id": str(engine.get("run_id") or worker.get("engine_run_id") or "").strip(),
            },
        )
    return sessions


def _worker_id_from_text(text: str) -> str:
    match = re.search(r"worker=([A-Za-z0-9_.:-]+)", text)
    return match.group(1) if match else ""


def _engine_run_id_from_text(text: str) -> str:
    match = re.search(r"engine_run_id=([A-Za-z0-9_.:-]+)", text)
    return match.group(1) if match else ""


def _append_unique_session(sessions: list[dict[str, Any]], session: dict[str, Any]) -> None:
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        return
    if any(str(existing.get("session_id") or "").strip() == session_id for existing in sessions):
        return
    cleaned = dict(session)
    cleaned["session_id"] = session_id
    cleaned["worker_id"] = str(cleaned.get("worker_id") or "worker-1").strip() or "worker-1"
    sessions.append(cleaned)


def _delete_tandem_session_with_timeout(
    cfg: ResolvedConfig,
    session_id: str,
    *,
    timeout_seconds: float,
) -> tuple[bool, str]:
    result: dict[str, Any] = {}

    def _delete() -> None:
        try:
            delete_tandem_session(cfg, session_id)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001 - cleanup should record and move on
            result["error"] = exc

    thread = threading.Thread(target=_delete, name="aca-cleanup-orphan-engine-session", daemon=True)
    thread.start()
    thread.join(max(0.1, timeout_seconds))
    if thread.is_alive():
        return False, "session_delete_timeout"
    if result.get("ok"):
        return True, ""
    error = result.get("error")
    return False, str(error or "session_delete_failed")[:500]


def _changed_files_from_workers(workers: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for worker in workers:
        for raw_path in _string_list(worker.get("changed_files")):
            if raw_path not in files:
                files.append(raw_path)
    return files


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if value in (None, "", [], (), {}):
        return []
    raw_items = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item or "").strip() for item in raw_items if str(item or "").strip()]
