from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.aca.cli.monitor import latest_run_dir
from src.aca.config.config import ResolvedConfig, resolve_config, validate_config
from src.aca.core.engine.engine import engine_status_report
from src.aca.core.integrations.github_mcp import (
    get_mcp_server,
    github_mcp_scope,
    github_remote_sync_mode,
)
from src.aca.runtime.runstate import load_status
from src.aca.runtime.workspace_registry import configured_project_binding, load_workspace, workspace_summary


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _root_dir(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    return Path(os.environ.get("ACA_ROOT", _repo_root())).expanduser().resolve()


def _task_source_snapshot(cfg: ResolvedConfig) -> dict[str, Any]:
    return {
        "type": str(cfg.task_source.type or "").strip(),
        "owner": str(cfg.task_source.owner or "").strip(),
        "repo": str(cfg.task_source.repo or "").strip(),
        "project": str(cfg.task_source.project or "").strip(),
        "item": str(cfg.task_source.item or "").strip(),
        "url": str(cfg.task_source.url or "").strip(),
        "path": str(cfg.task_source.path or "").strip(),
        "source_name": str(cfg.task_source.source_name or "").strip(),
        "card_id": str(cfg.task_source.card_id or "").strip(),
    }


def _repository_snapshot(cfg: ResolvedConfig) -> dict[str, Any]:
    return {
        "slug": str(cfg.repository.slug or "").strip(),
        "path": str(cfg.repository_path() or cfg.repository.path or "").strip(),
        "clone_url": str(cfg.repository.clone_url or "").strip(),
        "default_branch": str(cfg.repository.default_branch or "main").strip(),
        "remote_name": str(cfg.repository.remote_name or "origin").strip(),
    }


def _provider_snapshot(cfg: ResolvedConfig) -> dict[str, Any]:
    return {
        "id": str(cfg.provider.id or "").strip(),
        "model": str(cfg.provider.model or "").strip(),
        "base_url": str(cfg.provider.base_url or "").strip(),
        "fallback_provider": str(cfg.provider.fallback_provider or "").strip(),
        "fallback_model": str(cfg.provider.fallback_model or "").strip(),
    }


def _execution_snapshot(cfg: ResolvedConfig) -> dict[str, Any]:
    return {
        "backend": str(cfg.execution.backend or "").strip(),
        "swarm_enabled": bool(cfg.swarm.enabled),
        "swarm_shared_model": bool(cfg.swarm.shared_model),
        "max_workers": int(cfg.swarm.max_workers or 1),
        "review_policy": str(cfg.review.policy or "").strip(),
    }


def _engine_snapshot(cfg: ResolvedConfig) -> dict[str, Any]:
    try:
        report = engine_status_report(cfg)
    except Exception as exc:  # noqa: BLE001
        return {
            "base_url": str(cfg.tandem.base_url or "").strip(),
            "healthy": False,
            "running": False,
            "status": "unreachable",
            "version": None,
            "update_available": False,
            "update_policy": str(cfg.tandem.update_policy or "").strip(),
            "startup_mode": str(cfg.tandem.startup_mode or "").strip(),
            "detail": str(exc).strip(),
        }

    return {
        "base_url": str(report.get("base_url") or cfg.tandem.base_url or "").strip(),
        "healthy": bool(report.get("healthy")),
        "running": bool(report.get("running")),
        "status": str(report.get("status") or "").strip(),
        "version": report.get("version"),
        "update_available": bool(report.get("update_available")),
        "update_policy": str(report.get("update_policy") or cfg.tandem.update_policy or "").strip(),
        "startup_mode": str(report.get("startup_mode") or cfg.tandem.startup_mode or "").strip(),
        "detail": report.get("detail"),
        "api_token_required": bool(report.get("api_token_required")),
    }


def _github_mcp_snapshot(cfg: ResolvedConfig) -> dict[str, Any]:
    enabled = bool(cfg.github_mcp.enabled)
    source_type = str(cfg.task_source.type or "").strip()
    server: dict[str, Any] | None = None
    if enabled:
        try:
            server = get_mcp_server(cfg, "github")
        except Exception:
            server = None
    connected = bool(server and server.get("connected"))
    return {
        "enabled": enabled,
        "connected": connected,
        "scope": github_mcp_scope(cfg, source_type),
        "remote_sync": github_remote_sync_mode(cfg, source_type),
        "transport": str((server or {}).get("transport") or cfg.github_mcp.url or "").strip(),
        "toolsets": str(cfg.github_mcp.toolsets or "").strip(),
    }


def _workspace_snapshot(root: Path, cfg: ResolvedConfig) -> dict[str, Any]:
    workspace = workspace_summary(load_workspace(root))
    configured = configured_project_binding(cfg)
    projects = list(workspace.get("projects") or [])
    active_project_id = str((workspace.get("workspace") or {}).get("active_project_id") or "").strip()
    active_project = next((project for project in projects if str(project.get("id") or "").strip() == active_project_id), None)
    if not active_project:
        active_project = configured
    return {
        "workspace": workspace.get("workspace") or {},
        "summary": workspace.get("summary") or {},
        "active_project": {
            "id": str((active_project or {}).get("id") or "").strip(),
            "name": str((active_project or {}).get("name") or "").strip(),
            "repo": dict((active_project or {}).get("repo") or {}),
            "source": dict((active_project or {}).get("source") or {}),
        },
        "configured_project": {
            "id": str(configured.get("id") or "").strip(),
            "name": str(configured.get("name") or "").strip(),
            "repo": dict(configured.get("repo") or {}),
            "source": dict(configured.get("source") or {}),
        },
    }


def _latest_run_snapshot(cfg: ResolvedConfig) -> dict[str, Any] | None:
    run_dir = latest_run_dir(cfg.output_root())
    if run_dir is None or not run_dir.exists():
        return None
    status_path = run_dir / "status.json"
    status = load_status(status_path) if status_path.exists() else {}
    run_meta = dict(status.get("run") or {})
    task_meta = dict(status.get("task") or {})
    repo_meta = dict(status.get("repo") or {})
    phase_meta = dict(status.get("phase") or {})
    return {
        "run_id": str(run_dir.name or run_meta.get("run_id") or "").strip(),
        "status": str(run_meta.get("status") or "").strip(),
        "phase": str(phase_meta.get("name") or "").strip(),
        "task_title": str(task_meta.get("title") or "").strip(),
        "repo_slug": str(repo_meta.get("slug") or "").strip(),
        "branch": str(repo_meta.get("branch") or "").strip(),
        "summary_available": (run_dir / "summary.md").exists(),
        "is_running": str(run_meta.get("status") or "").strip().lower() in {"created", "running"},
        "artifacts": {
            "run_dir": str(run_dir),
            "status_json": str(status_path),
            "summary_md": str(run_dir / "summary.md"),
            "blackboard_yaml": str(run_dir / "blackboard.yaml"),
        },
    }


def _allowed_next_actions(cfg: ResolvedConfig, *, validation_ok: bool, engine: dict[str, Any], github_mcp: dict[str, Any], latest_run: dict[str, Any] | None) -> list[str]:
    actions: list[str] = []
    repo = _repository_snapshot(cfg)
    if not validation_ok:
        actions.append("fix_configuration")
    if not any(repo.get(key) for key in ("path", "slug", "clone_url")):
        actions.append("bind_repository")
    if not engine.get("healthy"):
        actions.append("check_engine_health")
    source_type = str(cfg.task_source.type or "").strip()
    if source_type == "github_project":
        if github_mcp.get("enabled") and not github_mcp.get("connected"):
            actions.append("connect_github_mcp")
        actions.append("intake_next_github_project_task")
    elif source_type == "kanban_board":
        actions.append("preview_kanban_task")
    elif source_type == "manual":
        actions.append("start_manual_run")
    else:
        actions.append("inspect_task_source")
    if latest_run and latest_run.get("is_running"):
        actions.append("inspect_latest_run")
    actions.extend(["open_agent_docs", "use_local_git_for_edits"])
    seen: set[str] = set()
    deduped: list[str] = []
    for action in actions:
        if action in seen:
            continue
        seen.add(action)
        deduped.append(action)
    return deduped


def build_aca_overview(root: Path | None = None) -> dict[str, Any]:
    root_dir = _root_dir(root)
    cfg = resolve_config(root_dir)
    validation_errors = validate_config(cfg)
    engine = _engine_snapshot(cfg)
    github_mcp = _github_mcp_snapshot(cfg)
    latest_run = _latest_run_snapshot(cfg)
    overview = {
        "summary": (
            f"ACA is {engine.get('status') or 'unknown'} on {engine.get('base_url') or cfg.tandem.base_url} "
            f"with task source `{cfg.task_source.type or 'unset'}` and repo `{cfg.repository.slug or cfg.repository.path or 'unset'}`."
        ),
        "auth": {
            "mode": "bearer_api_key",
            "required": True,
        },
        "validation": {
            "ok": not bool(validation_errors),
            "errors": validation_errors,
        },
        "task_source": _task_source_snapshot(cfg),
        "repository": _repository_snapshot(cfg),
        "provider": _provider_snapshot(cfg),
        "execution": _execution_snapshot(cfg),
        "tandem": {
            "base_url": str(cfg.tandem.base_url or "").strip(),
            "startup_mode": str(cfg.tandem.startup_mode or "").strip(),
            "update_policy": str(cfg.tandem.update_policy or "").strip(),
            "api_token_source": "ACA_API_TOKEN or ACA_API_TOKEN_FILE",
        },
        "engine": engine,
        "github_mcp": github_mcp,
        "workspace": _workspace_snapshot(root_dir, cfg),
        "latest_run": latest_run,
        "allowed_next_actions": _allowed_next_actions(
            cfg,
            validation_ok=not bool(validation_errors),
            engine=engine,
            github_mcp=github_mcp,
            latest_run=latest_run,
        ),
        "doc_refs": [
            {"title": "GitHub Projects guide", "path": str(_repo_root() / "docs" / "AUTONOMOUS_CODING_AGENT_GITHUB_PROJECTS_GUIDE.md")},
            {"title": "Task sources", "path": str(_repo_root() / "docs" / "TASK_SOURCES.md")},
            {"title": "Coding tasks with Tandem", "path": str(_repo_root() / "docs" / "CODING_TASKS_WITH_TANDEM.md")},
            {"title": "ACA README", "path": str(_repo_root() / "README.md")},
            {"title": "Command reference", "path": str(_repo_root() / "docs" / "COMMANDS.md")},
        ],
    }
    return overview
