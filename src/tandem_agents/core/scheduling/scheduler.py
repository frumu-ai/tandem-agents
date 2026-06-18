from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.engine.coder_backend import coder_backend_mode
from src.tandem_agents.core.integrations.linear_mcp import get_mcp_server, linear_mcp_server_name
from src.tandem_agents.core.task_contract import classify_task_execution_kind
from src.tandem_agents.core.scheduling.coder_supervisor import list_active_coder_task_refs


LINEAR_AUTH_CHALLENGE_MAX_AGE_MS = 4 * 60 * 1000


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


def _project_key_filter(project_keys: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    return {_nonempty(key) for key in (project_keys or []) if _nonempty(key)}


def _filter_project_tasks(tasks: list[dict[str, Any]], project_keys: set[str] | list[str] | tuple[str, ...] | None) -> list[dict[str, Any]]:
    keys = _project_key_filter(project_keys)
    if not keys:
        return tasks
    return [task for task in tasks if task_project_key(task) in keys]


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


def _task_source_type(task: dict[str, Any]) -> str:
    metadata = dict(task.get("metadata") or {})
    source_task = dict(metadata.get("task") or {})
    source = dict(task.get("source") or source_task.get("source") or {})
    return (_nonempty(source.get("type")) or _nonempty(task.get("source_type"))).lower()


def _server_auth_url(server: dict[str, Any]) -> str:
    challenge = server.get("last_auth_challenge") or server.get("lastAuthChallenge")
    if isinstance(challenge, dict):
        authorization_url = _nonempty(challenge.get("authorization_url") or challenge.get("authorizationUrl"))
        if authorization_url:
            return authorization_url
    pending = server.get("pending_auth_by_tool") or server.get("pendingAuthByTool")
    if isinstance(pending, dict):
        for value in pending.values():
            if not isinstance(value, dict):
                continue
            authorization_url = _nonempty(value.get("authorization_url") or value.get("authorizationUrl"))
            if authorization_url:
                return authorization_url
    return _nonempty(server.get("authorization_url") or server.get("authorizationUrl"))


def _server_auth_challenge_age_ms(server: dict[str, Any]) -> int | None:
    challenge = server.get("last_auth_challenge") or server.get("lastAuthChallenge")
    candidates: list[Any] = []
    if isinstance(challenge, dict):
        candidates.extend(
            [
                challenge.get("requested_at_ms"),
                challenge.get("requestedAtMs"),
                challenge.get("first_seen_ms"),
                challenge.get("firstSeenMs"),
            ]
        )
    pending = server.get("pending_auth_by_tool") or server.get("pendingAuthByTool")
    if isinstance(pending, dict):
        for value in pending.values():
            if not isinstance(value, dict):
                continue
            candidates.extend(
                [
                    value.get("requested_at_ms"),
                    value.get("requestedAtMs"),
                    value.get("first_seen_ms"),
                    value.get("firstSeenMs"),
                    value.get("last_probe_ms"),
                    value.get("lastProbeMs"),
                ]
            )
    timestamps = []
    for raw in candidates:
        try:
            timestamp = int(raw)
        except (TypeError, ValueError):
            continue
        if timestamp > 0:
            timestamps.append(timestamp)
    if not timestamps:
        return None
    return max(0, int(time.time() * 1000) - max(timestamps))


def _linear_auth_challenge_max_age_ms() -> int:
    raw = os.environ.get("ACA_LINEAR_AUTH_CHALLENGE_MAX_AGE_MS", "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            return LINEAR_AUTH_CHALLENGE_MAX_AGE_MS
    return LINEAR_AUTH_CHALLENGE_MAX_AGE_MS


def _control_panel_public_origin(cfg: ResolvedConfig) -> str:
    env = cfg.env if isinstance(getattr(cfg, "env", None), dict) else {}
    for key in ("TANDEM_CONTROL_PANEL_PUBLIC_URL", "HOSTED_CONTROL_PANEL_PUBLIC_URL", "HOSTED_PUBLIC_URL"):
        value = _nonempty(env.get(key) or os.environ.get(key)).rstrip("/")
        if value:
            return value
    return ""


def _request_linear_auth_url(cfg: ResolvedConfig, server_name: str, *, refresh: bool = False) -> str:
    headers: dict[str, str] = {}
    token = cfg.tandem_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Tandem-Token"] = token
    public_origin = _control_panel_public_origin(cfg)
    if public_origin:
        parsed = urlparse(public_origin)
        headers["Origin"] = public_origin
        headers["X-Forwarded-Proto"] = parsed.scheme or "https"
        headers["X-Forwarded-Host"] = parsed.netloc

    action = "refresh" if refresh else "auth"
    request = Request(
        f"{cfg.tandem.base_url.rstrip('/')}/mcp/{quote(server_name)}/{action}",
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict):
        return _server_auth_url(payload)
    return ""


def _linear_mcp_status_blocker(cfg: ResolvedConfig) -> dict[str, Any] | None:
    if not cfg.linear_mcp.enabled:
        return None
    server_name = linear_mcp_server_name(cfg)
    try:
        server = get_mcp_server(cfg, server_name)
    except Exception as exc:
        return {
            "reason": "linear_mcp_status_unavailable",
            "blocked_reason": f"Could not inspect Linear MCP server '{server_name}': {exc}",
        }
    if not isinstance(server, dict):
        return {
            "reason": "linear_mcp_not_configured",
            "blocked_reason": f"Linear MCP server '{server_name}' is not configured.",
        }
    if bool(server.get("connected")):
        return None
    last_error = _nonempty(server.get("last_error") or server.get("lastError"))
    blocked_reason = last_error or f"Linear MCP server '{server_name}' is not connected."
    authorization_url = _server_auth_url(server)
    auth_kind = _nonempty(server.get("auth_kind") or server.get("authKind")).lower()
    challenge_age_ms = _server_auth_challenge_age_ms(server)
    refresh_auth_url = not authorization_url
    force_refresh = False
    if (
        auth_kind == "oauth"
        and authorization_url
        and challenge_age_ms is not None
        and challenge_age_ms > _linear_auth_challenge_max_age_ms()
    ):
        refresh_auth_url = True
        force_refresh = True
    if auth_kind == "oauth" and refresh_auth_url:
        try:
            authorization_url = _request_linear_auth_url(cfg, server_name, refresh=force_refresh)
        except Exception:
            authorization_url = ""
    return {
        "reason": "linear_mcp_auth_required",
        "blocked_reason": blocked_reason,
        "authorization_url": authorization_url,
    }


def _linear_mcp_admission_blocker(cfg: ResolvedConfig, queued: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not any(_task_source_type(task) == "linear" for task in queued):
        return None
    return _linear_mcp_status_blocker(cfg)


def scheduler_integration_blockers(cfg: ResolvedConfig, *, project_key: str = "") -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if str(cfg.task_source.type or "").strip().lower() == "linear":
        blocker = _linear_mcp_status_blocker(cfg)
        if blocker is not None:
            blockers.append(
                {
                    "source_type": "linear",
                    "project_key": _nonempty(project_key)
                    or task_project_key(
                        {
                            "source": {
                                "type": "linear",
                                "team": cfg.task_source.team,
                                "project": cfg.task_source.project,
                            }
                        }
                    ),
                    **blocker,
                }
            )
    return blockers


def scheduler_snapshot(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int = 100,
    project_keys: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    requested_limit = max(1, int(limit or 1))
    fetch_limit = requested_limit if not _project_key_filter(project_keys) else max(requested_limit, int(cfg.scheduler.queue_depth_limit), 1000)
    tasks = _filter_project_tasks(store.list_tasks(limit=fetch_limit), project_keys)[:requested_limit]
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
    project_keys: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    requested_limit = max(1, int(limit or cfg.scheduler.queue_depth_limit))
    fetch_limit = requested_limit if not _project_key_filter(project_keys) else max(requested_limit, int(cfg.scheduler.queue_depth_limit), 1000)
    queued = _filter_project_tasks(store.list_tasks(state="queued", limit=fetch_limit), project_keys)[:requested_limit]
    all_tasks = store.list_tasks(limit=fetch_limit)
    active_for_capacity = [task for task in all_tasks if _active_state(task)]
    active = _filter_project_tasks(active_for_capacity, project_keys)[:requested_limit]

    active_by_project: dict[str, int] = defaultdict(int)
    active_by_repo: dict[str, int] = defaultdict(int)
    active_repo_locked: dict[str, bool] = defaultdict(bool)
    active_scopes_by_repo: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for task in active_for_capacity:
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
    linear_mcp_blocker = _linear_mcp_admission_blocker(cfg, queued)
    active_coder_runs = list_active_coder_task_refs(cfg)
    active_coder_task_keys = {
        _nonempty(item.get("task_key"))
        for item in active_coder_runs
        if _nonempty(item.get("task_key"))
    }
    active_coder_task_ids = {
        _nonempty(item.get("task_id"))
        for item in active_coder_runs
        if _nonempty(item.get("task_id"))
    }
    for task in queued:
        blocked_reason = None
        dependency_status = task.get("dependency_status") or {}
        if linear_mcp_blocker is not None and _task_source_type(task) == "linear":
            blocked.append(
                {
                    "task_key": task.get("task_key"),
                    "project_key": task_project_key(task),
                    "repo_key": task_repo_key(task),
                    "reason": linear_mcp_blocker["reason"],
                    "blocked_reason": linear_mcp_blocker.get("blocked_reason"),
                    "authorization_url": linear_mcp_blocker.get("authorization_url"),
                    "scope_mode": _scope_mode(task),
                    "task": task,
                }
            )
            continue
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
        task_key = _nonempty(task.get("task_key"))
        task_id = _nonempty(task.get("task_id"))
        if (task_key and task_key in active_coder_task_keys) or (task_id and task_id in active_coder_task_ids):
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
    max_active_total = max(1, int(cfg.scheduler.max_active_tasks))
    max_worker_runs = max(0, int(cfg.scheduler.max_concurrent_worker_runs))
    max_total = min(max_active_total, max_worker_runs) if max_worker_runs > 0 else max_active_total
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

    if max_worker_runs > 0 and len(admitted) >= max_worker_runs:
        for queue in grouped.values():
            for task in list(queue):
                task_scopes = task_file_scopes(task)
                blocked.append(
                    {
                        "task_key": task.get("task_key"),
                        "project_key": task_project_key(task),
                        "repo_key": task_repo_key(task),
                        "reason": "worker_concurrency_reached",
                        "scope_mode": _scope_mode(task),
                        "scope_paths": ["/".join(parts) for parts in task_scopes],
                    }
                )
            queue.clear()

    snapshot = {
        "policy": cfg.scheduler.policy,
        "limits": {
            "max_active_tasks": max_total,
            "max_concurrent_worker_runs": max_worker_runs,
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
