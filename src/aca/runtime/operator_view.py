from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from src.aca.config.config_types import ResolvedConfig
from src.aca.core.coordination.coordination import CoordinationStore, DEFAULT_WORKER_STALE_AFTER_SECONDS
from src.aca.core.scheduling.coder_supervisor import list_active_coder_runs
from src.aca.runtime.runstate import load_blackboard, load_status
from src.aca.runtime.workspace_registry import workspace_view
from src.aca.utils.utils import now_ms


def _is_run_directory(run_dir: Path) -> bool:
    if not run_dir.is_dir():
        return False
    name = run_dir.name
    if name in {"state", "browser-tests"}:
        return False
    if name.startswith(("_", ".")):
        return False
    if not (name.startswith("run-") or name.startswith("qa-") or name.startswith("bak-run-")):
        return False
    return (run_dir / "status.json").exists() or (run_dir / "blackboard.yaml").exists()


def _run_summary(run_dir: Path) -> str | None:
    summary_path = run_dir / "summary.md"
    if not summary_path.exists():
        return None
    try:
        return summary_path.read_text(encoding="utf-8")
    except Exception:
        return None


def _persisted_run_status(status_payload: Mapping[str, Any] | None) -> str:
    run_meta = status_payload.get("run") if isinstance(status_payload, dict) else {}
    return str(run_meta.get("status") or "").strip().lower()


def _persisted_run_is_active(status_payload: Mapping[str, Any] | None) -> bool:
    return _persisted_run_status(status_payload) in {"created", "running"}


def _persisted_run_error(status_payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(status_payload, dict):
        return None
    run_meta = status_payload.get("run")
    if isinstance(run_meta, dict):
        error = run_meta.get("error")
        if error:
            return str(error)
    blocker = status_payload.get("blocker")
    if isinstance(blocker, dict) and blocker.get("active") and blocker.get("message"):
        return str(blocker.get("message"))
    return None


def build_run_snapshot(run_id: str, run_dir: Path) -> dict[str, Any]:
    status_payload = load_status(run_dir / "status.json") if run_dir.exists() else {}
    run_meta = status_payload.get("run") if isinstance(status_payload, dict) else {}
    task_meta = status_payload.get("task") if isinstance(status_payload, dict) else {}
    repo_meta = status_payload.get("repo") if isinstance(status_payload, dict) else {}
    phase_meta = status_payload.get("phase") if isinstance(status_payload, dict) else {}
    blocker_meta = status_payload.get("blocker") if isinstance(status_payload, dict) else {}
    github_mcp = status_payload.get("github_mcp") if isinstance(status_payload, dict) else {}
    blackboard = load_blackboard(run_dir / "blackboard.yaml") if run_dir.exists() else {}
    task_contract = {}
    program_goal = None
    local_goal = None
    dependency_status = {}
    verification_plan = {}
    expected_deliverables = {}
    if isinstance(task_meta, dict):
        task_contract = dict(task_meta.get("task_contract") or {})
        program_goal = task_meta.get("program_goal") or task_contract.get("program_goal")
        local_goal = task_meta.get("local_goal") or task_contract.get("local_goal")
        dependency_status = task_meta.get("dependency_status") or {}
    if isinstance(blackboard, dict):
        task_contract = dict(task_contract or blackboard.get("task_contract") or {})
        program_goal = program_goal or blackboard.get("program_goal")
        local_goal = local_goal or blackboard.get("local_goal")
        dependency_status = dependency_status or blackboard.get("dependency_status") or {}
        verification_plan = blackboard.get("verification_plan") or {}
        expected_deliverables = blackboard.get("expected_deliverables") or {}

    project_slug = "unknown"
    task_repo = task_meta.get("repo") if isinstance(task_meta, dict) else None
    if isinstance(task_repo, dict):
        project_slug = str(
            task_repo.get("slug")
            or task_repo.get("repo_slug")
            or task_repo.get("path")
            or project_slug
        ).strip() or project_slug
    if project_slug == "unknown" and isinstance(repo_meta, dict):
        project_slug = str(
            repo_meta.get("slug")
            or repo_meta.get("remote")
            or repo_meta.get("path")
            or project_slug
        ).strip() or project_slug

    is_running = _persisted_run_is_active(status_payload)
    error = _persisted_run_error(status_payload)
    if not run_dir.exists():
        summary_available = False
    else:
        summary_available = _run_summary(run_dir) is not None

    branch_name = None
    if isinstance(repo_meta, dict):
        branch_name = repo_meta.get("branch") or repo_meta.get("branch_name")
    pull_request = None
    execution_backend = None
    review_policy = None
    blockers = []
    if isinstance(blackboard, dict):
        pull_request = blackboard.get("pull_request")
        execution_backend = blackboard.get("execution_backend")
        review_policy = blackboard.get("review_policy")
        blockers = blackboard.get("blockers") or []
        coder_supervision = blackboard.get("coder_supervision") or {}
    else:
        coder_supervision = {}

    if not run_dir.exists():
        return {
            "run_id": run_id,
            "project_slug": project_slug,
            "task_key": task_meta.get("task_key") if isinstance(task_meta, dict) else None,
            "title": task_meta.get("title") if isinstance(task_meta, dict) else None,
            "program_goal": program_goal,
            "local_goal": local_goal,
            "task_contract": task_contract,
            "dependency_status": dependency_status,
            "verification_plan": verification_plan,
            "expected_deliverables": expected_deliverables,
            "status": run_meta.get("status") if isinstance(run_meta, dict) else None,
            "phase": phase_meta if isinstance(phase_meta, dict) else {},
            "branch": branch_name,
            "pull_request": pull_request,
            "execution_backend": execution_backend,
            "github_mcp": github_mcp if isinstance(github_mcp, dict) else {},
            "review_policy": review_policy if isinstance(review_policy, dict) else {},
            "coder_supervision": coder_supervision if isinstance(coder_supervision, dict) else {},
            "blockers": blockers if isinstance(blockers, list) else [],
            "blocker": blocker_meta if isinstance(blocker_meta, dict) else {},
            "updated_at_ms": run_meta.get("updated_at_ms") if isinstance(run_meta, dict) else None,
            "created_at_ms": run_meta.get("created_at_ms") if isinstance(run_meta, dict) else None,
            "is_running": is_running,
            "has_error": bool(error),
            "error": error,
            "summary_available": summary_available,
            "artifacts": {},
            "blackboard": blackboard if isinstance(blackboard, dict) else {},
        }

    return {
        "run_id": run_id,
        "project_slug": project_slug,
        "task_key": task_meta.get("task_key") if isinstance(task_meta, dict) else None,
        "title": task_meta.get("title") if isinstance(task_meta, dict) else None,
        "program_goal": program_goal,
        "local_goal": local_goal,
        "task_contract": task_contract,
        "dependency_status": dependency_status,
        "verification_plan": verification_plan,
        "expected_deliverables": expected_deliverables,
        "status": run_meta.get("status") if isinstance(run_meta, dict) else None,
        "phase": phase_meta if isinstance(phase_meta, dict) else {},
        "branch": branch_name,
        "pull_request": pull_request,
        "execution_backend": execution_backend,
        "github_mcp": github_mcp if isinstance(github_mcp, dict) else {},
        "review_policy": review_policy if isinstance(review_policy, dict) else {},
        "coder_supervision": coder_supervision if isinstance(coder_supervision, dict) else {},
        "blockers": blockers if isinstance(blockers, list) else [],
        "blocker": blocker_meta if isinstance(blocker_meta, dict) else {},
        "updated_at_ms": run_meta.get("updated_at_ms") if isinstance(run_meta, dict) else None,
        "created_at_ms": run_meta.get("created_at_ms") if isinstance(run_meta, dict) else None,
        "is_running": is_running,
        "has_error": bool(error),
        "error": error,
        "summary_available": summary_available,
        "artifacts": {
            "run_dir": str(run_dir),
            "logs_dir": str(run_dir / "logs"),
            "artifacts_dir": str(run_dir / "artifacts"),
            "summary_md": str(run_dir / "summary.md"),
            "status_json": str(run_dir / "status.json"),
            "blackboard_yaml": str(run_dir / "blackboard.yaml"),
        },
        "blackboard": blackboard if isinstance(blackboard, dict) else {},
    }


def list_run_snapshots(cfg: ResolvedConfig, *, limit: int = 25) -> list[dict[str, Any]]:
    output_root = cfg.output_root()
    snapshots: dict[str, dict[str, Any]] = {}
    if output_root.exists():
        for run_dir in output_root.iterdir():
            if not _is_run_directory(run_dir):
                continue
            snapshots[run_dir.name] = build_run_snapshot(run_dir.name, run_dir)
    return sorted(
        snapshots.values(),
        key=lambda item: item.get("updated_at_ms") or item.get("created_at_ms") or 0,
        reverse=True,
    )[: max(1, int(limit or 1))]


def _joined_blocked_reason(task: Mapping[str, Any], run_snapshot: Mapping[str, Any] | None) -> str | None:
    status = str(task.get("status") or task.get("state") or "").strip().lower()
    if status not in {"blocked", "stale"} and not (run_snapshot and run_snapshot.get("has_error")):
        return None
    if run_snapshot:
        error = str(run_snapshot.get("error") or "").strip()
        if error:
            return error
        blocker = run_snapshot.get("blocker")
        if isinstance(blocker, dict):
            message = str(blocker.get("message") or "").strip()
            if message:
                return message
        blockers = run_snapshot.get("blockers")
        if isinstance(blockers, list) and blockers:
            first = blockers[0]
            if isinstance(first, dict):
                message = str(first.get("message") or "").strip()
                if message:
                    return message
                kind = str(first.get("kind") or "blocker").strip()
                return kind
            return str(first)
    return status or None


def _workspace_runs_map(workspace: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for run_ref in workspace.get("runs", []) if isinstance(workspace, dict) else []:
        if isinstance(run_ref, dict):
            run_id = str(run_ref.get("run_id") or "").strip()
            if run_id:
                result[run_id] = dict(run_ref)
    return result


def build_operator_summary(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    workspace_view_data = workspace_view(cfg.root_dir, cfg)
    snapshot = store.snapshot(limit=max(1, int(limit or 1)))
    run_snapshots = list_run_snapshots(cfg, limit=limit)
    coder_runs = list_active_coder_runs(cfg, limit=limit)
    run_by_id = {str(run.get("run_id") or "").strip(): run for run in run_snapshots if str(run.get("run_id") or "").strip()}
    workspace_runs = _workspace_runs_map(workspace_view_data)
    projects = workspace_view_data.get("projects", [])

    task_views: list[dict[str, Any]] = []
    for task in snapshot.get("tasks", []):
        task_key = str(task.get("task_key") or "").strip()
        ownership = store.task_ownership(task_key) or {}
        lease = ownership.get("lease") if isinstance(ownership, dict) else None
        run_coord = ownership.get("run") if isinstance(ownership, dict) else None
        worker = ownership.get("worker") if isinstance(ownership, dict) else None
        claimed_run_id = str((run_coord or {}).get("run_id") or task.get("claimed_run_id") or "").strip()
        workspace_run = workspace_runs.get(claimed_run_id) if claimed_run_id else None
        run_file = run_by_id.get(claimed_run_id) if claimed_run_id else None
        blocked_reason = (
            (ownership or {}).get("blocked_reason")
            or _joined_blocked_reason(task, run_file or run_coord)
            or (task.get("dependency_status") or {}).get("blocked_reason")
            or (task.get("contract_completeness") or {}).get("blocker_message")
        )
        task_views.append(
            {
                "task_key": task_key,
                "task_id": task.get("task_id"),
                "title": task.get("title"),
                "program_goal": task.get("program_goal"),
                "local_goal": task.get("local_goal"),
                "source_type": task.get("source_type"),
                "source_ref": task.get("source_ref"),
                "repo_slug": task.get("repo_slug"),
                "repo_path": task.get("repo_path"),
                "board_path": task.get("board_path"),
                "status": task.get("status"),
                "state": task.get("state"),
                "task_contract": task.get("task_contract"),
                "dependency_status": task.get("dependency_status"),
                "contract_completeness": task.get("contract_completeness"),
                "verification_commands": task.get("verification_commands"),
                "target_files": task.get("target_files"),
                "deliverables": task.get("deliverables"),
                "claimed_run_id": claimed_run_id or None,
                "claimed_lease_id": (lease or {}).get("lease_id") if isinstance(lease, dict) else task.get("claimed_lease_id"),
                "claimed_by": task.get("claimed_by"),
                "claimed_host_id": task.get("claimed_host_id"),
                "lease_expires_at_ms": task.get("lease_expires_at_ms"),
                "ownership_state": (ownership or {}).get("ownership_state"),
                "ownership": ownership,
                "blocked_reason": blocked_reason,
                "lease": lease,
                "worker": worker,
                "run": run_coord,
                "run_snapshot": run_file,
                "workspace_run": workspace_run,
                "github_sync_state": (run_file or {}).get("github_mcp", {}),
                "execution_backend": (run_file or workspace_run or {}).get("execution_backend"),
                "admission_role": (workspace_run or {}).get("admission_role"),
                "execution_path": (workspace_run or {}).get("execution_path"),
                "branch": (run_file or run_coord or {}).get("branch_name") or (run_file or {}).get("branch"),
                "pull_request": (run_file or {}).get("pull_request"),
                "project_id": workspace_run.get("project_id") if isinstance(workspace_run, dict) else None,
            }
        )

    recovery = {
        "stale_tasks": [task for task in snapshot.get("tasks", []) if str(task.get("status") or "").strip().lower() == "stale"],
        "stale_leases": [lease for lease in snapshot.get("leases", []) if str(lease.get("status") or "").strip().lower() == "stale"],
        "stale_workers": [
            worker
            for worker in snapshot.get("workers", [])
            if int(worker.get("last_seen_at_ms") or 0) <= now_ms() - (DEFAULT_WORKER_STALE_AFTER_SECONDS * 1000)
        ],
        "failed_outbox": [entry for entry in snapshot.get("outbox", []) if str(entry.get("status") or "").strip().lower() == "failed"],
    }

    return {
        "workspace": workspace_view_data.get("workspace", {}),
        "projects": workspace_view_data.get("projects", projects),
        "coordination": {
            "backend": snapshot.get("backend"),
            "db_path": snapshot.get("db_path"),
            "summary": snapshot.get("summary", {}),
        },
        "tasks": task_views,
        "runs": run_snapshots,
        "coder_runs": coder_runs,
        "workers": snapshot.get("workers", []),
        "leases": snapshot.get("leases", []),
        "outbox": snapshot.get("outbox", []),
        "scheduler_events": snapshot.get("scheduler_events", []),
        "recovery": recovery,
        "generated_at_ms": now_ms(),
    }
