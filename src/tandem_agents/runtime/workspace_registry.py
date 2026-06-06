from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.utils.utils import atomic_write_yaml, load_yaml, now_ms, short_id, slugify

WORKSPACE_DIR_NAME = ".tandem-agents"
WORKSPACE_FILE_NAME = "workspace.yaml"
PROJECTS_DIR_NAME = "projects"
LEGACY_PROJECTS_FILE = "config/projects.yaml"
DEFAULT_STALE_THRESHOLD_MS = 300_000


def workspace_root(root: Path) -> Path:
    explicit_root = _as_text(os.environ.get("ACA_WORKSPACE_STATE_DIR") or os.environ.get("TANDEM_AGENTS_WORKSPACE_DIR"))
    if explicit_root:
        path = Path(explicit_root).expanduser()
        if not path.is_absolute():
            path = Path(root).expanduser() / path
        return path.resolve()
    output_root = _as_text(os.environ.get("AUTOCODER_OUTPUT_ROOT") or os.environ.get("ACA_OUTPUT_ROOT"))
    if output_root:
        path = Path(output_root).expanduser()
        if not path.is_absolute():
            path = Path(root).expanduser() / path
        return (path / "state" / WORKSPACE_DIR_NAME).resolve()
    return (Path(root).expanduser() / WORKSPACE_DIR_NAME).resolve()


def legacy_workspace_root(root: Path) -> Path:
    return (Path(root).expanduser() / WORKSPACE_DIR_NAME).resolve()


def workspace_file(root: Path) -> Path:
    return workspace_root(root) / WORKSPACE_FILE_NAME


def legacy_workspace_state_file(root: Path) -> Path:
    return legacy_workspace_root(root) / WORKSPACE_FILE_NAME


def projects_dir(root: Path) -> Path:
    return workspace_root(root) / PROJECTS_DIR_NAME


def legacy_projects_file(root: Path) -> Path:
    return Path(root).expanduser() / LEGACY_PROJECTS_FILE


def _project_file_stem(project_id: str) -> str:
    raw = str(project_id or "").strip() or "project"
    slug = slugify(raw, limit=48)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def project_file(root: Path, project_id: str) -> Path:
    return projects_dir(root) / f"{_project_file_stem(project_id)}.yaml"


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unwrap(payload: Any, key: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
        return payload
    return {}


def _normalize_repo(repo: Any, *, fallback_slug: str = "") -> dict[str, Any]:
    data = repo if isinstance(repo, dict) else {}
    repo_url = _as_text(data.get("clone_url") or data.get("repo_url") or data.get("url"))
    slug = _as_text(data.get("slug"), fallback_slug)
    if not slug and repo_url:
        slug = _infer_slug_from_repo_url(repo_url) or fallback_slug
    return {
        "slug": slug,
        "default_branch": _as_text(data.get("default_branch"), "main"),
        "path": _as_text(data.get("path")),
        "remote_name": _as_text(data.get("remote_name"), "origin"),
        "worktree_root": _as_text(data.get("worktree_root")),
        "credential_file": _as_text(data.get("credential_file") or data.get("token_file")),
        "clone_url": repo_url,
    }


def _normalize_source(source: Any) -> dict[str, Any]:
    data = source if isinstance(source, dict) else {}
    return {
        "type": _as_text(data.get("type"), "manual"),
        "owner": _as_text(data.get("owner")),
        "repo": _as_text(data.get("repo")),
        "team": _as_text(data.get("team")),
        "project": _as_text(data.get("project")),
        "statuses": _as_text(data.get("statuses")),
        "labels": _as_text(data.get("labels")),
        "query": _as_text(data.get("query")),
        "item": _as_text(data.get("item")),
        "url": _as_text(data.get("url")),
        "path": _as_text(data.get("path")),
        "card_id": _as_text(data.get("card_id")),
        "payload": deepcopy(data.get("payload") if isinstance(data.get("payload"), dict) else {}),
    }


def _normalize_snapshot(snapshot: Any, *, project_id: str, source_type: str) -> dict[str, Any]:
    data = snapshot if isinstance(snapshot, dict) else {}
    captured_at_ms = data.get("captured_at_ms")
    stale_threshold_ms = data.get("stale_threshold_ms")
    return {
        "id": _as_text(data.get("id"), f"{project_id}-snapshot-{short_id()}"),
        "project_id": project_id,
        "source_type": _as_text(data.get("source_type"), source_type),
        "columns": list(data.get("columns") or []),
        "cards": list(data.get("cards") or []),
        "captured_at_ms": _as_int(captured_at_ms, now_ms()) or now_ms(),
        "stale_threshold_ms": _as_int(stale_threshold_ms, DEFAULT_STALE_THRESHOLD_MS) or DEFAULT_STALE_THRESHOLD_MS,
    }


def _normalize_run_ref(run_ref: Any) -> dict[str, Any]:
    if isinstance(run_ref, dict):
        return {
            "run_id": _as_text(run_ref.get("run_id") or run_ref.get("id")),
            "project_id": _as_text(run_ref.get("project_id")),
            "project_key": _as_text(run_ref.get("project_key")),
            "status": _as_text(run_ref.get("status")),
            "phase": _as_text(run_ref.get("phase")),
            "execution_backend": _as_text(run_ref.get("execution_backend")),
            "admission_role": _as_text(run_ref.get("admission_role")),
            "execution_path": _as_text(run_ref.get("execution_path")),
            "task_key": _as_text(run_ref.get("task_key")),
            "task_title": _as_text(run_ref.get("task_title")),
            "created_at_ms": _as_int(run_ref.get("created_at_ms"), now_ms()) or now_ms(),
            "updated_at_ms": _as_int(run_ref.get("updated_at_ms"), now_ms()) or now_ms(),
        }
    return {
        "run_id": _as_text(run_ref),
        "project_id": "",
        "project_key": "",
        "status": "",
        "phase": "",
        "execution_backend": "",
        "admission_role": "",
        "execution_path": "",
        "task_key": "",
        "task_title": "",
        "created_at_ms": now_ms(),
        "updated_at_ms": now_ms(),
    }


def _normalize_project_binding(project: Any, *, fallback_id: str = "") -> dict[str, Any]:
    data = _unwrap(project, "project")
    project_id = _as_text(data.get("id") or data.get("slug") or fallback_id, fallback_id or "project")
    source = _normalize_source(data.get("source") or data.get("task_source"))
    repo = _normalize_repo(data.get("repo"), fallback_slug=_as_text(data.get("repo_slug") or project_id))
    if not repo["slug"]:
        repo["slug"] = _infer_slug_from_repo_url(repo["clone_url"]) or project_id
    name = _as_text(data.get("name"), project_id)
    created_at_ms = data.get("created_at_ms")
    updated_at_ms = data.get("updated_at_ms")
    last_refresh_ms = data.get("last_refresh_ms")
    sync_state = _as_text(data.get("sync_state"), "unknown")
    source_type = source["type"] or "manual"
    return {
        "id": project_id,
        "name": name,
        "repo": repo,
        "source": source,
        "snapshot": _normalize_snapshot(data.get("snapshot"), project_id=project_id, source_type=source_type),
        "sync_state": sync_state,
        "last_refresh_ms": _as_int(last_refresh_ms, now_ms()) or now_ms(),
        "created_at_ms": _as_int(created_at_ms, now_ms()) or now_ms(),
        "updated_at_ms": _as_int(updated_at_ms, now_ms()) or now_ms(),
        "repo_url": repo.get("clone_url", ""),
        "implicit": bool(data.get("implicit")),
    }


def _normalize_workspace(workspace: Any) -> dict[str, Any]:
    data = _unwrap(workspace, "workspace")
    projects = []
    for entry in data.get("projects") or []:
        projects.append(_normalize_project_binding(entry))
    runs = [_normalize_run_ref(entry) for entry in (data.get("runs") or [])]
    created_at_ms = data.get("created_at_ms")
    updated_at_ms = data.get("updated_at_ms")
    return {
        "id": _as_text(data.get("id"), f"workspace-{short_id()}"),
        "name": _as_text(data.get("name"), "Tandem Agents Workspace"),
        "created_at_ms": int(created_at_ms) if created_at_ms not in (None, "") else now_ms(),
        "updated_at_ms": int(updated_at_ms) if updated_at_ms not in (None, "") else now_ms(),
        "projects": projects,
        "runs": runs,
        "active_project_id": _as_text(data.get("active_project_id")) or None,
    }


def _infer_slug_from_repo_url(repo_url: str) -> str:
    text = _as_text(repo_url)
    if not text:
        return ""
    path = text
    if "://" in text:
        from urllib.parse import urlparse

        path = urlparse(text).path
    elif ":" in text and "@" in text:
        path = text.split(":", 1)[1]
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return "/".join(parts)


def default_workspace(root: Path) -> dict[str, Any]:
    return {
        "workspace": {
            "id": f"workspace-{short_id()}",
            "name": Path(root).expanduser().name or "Tandem Agents Workspace",
            "created_at_ms": now_ms(),
            "updated_at_ms": now_ms(),
            "projects": [],
            "runs": [],
            "active_project_id": None,
        }
    }


def configured_project_id(cfg: ResolvedConfig) -> str:
    candidates = [
        getattr(cfg.repository, "slug", ""),
        getattr(cfg.task_source, "repo", ""),
        getattr(cfg.task_source, "team", ""),
        Path(str(cfg.repository_path() or "")).name if cfg.repository_path() else "",
        Path(str(cfg.task_source_path() or "")).stem if cfg.task_source_path() else "",
        "configured-project",
    ]
    for raw in candidates:
        text = _as_text(raw)
        if text:
            return text
    return "configured-project"


def configured_project_binding(cfg: ResolvedConfig) -> dict[str, Any]:
    project_id = configured_project_id(cfg)
    repo_slug = _as_text(cfg.repository.slug) or _infer_slug_from_repo_url(cfg.repository.clone_url) or project_id
    task_source = cfg.task_source
    binding = {
        "id": project_id,
        "name": _as_text(getattr(task_source, "source_name", "")) or _as_text(cfg.repository.slug) or project_id,
        "repo": {
            "slug": repo_slug,
            "default_branch": _as_text(cfg.repository.default_branch, "main"),
            "path": _as_text(str(cfg.repository_path() or cfg.repository.path)),
            "remote_name": _as_text(cfg.repository.remote_name, "origin"),
            "worktree_root": _as_text(cfg.repository.worktree_root),
            "credential_file": _as_text(cfg.repository.credential_file),
            "clone_url": _as_text(cfg.repository.clone_url),
        },
        "source": {
            "type": _as_text(getattr(task_source, "type", ""), "manual"),
            "owner": _as_text(getattr(task_source, "owner", "")),
            "repo": _as_text(getattr(task_source, "repo", "")),
            "team": _as_text(getattr(task_source, "team", "")),
            "project": _as_text(getattr(task_source, "project", "")),
            "statuses": _as_text(getattr(task_source, "statuses", "")),
            "labels": _as_text(getattr(task_source, "labels", "")),
            "query": _as_text(getattr(task_source, "query", "")),
            "item": _as_text(getattr(task_source, "item", "")),
            "url": _as_text(getattr(task_source, "url", "")),
            "path": _as_text(getattr(task_source, "path", "")),
            "card_id": _as_text(getattr(task_source, "card_id", "")),
            "payload": deepcopy(getattr(task_source, "payload", {}) or {}),
        },
        "snapshot": _normalize_snapshot({}, project_id=project_id, source_type=_as_text(getattr(task_source, "type", ""), "manual")),
        "sync_state": "unknown",
        "last_refresh_ms": now_ms(),
        "created_at_ms": now_ms(),
        "updated_at_ms": now_ms(),
        "repo_url": _as_text(cfg.repository.clone_url),
        "implicit": True,
    }
    return _normalize_project_binding(binding, fallback_id=project_id)


def project_binding_from_compat(project_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(record)
    repo = data.get("repo") if isinstance(data.get("repo"), dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else data.get("task_source")
    if not isinstance(source, dict):
        source = {}
    if not isinstance(repo, dict):
        repo = {}
    repo_url = _as_text(data.get("repo_url") or repo.get("clone_url") or repo.get("repo_url") or data.get("clone_url"))
    repo_slug = _as_text(repo.get("slug") or data.get("repo_slug") or data.get("slug"))
    if not repo_slug and repo_url:
        repo_slug = _infer_slug_from_repo_url(repo_url)
    binding = {
        "id": _as_text(data.get("id"), project_id),
        "name": _as_text(data.get("name"), project_id),
        "repo": {
            "slug": repo_slug or project_id,
            "default_branch": _as_text(repo.get("default_branch"), "main"),
            "path": _as_text(repo.get("path")),
            "remote_name": _as_text(repo.get("remote_name"), "origin"),
            "worktree_root": _as_text(repo.get("worktree_root") or data.get("worktree_root")),
            "credential_file": _as_text(
                repo.get("credential_file")
                or repo.get("token_file")
                or data.get("credential_file")
                or data.get("token_file")
            ),
            "clone_url": repo_url,
        },
        "source": _normalize_source(source),
        "snapshot": data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {},
        "sync_state": _as_text(data.get("sync_state"), "unknown"),
        "last_refresh_ms": data.get("last_refresh_ms"),
        "created_at_ms": data.get("created_at_ms"),
        "updated_at_ms": data.get("updated_at_ms"),
        "repo_url": repo_url,
        "implicit": bool(data.get("implicit")),
    }
    return _normalize_project_binding(binding, fallback_id=project_id)


def project_binding_to_compat(binding: Mapping[str, Any]) -> dict[str, Any]:
    project = _normalize_project_binding(binding)
    payload = {
        "id": project["id"],
        "slug": project["id"],
        "name": project["name"],
        "repo_url": project["repo"].get("clone_url", "") or project.get("repo_url", ""),
        "repo": deepcopy(project["repo"]),
        "task_source": deepcopy(project["source"]),
        "source": deepcopy(project["source"]),
        "snapshot": deepcopy(project["snapshot"]),
        "sync_state": project["sync_state"],
        "last_refresh_ms": project["last_refresh_ms"],
        "created_at_ms": project["created_at_ms"],
        "updated_at_ms": project["updated_at_ms"],
        "implicit": bool(project.get("implicit")),
    }
    return payload


def workspace_projects_map(workspace: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    record = _normalize_workspace(workspace)
    return {project["id"]: project_binding_to_compat(project) for project in record["projects"]}


def workspace_summary(workspace: Mapping[str, Any]) -> dict[str, Any]:
    record = _normalize_workspace(workspace)
    projects = record["projects"]
    runs = record["runs"]
    active_project_id = record.get("active_project_id")
    return {
        "workspace": {
            "id": record["id"],
            "name": record["name"],
            "created_at_ms": record["created_at_ms"],
            "updated_at_ms": record["updated_at_ms"],
            "active_project_id": active_project_id,
        },
        "summary": {
            "project_count": len(projects),
            "run_count": len(runs),
            "active_project_id": active_project_id,
        },
        "projects": [project_binding_to_compat(project) for project in projects],
        "runs": deepcopy(runs),
    }


def workspace_view(root: Path, cfg: ResolvedConfig | None = None) -> dict[str, Any]:
    record = load_workspace(root)
    if cfg is None:
        return workspace_summary(record)
    configured = configured_project_binding(cfg)
    projects = list(record["workspace"]["projects"])
    if not any(str(project.get("id")) == str(configured["id"]) for project in projects):
        projects = [configured] + projects
    active_project_id = record["workspace"].get("active_project_id")
    if not active_project_id and projects:
        active_project_id = projects[0]["id"]
    view = {
        "workspace": {
            **deepcopy(record["workspace"]),
            "active_project_id": active_project_id,
            "projects": projects,
        }
    }
    return workspace_summary(view)


def load_workspace(root: Path) -> dict[str, Any]:
    path = workspace_file(root)
    if path.exists():
        return {"workspace": _normalize_workspace(load_yaml(path))}
    legacy_state = legacy_workspace_state_file(root)
    if legacy_state.exists() and legacy_state.resolve() != path.resolve():
        loaded = {"workspace": _normalize_workspace(load_yaml(legacy_state))}
        save_workspace(root, loaded)
        return loaded
    legacy = legacy_projects_file(root)
    if legacy.exists():
        loaded = load_yaml(legacy)
        if isinstance(loaded, dict) and loaded:
            workspace = default_workspace(root)
            for project_id, record in loaded.items():
                if not isinstance(record, dict):
                    continue
                workspace["workspace"]["projects"].append(project_binding_from_compat(str(project_id), record))
            if workspace["workspace"]["projects"]:
                workspace["workspace"]["active_project_id"] = workspace["workspace"]["projects"][0]["id"]
            return workspace
    return default_workspace(root)


def save_workspace(root: Path, workspace: Mapping[str, Any]) -> dict[str, Any]:
    record = _normalize_workspace(workspace)
    path = workspace_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at_ms"] = now_ms()
    atomic_write_yaml(path, {"workspace": record})

    expected_files = set()
    projects_path = projects_dir(root)
    projects_path.mkdir(parents=True, exist_ok=True)
    for project in record["projects"]:
        file_path = project_file(root, project["id"])
        atomic_write_yaml(file_path, {"project": project})
        expected_files.add(file_path.resolve())
    for file_path in projects_path.glob("*.yaml"):
        if file_path.resolve() not in expected_files:
            try:
                file_path.unlink()
            except OSError:
                pass
    return record


def get_project(workspace: Mapping[str, Any], project_id: str) -> dict[str, Any] | None:
    record = _normalize_workspace(workspace)
    for project in record["projects"]:
        if str(project.get("id")) == str(project_id):
            return project_binding_to_compat(project)
    return None


def register_project(
    workspace: Mapping[str, Any],
    project: Mapping[str, Any],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    record = _normalize_workspace(workspace)
    new_project = project_binding_from_compat(project_id or _as_text(project.get("id") or project.get("slug")), project)
    projects = record["projects"]
    replaced = False
    for index, existing in enumerate(projects):
        if str(existing.get("id")) == str(new_project["id"]):
            new_project["created_at_ms"] = existing.get("created_at_ms") or new_project["created_at_ms"]
            projects[index] = new_project
            replaced = True
            break
    if not replaced:
        projects.append(new_project)
    if not record.get("active_project_id"):
        record["active_project_id"] = new_project["id"]
    record["updated_at_ms"] = now_ms()
    return {"workspace": record}


def set_active_project(workspace: Mapping[str, Any], project_id: str | None) -> dict[str, Any]:
    record = _normalize_workspace(workspace)
    if project_id:
        if not any(str(project.get("id")) == str(project_id) for project in record["projects"]):
            raise ValueError(f"Project not found: {project_id}")
        record["active_project_id"] = project_id
    else:
        record["active_project_id"] = None
    record["updated_at_ms"] = now_ms()
    return {"workspace": record}


def record_run_reference(
    workspace: Mapping[str, Any],
    *,
    run_id: str,
    project_id: str,
    status: str,
    project_key: str | None = None,
    phase: str | None = None,
    execution_backend: str | None = None,
    admission_role: str | None = None,
    execution_path: str | None = None,
    task_key: str | None = None,
    task_title: str | None = None,
    created_at_ms: int | None = None,
    updated_at_ms: int | None = None,
) -> dict[str, Any]:
    record = _normalize_workspace(workspace)
    existing = next((item for item in record["runs"] if str(item.get("run_id")) == _as_text(run_id)), None)
    run_ref = {
        "run_id": _as_text(run_id),
        "project_id": _as_text(project_id),
        "project_key": _as_text(project_key),
        "status": _as_text(status),
        "phase": _as_text(phase),
        "execution_backend": _as_text(execution_backend),
        "admission_role": _as_text(admission_role),
        "execution_path": _as_text(execution_path),
        "task_key": _as_text(task_key),
        "task_title": _as_text(task_title),
        "created_at_ms": int(created_at_ms) if created_at_ms not in (None, "") else now_ms(),
        "updated_at_ms": int(updated_at_ms) if updated_at_ms not in (None, "") else now_ms(),
    }
    if existing:
        if created_at_ms in (None, "") and existing.get("created_at_ms"):
            run_ref["created_at_ms"] = int(existing["created_at_ms"])
    runs = [item for item in record["runs"] if str(item.get("run_id")) != run_ref["run_id"]]
    runs.append(run_ref)
    record["runs"] = runs
    record["updated_at_ms"] = now_ms()
    return {"workspace": record}
