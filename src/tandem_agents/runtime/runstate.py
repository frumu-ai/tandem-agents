from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.tandem_agents.utils.utils import atomic_write_json, atomic_write_yaml, iso_now, load_yaml, now_ms, short_id
from src.tandem_agents.runtime.artifact_store import mirror_run_file

logger = logging.getLogger("aca.runtime.runstate")

EVENT_LOCK = threading.Lock()
_EVENT_BROADCAST_CALLBACK = None

def set_event_broadcast_callback(callback):
    global _EVENT_BROADCAST_CALLBACK
    _EVENT_BROADCAST_CALLBACK = callback

def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{short_id()}"


def ensure_layout(run_dir: Path) -> dict[str, Path]:
    layout = {
        "run_dir": run_dir,
        "status": run_dir / "status.json",
        "events": run_dir / "events.jsonl",
        "summary": run_dir / "summary.md",
        "board": run_dir / "board.yaml",
        "blackboard": run_dir / "blackboard.yaml",
        "logs": run_dir / "logs",
        "worktrees": run_dir / "worktrees",
        "diffs": run_dir / "diffs",
        "artifacts": run_dir / "artifacts",
    }
    for key in ("logs", "worktrees", "diffs", "artifacts"):
        layout[key].mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    return layout


def initial_status(
    run_id: str,
    task: dict[str, Any],
    repo: dict[str, Any],
    engine: dict[str, Any],
    provider: dict[str, Any],
    swarm: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    ms = now_ms()
    return {
        "run": {
            "run_id": run_id,
            "status": "created",
            "created_at_ms": ms,
            "updated_at_ms": ms,
            "started_at_ms": None,
            "completed_at_ms": None,
            "owner": None,
            "source": None,
        },
        "task": task,
        "repo": repo,
        "engine": engine,
        "provider": provider,
        "swarm": swarm,
        "phase": {"name": "bootstrap", "updated_at_ms": ms, "detail": None, "role": None},
        "blocker": {"active": False, "kind": None, "message": None, "owner_role": None},
        "artifacts": {
            "run_dir": str(run_dir),
            "status_json": str(run_dir / "status.json"),
            "events_jsonl": str(run_dir / "events.jsonl"),
            "summary_md": str(run_dir / "summary.md"),
            "board_yaml": str(run_dir / "board.yaml"),
            "blackboard_yaml": str(run_dir / "blackboard.yaml"),
            "logs_dir": str(run_dir / "logs"),
            "worktrees_dir": str(run_dir / "worktrees"),
            "diffs_dir": str(run_dir / "diffs"),
        },
        "timestamps": {
            "created_at_ms": ms,
            "updated_at_ms": ms,
            "started_at_ms": None,
            "completed_at_ms": None,
        },
        "metrics": {
            "planned_workers": 0,
            "completed_workers": 0,
            "failed_workers": 0,
            "skipped_workers": 0,
            "tolerated_workers": 0,
            "tests_passed": None,
        },
    }


def load_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_status(path: Path, status: dict[str, Any]) -> None:
    atomic_write_json(path, status)
    run_dir = path.parent
    run_id = str(status.get("run", {}).get("run_id") or run_dir.name)
    mirror_run_file(run_id, path, "status.json")


def update_status(status: dict[str, Any], **changes: Any) -> dict[str, Any]:
    run = status.setdefault("run", {})
    ts = status.setdefault("timestamps", {})
    phase = status.setdefault("phase", {})
    blocker = status.setdefault("blocker", {})
    metrics = status.setdefault("metrics", {})
    now = now_ms()
    if "run_status" in changes:
        run["status"] = changes.pop("run_status")
    if "phase_name" in changes:
        phase["name"] = changes.pop("phase_name")
        phase["updated_at_ms"] = now
    if "phase_detail" in changes:
        phase["detail"] = changes.pop("phase_detail")
        phase["updated_at_ms"] = now
    if "phase_role" in changes:
        phase["role"] = changes.pop("phase_role")
        phase["updated_at_ms"] = now
    if "blocker_active" in changes:
        blocker["active"] = changes.pop("blocker_active")
    if "blocker_kind" in changes:
        blocker["kind"] = changes.pop("blocker_kind")
    if "blocker_message" in changes:
        blocker["message"] = changes.pop("blocker_message")
    if "blocker_owner_role" in changes:
        blocker["owner_role"] = changes.pop("blocker_owner_role")
    if "metrics" in changes:
        metrics.update(changes.pop("metrics"))
    if "run_started" in changes and changes["run_started"]:
        run["started_at_ms"] = run.get("started_at_ms") or now
        ts["started_at_ms"] = ts.get("started_at_ms") or now
        changes.pop("run_started")
    if "run_completed" in changes and changes["run_completed"]:
        run["completed_at_ms"] = now
        ts["completed_at_ms"] = now
        changes.pop("run_completed")
    if "run_owner" in changes:
        run["owner"] = changes.pop("run_owner")
    if "run_source" in changes:
        run["source"] = changes.pop("run_source")
    if "timestamp_ms" in changes:
        ts["updated_at_ms"] = changes.pop("timestamp_ms")
        run["updated_at_ms"] = ts["updated_at_ms"]
    if changes:
        status.update(changes)
    status["run"]["updated_at_ms"] = now
    status["timestamps"]["updated_at_ms"] = now
    return status


def append_event(
    path: Path,
    event_type: str,
    run_id: str,
    payload: dict[str, Any] | None = None,
    *,
    task_id: str | None = None,
    role: str | None = None,
    repo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with EVENT_LOCK:
        seq = 1
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    lines = handle.readlines()
                if lines:
                    seq = json.loads(lines[-1]).get("seq", 0) + 1
            except Exception:
                with path.open("r", encoding="utf-8") as handle:
                    seq = sum(1 for _ in handle) + 1
        event = {
            "seq": seq,
            "type": event_type,
            "timestamp_ms": now_ms(),
            "timestamp": iso_now(),
            "run_id": run_id,
        }
        if task_id is not None:
            event["task_id"] = task_id
        if role is not None:
            event["role"] = role
        if repo is not None:
            event["repo"] = repo
        if payload:
            event["payload"] = payload
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=False) + "\n")
        
        if _EVENT_BROADCAST_CALLBACK:
            try:
                _EVENT_BROADCAST_CALLBACK(run_id, event_type, event)
            except Exception:
                # Don't let UI broadcast failure mask the persisted event;
                # the on-disk events.jsonl is the source of truth. Log loudly
                # so the broken callback is visible in operator logs.
                logger.warning(
                    "Event broadcast callback failed for run_id=%s event_type=%s",
                    run_id,
                    event_type,
                    exc_info=True,
                )

        return event


def initial_blackboard(
    run_id: str,
    task: dict[str, Any],
    repo: dict[str, Any],
    provider: dict[str, Any],
    engine: dict[str, Any],
    swarm: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task": task,
        "repo": repo,
        "provider": provider,
        "engine": engine,
        "swarm": swarm,
        "task_contract": task.get("task_contract") or {},
        "program_goal": task.get("program_goal") or None,
        "local_goal": task.get("local_goal") or None,
        "dependency_status": task.get("dependency_status") or {},
        "contract_completeness": task.get("contract_completeness") or {},
        "verification_plan": {
            "commands": [str(entry).strip() for entry in _as_list(task.get("verification_commands")) if str(entry).strip()],
        },
        "expected_deliverables": {
            "deliverables": [str(entry).strip() for entry in _as_list(task.get("deliverables")) if str(entry).strip()],
            "target_files": [str(entry).strip() for entry in _as_list(task.get("target_files")) if str(entry).strip()],
            "acceptance_criteria": [str(entry).strip() for entry in _as_list(task.get("acceptance_criteria")) if str(entry).strip()],
        },
        "manager_plan": None,
        "subtasks": [],
        "workers": [],
        "blockers": [],
        "notes": [],
        "artifacts": [],
        "updated_at_ms": now_ms(),
    }


def load_blackboard(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return load_yaml(path)


def save_blackboard(path: Path, blackboard: dict[str, Any]) -> None:
    payload = deepcopy(blackboard)
    payload["updated_at_ms"] = now_ms()
    atomic_write_yaml(path, payload)
    run_dir = path.parent
    run_id = str(payload.get("run_id") or run_dir.name)
    mirror_run_file(run_id, path, "blackboard.yaml")


def update_blackboard(path: Path, updater) -> dict[str, Any]:
    blackboard = load_blackboard(path) or {}
    result = updater(blackboard)
    if result is not None:
        blackboard = result
    save_blackboard(path, blackboard)
    return blackboard


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


def blackboard_markdown(blackboard: dict[str, Any]) -> str:
    def _inline_items(values: Any) -> str:
        values = _as_list(values)
        items = [str(value).strip() for value in values if str(value).strip()]
        return ", ".join(f"`{item}`" for item in items) if items else "_none_"

    lines = [f"# Run Blackboard `{blackboard.get('run_id', '')}`", ""]
    task = blackboard.get("task") or {}
    lines.append(f"- Task: {task.get('title', 'Untitled task')}")
    lines.append(f"- Updated: `{blackboard.get('updated_at_ms', now_ms())}`")
    lines.append("")
    task_contract = blackboard.get("task_contract") or {}
    if task_contract or blackboard.get("program_goal") or blackboard.get("local_goal"):
        lines.append("## Task Contract")
        program_goal = str(blackboard.get("program_goal") or task_contract.get("program_goal") or "").strip()
        local_goal = str(blackboard.get("local_goal") or task_contract.get("local_goal") or "").strip()
        if program_goal:
            lines.append(f"- Program goal: {program_goal}")
        if local_goal:
            lines.append(f"- Local goal: {local_goal}")
        lines.append(f"- In scope: {_inline_items(task_contract.get('in_scope') or task.get('in_scope') or [])}")
        lines.append(f"- Out of scope: {_inline_items(task_contract.get('out_of_scope') or task.get('out_of_scope') or [])}")
        lines.append(f"- Dependencies: {_inline_items(task_contract.get('dependencies') or task.get('dependencies') or [])}")
        lines.append(f"- Deliverables: {_inline_items(task_contract.get('deliverables') or task.get('deliverables') or [])}")
        lines.append(f"- Target files: {_inline_items(task_contract.get('target_files') or task.get('target_files') or [])}")
        lines.append(f"- Verification commands: {_inline_items(blackboard.get('verification_plan', {}).get('commands') or task.get('verification_commands') or [])}")
        lines.append(f"- Acceptance criteria: {_inline_items(task_contract.get('acceptance_criteria') or task.get('acceptance_criteria') or [])}")
        notes_for_agent = str(task_contract.get("notes_for_agent") or task.get("notes_for_agent") or "").strip()
        if notes_for_agent:
            lines.append(f"- Notes for agent: {notes_for_agent}")
        lines.append("")
    dependency_status = blackboard.get("dependency_status") or {}
    if dependency_status:
        lines.append("## Dependency Status")
        lines.append(f"- Declared: {_inline_items(dependency_status.get('declared') or [])}")
        lines.append(f"- Resolved: {_inline_items([item.get('dependency') for item in dependency_status.get('resolved') or []])}")
        lines.append(f"- Unresolved: {_inline_items([item.get('dependency') for item in dependency_status.get('unresolved') or []])}")
        lines.append(f"- Blocked: `{dependency_status.get('blocked', False)}`")
        blocked_reason = str(dependency_status.get("blocked_reason") or "").strip()
        if blocked_reason:
            lines.append(f"- Reason: {blocked_reason}")
        lines.append("")
    verification_plan = blackboard.get("verification_plan") or {}
    if verification_plan:
        lines.append("## Verification Plan")
        lines.append(f"- Commands: {_inline_items(verification_plan.get('commands') or [])}")
        lines.append(f"- Expected deliverables: {_inline_items((blackboard.get('expected_deliverables') or {}).get('deliverables') or [])}")
        lines.append(f"- Expected files: {_inline_items((blackboard.get('expected_deliverables') or {}).get('target_files') or [])}")
        lines.append("")
    plan = blackboard.get("manager_plan")
    if plan:
        lines.append("## Manager Plan")
        lines.append(str(plan).strip())
        lines.append("")
    subtasks = blackboard.get("subtasks") or []
    lines.append(f"## Subtasks ({len(subtasks)})")
    if not subtasks:
        lines.append("- _none_")
    else:
        for subtask in subtasks:
            lines.append(f"- `{subtask.get('id', '')}` {subtask.get('title', '')} - {subtask.get('status', 'pending')}")
    lines.append("")
    workers = blackboard.get("workers") or []
    lines.append(f"## Workers ({len(workers)})")
    if not workers:
        lines.append("- _none_")
    else:
        for worker in workers:
            lines.append(
                f"- `{worker.get('worker_id', '')}` {worker.get('role', '')} - {worker.get('status', 'pending')}"
            )
    lines.append("")
    review_policy = blackboard.get("review_policy") or {}
    if review_policy:
        lines.append("## Review Policy")
        lines.append(f"- Mode: `{review_policy.get('policy', '')}`")
        lines.append(f"- Human review required: `{review_policy.get('human_review_required', False)}`")
        lines.append(f"- Auto-merge requested: `{review_policy.get('auto_merge_requested', False)}`")
        blocker = str(review_policy.get("blocker") or "").strip()
        if blocker:
            lines.append(f"- Blocker: {blocker}")
        lines.append("")
    verification = blackboard.get("verification") or {}
    if verification:
        lines.append("## Verification")
        lines.append(f"- Outcome: `{verification.get('outcome', '')}`")
        lines.append(f"- Review outcome: `{verification.get('review_outcome', '')}`")
        lines.append(f"- Test outcome: `{verification.get('test_outcome', '')}`")
        lines.append(f"- Retry suggested: `{verification.get('should_retry', False)}`")
        blocker = str(verification.get("validation_blocker") or "").strip()
        if blocker:
            lines.append(f"- Blocker: {blocker}")
        lines.append("")
    blockers = blackboard.get("blockers") or []
    lines.append(f"## Blockers ({len(blockers)})")
    if not blockers:
        lines.append("- _none_")
    else:
        for blocker in blockers:
            lines.append(f"- {blocker.get('kind', 'blocker')}: {blocker.get('message', '')}")
    return "\n".join(lines).rstrip() + "\n"
