from __future__ import annotations

from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.engine.coder_backend import coder_backend_mode
from src.tandem_agents.core.task_contract import classify_task_execution_kind
from src.tandem_agents.core.scheduling.coder_supervisor import task_has_active_coder_run


def _nonempty(value: Any) -> str:
    return str(value or "").strip()


def task_project_key(task: dict[str, Any]) -> str:
    metadata = dict(task.get("metadata") or {})
    source_task = dict(metadata.get("task") or {})
    source = dict(task.get("source") or source_task.get("source") or {})
    source_type = _nonempty(source.get("type")) or _nonempty(task.get("source_type")) or "unknown"
    if source_type == "github_project":
        owner = _nonempty(source.get("owner"))
        project = _nonempty(source.get("project")) or _nonempty(source.get("project_name"))
        if owner or project:
            return f"github_project:{owner}/{project}".strip()
    if source_type == "linear":
        team = _nonempty(source.get("team"))
        project = _nonempty(source.get("project")) or _nonempty(source.get("project_name"))
        if team or project:
            return f"linear:{team}/{project or 'issues'}".strip()
    if source_type in {"kanban_board", "local_backlog"}:
        path = _nonempty(source.get("board_path")) or _nonempty(source.get("path"))
        if path:
            return f"{source_type}:{path}"
    if source_type == "manual":
        source_name = _nonempty(source.get("source_name")) or _nonempty(task.get("project_name"))
        if source_name:
            return f"manual:{source_name}"
    if source_type == "custom":
        source_name = _nonempty(source.get("source_name")) or _nonempty(task.get("project_name"))
        if source_name:
            return f"custom:{source_name}"
    repo = dict(task.get("repo") or source_task.get("repo") or {})
    repo_slug = _nonempty(repo.get("slug")) or _nonempty(task.get("repo_slug"))
    if repo_slug:
        return f"{source_type}:{repo_slug}"
    source_ref = _nonempty(task.get("source_ref")) or _nonempty(source.get("item")) or _nonempty(source.get("url"))
    return f"{source_type}:{source_ref or _nonempty(task.get('task_key')) or 'task'}"


def task_repo_key(task: dict[str, Any]) -> str:
    metadata = dict(task.get("metadata") or {})
    source_task = dict(metadata.get("task") or {})
    repo = dict(task.get("repo") or source_task.get("repo") or {})
    repo_slug = _nonempty(repo.get("slug")) or _nonempty(task.get("repo_slug"))
    repo_path = _nonempty(repo.get("path")) or _nonempty(task.get("repo_path"))
    if repo_slug:
        return repo_slug
    if repo_path:
        return repo_path
    source = dict(task.get("source") or source_task.get("source") or {})
    return _nonempty(source.get("repo_name")) or _nonempty(source.get("repo")) or _nonempty(task.get("task_key")) or "repo"


def task_execution_backend(cfg: ResolvedConfig, task: dict[str, Any]) -> str:
    execution_kind = classify_task_execution_kind(task)
    if execution_kind == "linear_comment":
        return "linear_comment"
    if execution_kind == "github_pr_action":
        return "github_pr_action"
    repo = dict(task.get("repo") or {})
    return coder_backend_mode(cfg, task, repo)


def _normalize_repo_relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    while text.startswith("/"):
        text = text[1:]
    return text


def task_file_scopes(task: dict[str, Any]) -> list[tuple[str, ...]]:
    metadata = dict(task.get("metadata") or {})
    source_task = dict(metadata.get("task") or {})
    scopes: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for container in (task, metadata, source_task):
        paths: list[Any] = []
        if isinstance(container, dict):
            for key in ("files", "target_files"):
                value = container.get(key)
                if isinstance(value, list):
                    paths.extend(value)
        if not paths:
            continue
        for raw_path in paths:
            rel_path = _normalize_repo_relative_path(raw_path)
            if not rel_path:
                continue
            parts = PurePosixPath(rel_path).parts
            if not parts:
                continue
            if parts in seen:
                continue
            seen.add(parts)
            scopes.append(parts)
    return scopes


def _paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return False
    shared = min(len(left), len(right))
    return left[:shared] == right[:shared]


def _scope_mode(task: dict[str, Any]) -> str:
    return "files" if task_file_scopes(task) else "repo"


def _active_state(task: dict[str, Any]) -> bool:
    return str(task.get("state") or task.get("status") or "").strip().lower() in {"claimed", "active", "review"}


def scheduler_snapshot(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    tasks = store.list_tasks(limit=limit)
    queued = [task for task in tasks if str(task.get("state") or task.get("status") or "").strip().lower() == "queued"]
    active = [task for task in tasks if _active_state(task)]
    blocked = [
        {
            "task_key": task.get("task_key"),
            "project_key": task_project_key(task),
            "repo_key": task_repo_key(task),
            "reason": "dependency_blocked" if (task.get("dependency_status") or {}).get("blocked") else "task_blocked",
            "blocked_reason": (task.get("dependency_status") or {}).get("blocked_reason")
            or (task.get("state") or task.get("status")),
            "task": task,
        }
        for task in tasks
        if (
            str(task.get("state") or task.get("status") or "").strip().lower() == "blocked"
            or (task.get("dependency_status") or {}).get("blocked")
        )
    ]
    return {
        "policy": cfg.scheduler.policy,
        "limits": {
            "max_active_tasks": cfg.scheduler.max_active_tasks,
            "max_active_tasks_per_project": cfg.scheduler.max_active_tasks_per_project,
            "max_active_tasks_per_repo": cfg.scheduler.max_active_tasks_per_repo,
            "queue_depth_limit": cfg.scheduler.queue_depth_limit,
        },
        "queued_tasks": len(queued),
        "active_tasks": len(active),
        "queued": queued,
        "active": active,
        "blocked_tasks": blocked,
    }


def plan_task_admissions(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    queued = store.list_tasks(state="queued", limit=max(1, int(limit or cfg.scheduler.queue_depth_limit)))
    active = store.list_tasks(limit=max(1, int(limit or cfg.scheduler.queue_depth_limit)))
    active = [task for task in active if _active_state(task)]

    active_by_project: dict[str, int] = defaultdict(int)
    active_by_repo: dict[str, int] = defaultdict(int)
    active_repo_locked: dict[str, bool] = defaultdict(bool)
    active_scopes_by_repo: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for task in active:
        project_key = task_project_key(task)
        repo_key = task_repo_key(task)
        active_by_project[project_key] += 1
        active_by_repo[repo_key] += 1
        task_scopes = task_file_scopes(task)
        if task_scopes:
            active_scopes_by_repo[repo_key].extend(task_scopes)
        else:
            active_repo_locked[repo_key] = True

    grouped: dict[str, deque[dict[str, Any]]] = {}
    blocked: list[dict[str, Any]] = []
    for task in queued:
        blocked_reason = None
        dependency_status = task.get("dependency_status") or {}
        if dependency_status.get("blocked"):
            blocked_reason = {
                "reason": "dependency_blocked",
                "blocked_reason": dependency_status.get("blocked_reason"),
            }
        if blocked_reason is not None:
            blocked.append(
                {
                    "task_key": task.get("task_key"),
                    "project_key": task_project_key(task),
                    "repo_key": task_repo_key(task),
                    "reason": blocked_reason["reason"],
                    "blocked_reason": blocked_reason.get("blocked_reason"),
                    "scope_mode": _scope_mode(task),
                    "task": task,
                }
            )
            continue
        if task_has_active_coder_run(cfg, task):
            blocked.append(
                {
                    "task_key": task.get("task_key"),
                    "project_key": task_project_key(task),
                    "repo_key": task_repo_key(task),
                    "reason": "coder_run_active",
                    "scope_mode": _scope_mode(task),
                    "task": task,
                }
            )
            continue
        grouped.setdefault(task_project_key(task), deque()).append(task)

    admitted: list[dict[str, Any]] = []
    max_total = max(1, int(cfg.scheduler.max_active_tasks))
    max_per_project = max(1, int(cfg.scheduler.max_active_tasks_per_project))
    max_per_repo = max(1, int(cfg.scheduler.max_active_tasks_per_repo))
    ordered_projects = sorted(grouped.keys())

    while len(admitted) < max_total and any(grouped.values()):
        progressed = False
        for project_key in ordered_projects:
            queue = grouped.get(project_key)
            if not queue:
                continue
            candidate = queue[0]
            repo_key = task_repo_key(candidate)
            candidate_scopes = task_file_scopes(candidate)
            if active_by_project[project_key] >= max_per_project:
                blocked.append(
                    {
                        "task_key": candidate.get("task_key"),
                        "project_key": project_key,
                        "repo_key": repo_key,
                        "reason": "project_capacity_reached",
                    }
                )
                queue.popleft()
                progressed = True
                continue
            if active_by_repo[repo_key] >= max_per_repo:
                blocked.append(
                    {
                        "task_key": candidate.get("task_key"),
                        "project_key": project_key,
                        "repo_key": repo_key,
                        "reason": "repo_capacity_reached",
                    }
                )
                queue.popleft()
                progressed = True
                continue
            if active_repo_locked[repo_key]:
                blocked.append(
                    {
                        "task_key": candidate.get("task_key"),
                        "project_key": project_key,
                        "repo_key": repo_key,
                        "reason": "repo_overlap_reached",
                        "scope_mode": _scope_mode(candidate),
                    }
                )
                queue.popleft()
                progressed = True
                continue
            if not candidate_scopes and active_scopes_by_repo[repo_key]:
                blocked.append(
                    {
                        "task_key": candidate.get("task_key"),
                        "project_key": project_key,
                        "repo_key": repo_key,
                        "reason": "repo_overlap_reached",
                        "scope_mode": "repo",
                    }
                )
                queue.popleft()
                progressed = True
                continue
            if candidate_scopes:
                overlap = False
                for active_scope in active_scopes_by_repo[repo_key]:
                    if any(_paths_overlap(scope, active_scope) for scope in candidate_scopes):
                        overlap = True
                        break
                if overlap:
                    blocked.append(
                        {
                            "task_key": candidate.get("task_key"),
                            "project_key": project_key,
                            "repo_key": repo_key,
                            "reason": "file_overlap_reached",
                            "scope_mode": "files",
                            "scope_paths": ["/".join(parts) for parts in candidate_scopes],
                        }
                    )
                    queue.popleft()
                    progressed = True
                    continue
            admitted.append(
                {
                    "task_key": candidate.get("task_key"),
                    "project_key": project_key,
                    "repo_key": repo_key,
                    "execution_backend": task_execution_backend(cfg, candidate),
                    "scope_mode": _scope_mode(candidate),
                    "scope_paths": ["/".join(parts) for parts in candidate_scopes],
                    "task": candidate,
                }
            )
            active_by_project[project_key] += 1
            active_by_repo[repo_key] += 1
            if candidate_scopes:
                active_scopes_by_repo[repo_key].extend(candidate_scopes)
            else:
                active_repo_locked[repo_key] = True
            queue.popleft()
            progressed = True
            if len(admitted) >= max_total:
                break
        if not progressed:
            break

    snapshot = {
        "policy": cfg.scheduler.policy,
        "limits": {
            "max_active_tasks": max_total,
            "max_active_tasks_per_project": max_per_project,
            "max_active_tasks_per_repo": max_per_repo,
            "queue_depth_limit": cfg.scheduler.queue_depth_limit,
        },
        "queued_tasks": len(queued),
        "active_tasks": len(active),
        "admitted": admitted,
        "blocked": blocked,
        "remaining": [
            {
                "task_key": task.get("task_key"),
                "project_key": task_project_key(task),
                "repo_key": task_repo_key(task),
            }
            for queue in grouped.values()
            for task in list(queue)
        ],
    }
    store.record_scheduler_event(
        "scheduler.plan",
        {
            "policy": snapshot["policy"],
            "limits": snapshot["limits"],
            "queued_tasks": snapshot["queued_tasks"],
            "active_tasks": snapshot["active_tasks"],
            "admitted": [
                {
                    "task_key": item.get("task_key"),
                    "project_key": item.get("project_key"),
                    "repo_key": item.get("repo_key"),
                    "execution_backend": item.get("execution_backend"),
                    "scope_mode": item.get("scope_mode"),
                }
                for item in admitted
            ],
            "blocked": blocked,
        },
    )
    return snapshot
