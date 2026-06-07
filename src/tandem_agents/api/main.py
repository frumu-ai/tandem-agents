import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from src.tandem_agents.api.auth import assert_api_token_configured, get_token
from src.tandem_agents.mcp.app import router as aca_mcp_router
from sse_starlette.sse import EventSourceResponse

from src.tandem_agents.config.config import resolve_config, validate_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.coordination.coordination_reaper import coordination_reaper_interval, coordination_reaper_tick
from src.tandem_agents.core.execution.runtime_entrypoints import run_coordinator
from src.tandem_agents.core.scheduling.scheduler import plan_task_admissions, scheduler_snapshot
from src.tandem_agents.core.scheduling.scheduler_dispatcher import dispatch_scheduled_runs
from src.tandem_agents.core.scheduling.coder_supervisor import (
    list_active_coder_runs,
    reconcile_active_coder_runs,
    reconcile_coder_run,
)
from src.tandem_agents.core.execution.runner_core import run_qa
from src.tandem_agents.core.engine.engine import engine_status_report, resolve_repository
from src.tandem_agents.runtime.operator_dashboard import render_operator_dashboard
from src.tandem_agents.runtime.operator_view import build_operator_summary
from src.tandem_agents.runtime.workspace_registry import (
    configured_project_binding,
    get_project as workspace_get_project,
    load_workspace,
    project_binding_from_compat,
    project_binding_to_compat,
    register_project,
    save_workspace,
    set_active_project,
    workspace_summary,
)
from src.tandem_agents.runtime.runstate import load_status, load_blackboard, set_event_broadcast_callback, new_run_id
from src.tandem_agents.runtime.runstate import append_event, write_status
from src.tandem_agents.runtime.run_output import save_run_text
from src.tandem_agents.core.external_actions.github_pr import execute_approved_actions
from src.tandem_agents.core.integrations.linear_mcp import (
    linear_status_name_for_task_state,
    linear_update_issue,
    normalize_linear_key,
)
from src.tandem_agents.cli.monitor import latest_run_dir

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aca.api")

app = FastAPI(title="ACA Control Plane API")
app.include_router(aca_mcp_router)

# Enable CORS for the Control Panel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@dataclass
class RunState:
    run_id: str
    project_slug: str
    is_running: bool = True
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

class RunManager:
    def __init__(self):
        self.runs: Dict[str, RunState] = {}
        self.global_queue: asyncio.Queue = asyncio.Queue()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def create_run(self, run_id: str, project_slug: str) -> RunState:
        with self._lock:
            state = RunState(run_id=run_id, project_slug=project_slug)
            self.runs[run_id] = state
            return state

    def _dispatch(self, queue: asyncio.Queue, payload: Dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        if loop is None:
            logger.warning("Skipping ACA event broadcast because no asyncio loop is attached yet.")
            return
        if loop.is_running():
            loop.call_soon_threadsafe(queue.put_nowait, payload)
            return
        queue.put_nowait(payload)

    def broadcast_global(self, event_type: str, data: Any):
        self._dispatch(self.global_queue, {"event": event_type, "data": data})

    def broadcast_run(self, run_id: str, event_type: str, data: Any):
        # Always broadcast to global queue for system-wide monitoring
        self.broadcast_global(f"run_event:{run_id}", {"type": event_type, "event": data})
        
        # Also broadcast to run-specific queue if it's active
        if run_id in self.runs:
            self._dispatch(self.runs[run_id].event_queue, {"event": event_type, "data": data})

run_manager = RunManager()
set_event_broadcast_callback(run_manager.broadcast_run)
_coordination_reaper_task: Optional[asyncio.Task[None]] = None
_coordination_reaper_stop = threading.Event()
_coder_supervisor_task: Optional[asyncio.Task[None]] = None
_coder_supervisor_stop = threading.Event()
_start_time = time.monotonic()


@app.on_event("startup")
async def _attach_run_manager_loop():
    # Fail fast before serving any requests if the API token is not configured
    # in strict mode. See src/tandem_agents/api/auth.py:assert_api_token_configured.
    assert_api_token_configured()
    run_manager.attach_loop(asyncio.get_running_loop())
    await _start_coder_supervisor()
    await _start_coordination_reaper()


@app.on_event("shutdown")
async def _shutdown_background_tasks():
    _coordination_reaper_stop.set()
    _coder_supervisor_stop.set()
    global _coordination_reaper_task
    if _coordination_reaper_task and not _coordination_reaper_task.done():
        _coordination_reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _coordination_reaper_task
    global _coder_supervisor_task
    if _coder_supervisor_task and not _coder_supervisor_task.done():
        _coder_supervisor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _coder_supervisor_task


async def _coder_supervisor_loop(cfg) -> None:
    interval = max(1, int(getattr(cfg.execution, "coder_supervisor_interval_seconds", 30) or 30))
    logger.info("Starting coder supervisor loop with %ss interval.", interval)
    while not _coder_supervisor_stop.is_set():
        try:
            summary = await asyncio.to_thread(reconcile_active_coder_runs, cfg)
            if summary.get("count"):
                run_manager.broadcast_global("coder_supervisor.reconciled", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Coder supervisor reconciliation failed.", exc_info=True)
        try:
            stopped = await asyncio.to_thread(_coder_supervisor_stop.wait, interval)
            if stopped:
                break
        except asyncio.CancelledError:
            raise


async def _start_coder_supervisor() -> None:
    global _coder_supervisor_task
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    if not bool(getattr(cfg.execution, "coder_supervisor_enabled", True)):
        return
    if _coder_supervisor_task and not _coder_supervisor_task.done():
        return
    _coder_supervisor_stop.clear()
    try:
        await asyncio.to_thread(reconcile_active_coder_runs, cfg)
    except Exception:
        logger.debug("Initial coder supervisor reconciliation failed.", exc_info=True)
    _coder_supervisor_task = asyncio.create_task(_coder_supervisor_loop(cfg))

async def _coordination_reaper_loop(cfg) -> None:
    interval = coordination_reaper_interval(cfg)
    logger.info("Starting coordination lease reaper loop with %ss interval.", interval)
    while not _coordination_reaper_stop.is_set():
        try:
            expired = await asyncio.to_thread(coordination_reaper_tick, cfg)
            if expired:
                logger.info("Reaped %s expired coordination lease(s).", len(expired))
                run_manager.broadcast_global(
                    "coordination_leases_reaped",
                    {
                        "count": len(expired),
                        "leases": expired,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Coordination lease reaper tick failed")
        try:
            stopped = await asyncio.to_thread(_coordination_reaper_stop.wait, interval)
            if stopped:
                break
        except asyncio.CancelledError:
            raise


async def _start_coordination_reaper() -> None:
    global _coordination_reaper_task
    if _coordination_reaper_task and not _coordination_reaper_task.done():
        return
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    _coordination_reaper_stop.clear()
    _coordination_reaper_task = asyncio.create_task(_coordination_reaper_loop(cfg))

def load_projects(root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or Path(os.environ.get("ACA_ROOT", "."))
    workspace = load_workspace(root)
    cfg = resolve_config(root)
    configured = project_binding_to_compat(configured_project_binding(cfg))
    projects: Dict[str, Any] = {
        str(project.get("id")): project_binding_to_compat(project)
        for project in workspace.get("workspace", {}).get("projects", [])
        if str(project.get("id") or "").strip()
    }
    if configured.get("id") and configured["id"] not in projects:
        projects = {configured["id"]: configured, **projects}
    return projects


def save_projects(projects: Dict[str, Any], root: Optional[Path] = None):
    root = root or Path(os.environ.get("ACA_ROOT", "."))
    workspace = load_workspace(root)
    if isinstance(projects, dict) and "workspace" in projects and isinstance(projects["workspace"], dict):
        workspace = projects
    else:
        workspace["workspace"]["projects"] = [
            project_binding_from_compat(str(project_id), record)
            for project_id, record in projects.items()
            if isinstance(record, dict)
        ]
        if workspace["workspace"]["projects"] and not workspace["workspace"].get("active_project_id"):
            workspace["workspace"]["active_project_id"] = workspace["workspace"]["projects"][0]["id"]
    save_workspace(root, workspace)


def _workspace_view(root: Path, cfg=None) -> Dict[str, Any]:
    cfg = cfg or resolve_config(root)
    workspace = load_workspace(root)
    configured = configured_project_binding(cfg)
    projects = list(workspace.get("workspace", {}).get("projects", []))
    if not any(str(project.get("id")) == str(configured["id"]) for project in projects):
        projects = [configured] + projects
    active_project_id = workspace.get("workspace", {}).get("active_project_id") or (projects[0]["id"] if projects else None)
    workspace["workspace"]["projects"] = projects
    workspace["workspace"]["active_project_id"] = active_project_id
    return workspace_summary(workspace)


def _all_projects(root: Path) -> Dict[str, Any]:
    workspace = _workspace_view(root)
    return {str(project.get("id")): project for project in workspace.get("projects", []) if str(project.get("id") or "").strip()}


def _project_runtime_env(root: Path, project: Dict[str, Any], *, fallback_slug: str = "") -> Dict[str, str]:
    env: Dict[str, str] = {}
    repo = project.get("repo") if isinstance(project.get("repo"), dict) else {}
    project_id = str(project.get("id") or project.get("slug") or fallback_slug or "").strip()
    repo_slug = str(repo.get("slug") or project.get("repo_slug") or project.get("slug") or project_id).strip()
    repo_url = str(project.get("repo_url") or repo.get("clone_url") or repo.get("repo_url") or "").strip()
    repo_path = str(repo.get("path") or project.get("repo_path") or "").strip()
    worktree_root = str(repo.get("worktree_root") or project.get("worktree_root") or "").strip()
    default_branch = str(repo.get("default_branch") or project.get("default_branch") or "").strip()
    remote_name = str(repo.get("remote_name") or project.get("remote_name") or "").strip()
    credential_file = str(
        repo.get("credential_file")
        or repo.get("token_file")
        or project.get("credential_file")
        or project.get("token_file")
        or ""
    ).strip()
    if repo_slug:
        env["ACA_REPO_SLUG"] = repo_slug
    elif fallback_slug:
        env["ACA_REPO_SLUG"] = fallback_slug
    if repo_url:
        env["ACA_REPO_URL"] = repo_url
    if not repo_path and (repo_slug or repo_url):
        repo_name = (repo_slug.rstrip("/").split("/")[-1] if repo_slug else "").strip()
        if not repo_name and repo_url:
            repo_name = repo_url.rstrip("/").removesuffix(".git").split("/")[-1].strip()
        if repo_name:
            repo_path = f"workspace/repos/{repo_name}"
    if not worktree_root and repo_path.startswith("workspace/repos/"):
        worktree_root = "workspace/repos"
    if repo_path:
        env["ACA_REPO_PATH"] = repo_path
    if worktree_root:
        env["ACA_WORKTREE_ROOT"] = worktree_root
    if default_branch:
        env["ACA_DEFAULT_BRANCH"] = default_branch
    if remote_name:
        env["ACA_REMOTE_NAME"] = remote_name
    if credential_file:
        env["ACA_REPO_TOKEN_FILE"] = credential_file
        env["GITHUB_PERSONAL_ACCESS_TOKEN_FILE"] = credential_file
    _apply_task_source_env(env, project.get("task_source") or project.get("source"))
    return env


def _project_config(root: Path, slug: str):
    projects = _all_projects(root)
    if slug not in projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = projects[slug]
    env = _project_runtime_env(root, project, fallback_slug=slug)
    return project, resolve_config(root, env=env)


def _is_safe_managed_path(raw: str) -> bool:
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return True
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return False
    parts = [part for part in text.split("/") if part]
    return bool(parts) and not any(part in {".", ".."} for part in parts)


def _apply_task_source_env(target_env: Dict[str, str], task_source: Optional[Dict[str, Any]]) -> None:
    for key, value in (task_source or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        target_env[f"ACA_TASK_SOURCE_{str(key).upper()}"] = text


def _run_dir(cfg, run_id: str) -> Path:
    return cfg.output_root() / run_id


def _run_summary(run_dir: Path) -> Optional[str]:
    summary_path = run_dir / "summary.md"
    if not summary_path.exists():
        return None
    try:
        return summary_path.read_text(encoding="utf-8")
    except Exception:
        return None


def _run_events(run_dir: Path, tail: int = 80) -> list[dict[str, Any]]:
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-max(1, min(tail, 500)):]:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _run_diff_snapshot(run_dir: Path) -> dict[str, Any]:
    after_path = run_dir / "diffs" / "after.txt"
    before_path = run_dir / "diffs" / "before.txt"
    after = ""
    before = ""
    try:
        if after_path.exists():
            after = after_path.read_text(encoding="utf-8")
    except Exception:
        after = ""
    try:
        if before_path.exists():
            before = before_path.read_text(encoding="utf-8")
    except Exception:
        before = ""

    changed_files: list[str] = []
    for line in after.splitlines():
        text = line.strip()
        if not text or text.startswith("("):
            continue
        if " file changed" in text or " files changed" in text:
            continue
        if "|" not in text:
            continue
        path = text.split("|", 1)[0].strip()
        if path:
            changed_files.append(path)

    return {
        "before": before,
        "after": after,
        "changed_files": changed_files,
        "available": bool(after.strip() and after.strip() != "(clean)"),
    }


def _persisted_run_status(status_payload: dict[str, Any] | None) -> str:
    run_meta = status_payload.get("run") if isinstance(status_payload, dict) else None
    if not isinstance(run_meta, dict):
        return ""
    return str(run_meta.get("status") or "").strip().lower()


def _persisted_run_is_active(status_payload: dict[str, Any] | None) -> bool:
    return _persisted_run_status(status_payload) in {"created", "running"}


def _persisted_run_error(status_payload: dict[str, Any] | None) -> str | None:
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


def _build_run_snapshot(run_id: str, run_dir: Path, active_state: Optional["RunState"] = None) -> Dict[str, Any]:
    status_payload = load_status(run_dir / "status.json") if run_dir.exists() else {}
    run_meta = status_payload.get("run") if isinstance(status_payload, dict) else {}
    task_meta = status_payload.get("task") if isinstance(status_payload, dict) else {}
    repo_meta = status_payload.get("repo") if isinstance(status_payload, dict) else {}
    phase_meta = status_payload.get("phase") if isinstance(status_payload, dict) else {}
    blackboard = load_blackboard(run_dir / "blackboard.yaml") if run_dir.exists() else {}

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
    if active_state and active_state.project_slug:
        project_slug = active_state.project_slug
    is_running = _persisted_run_is_active(status_payload)
    error = _persisted_run_error(status_payload)
    if not run_dir.exists() and active_state:
        is_running = active_state.is_running
        error = active_state.error if active_state.error is not None else error
    elif active_state and not error and active_state.error is not None:
        error = active_state.error

    return {
        "run_id": run_id,
        "project_slug": project_slug,
        "title": task_meta.get("title") if isinstance(task_meta, dict) else None,
        "status": run_meta.get("status") if isinstance(run_meta, dict) else None,
        "phase": phase_meta if isinstance(phase_meta, dict) else {},
        "branch": repo_meta.get("branch") if isinstance(repo_meta, dict) else None,
        "updated_at_ms": run_meta.get("updated_at_ms") if isinstance(run_meta, dict) else None,
        "created_at_ms": run_meta.get("created_at_ms") if isinstance(run_meta, dict) else None,
        "is_running": is_running,
        "has_error": bool(error),
        "error": error,
        "summary_available": _run_summary(run_dir) is not None,
        "events": _run_events(run_dir),
        "diff": _run_diff_snapshot(run_dir),
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


def _list_run_snapshots(cfg) -> List[Dict[str, Any]]:
    output_root = cfg.output_root()
    snapshots: Dict[str, Dict[str, Any]] = {}
    if output_root.exists():
        for run_dir in output_root.iterdir():
            if not _is_run_directory(run_dir):
                continue
            snapshots[run_dir.name] = _build_run_snapshot(run_dir.name, run_dir)

    for run_id, state in run_manager.runs.items():
        run_dir = _run_dir(cfg, run_id)
        if run_dir.exists():
            snapshots[run_id] = _build_run_snapshot(run_id, run_dir, state)
            continue
        # Live event queues are intentionally ephemeral. If the process restarts
        # before the run directory appears, this fallback keeps the brand-new run
        # visible for the current process lifetime only.
        snapshots[run_id] = {
            "run_id": run_id,
            "project_slug": state.project_slug,
            "title": None,
            "status": "starting",
            "phase": {"name": "bootstrap"},
            "branch": None,
            "updated_at_ms": None,
            "created_at_ms": None,
            "is_running": state.is_running,
            "has_error": state.error is not None,
            "error": state.error,
            "summary_available": False,
            "artifacts": {},
            "blackboard": {},
        }

    return sorted(
        snapshots.values(),
        key=lambda item: item.get("updated_at_ms") or item.get("created_at_ms") or 0,
        reverse=True,
    )

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "0.1.0",
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    }


@app.get("/ready")
async def readiness(token: str = Depends(get_token)):
    """Readiness check: reports whether ACA can accept work.

    Returns 200 with ready=true only when the Tandem engine is reachable
    and the coordination store is connected.
    """
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    engine_info = engine_status_report(cfg)
    engine_healthy = bool(engine_info.get("healthy"))

    coord_ok = False
    try:
        from src.tandem_agents.core.coordination.coordination import CoordinationStore
        store = CoordinationStore.from_config(cfg)
        store.ensure_schema()
        coord_ok = True
    except Exception as exc:
        logger.warning("Readiness check: coordination store unavailable: %s", exc)

    return {
        "ready": engine_healthy and coord_ok,
        "engine": engine_info,
        "coordination": {"connected": coord_ok},
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    }

@app.get("/projects")
async def list_projects(token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    return _all_projects(root)


@app.get("/workspace")
async def get_workspace(token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    return _workspace_view(root)


@app.post("/workspace/projects")
async def upsert_workspace_project(
    slug: str,
    repo_url: Optional[str] = None,
    repo_path: Optional[str] = None,
    worktree_root: Optional[str] = None,
    default_branch: Optional[str] = None,
    remote_name: Optional[str] = None,
    credential_file: Optional[str] = None,
    name: Optional[str] = None,
    task_source: Optional[Dict[str, Any]] = Body(default=None),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    if (repo_path and not _is_safe_managed_path(repo_path)) or (
        worktree_root and not _is_safe_managed_path(worktree_root)
    ):
        raise HTTPException(status_code=400, detail="Managed repo paths must stay within the workspace root")
    workspace = load_workspace(root)
    record = {
        "id": slug,
        "name": name or slug,
        "repo": {
            "slug": "",
            "default_branch": default_branch or "main",
            "path": repo_path or "",
            "remote_name": remote_name or "origin",
            "worktree_root": worktree_root or "",
            "credential_file": credential_file or "",
            "clone_url": repo_url or "",
        },
        "source": task_source or {},
        "repo_url": repo_url or "",
    }
    workspace = register_project(workspace, record, project_id=slug)
    save_workspace(root, workspace)
    project = workspace_get_project(workspace, slug)
    return project or project_binding_to_compat(record)


@app.post("/workspace/active/{project_id:path}")
async def set_workspace_active_project(project_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    workspace = load_workspace(root)
    try:
        workspace = set_active_project(workspace, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    save_workspace(root, workspace)
    return workspace_summary(workspace)


@app.get("/workspace/guide")
async def get_workspace_guide(token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    workspace = _workspace_view(root, cfg)
    projects = workspace.get("projects", [])
    active_project_id = workspace.get("workspace", {}).get("active_project_id")
    active_project = next((project for project in projects if str(project.get("id")) == str(active_project_id)), None)
    return {
        "ok": True,
        "workspace": workspace.get("workspace", {}),
        "active_project_id": active_project_id,
        "active_project": active_project,
        "projects": projects,
        "instructions": [
            "Call this guide first so you know which named repo bindings and workspace paths are available.",
            "Use the project id as the stable alias for each repo binding.",
            "Use the repo.path field as the managed checkout location and keep edits inside that checkout.",
            "If repo.credential_file is set, ACA will use that secret file for clone and push operations.",
            "When a repo path is not explicitly configured, ACA falls back to the managed worktree root for that project.",
        ],
        "layout": {
            "workspace_root": str(cfg.repository_worktree_root()),
            "repo_path": str(cfg.repository_path() or cfg.repository.path or ""),
            "worktree_root": str(cfg.repository_worktree_root()),
        },
        "fields": {
            "project_id": "stable alias used by ACA and the control panel",
            "repo.slug": "provider slug, typically owner/repo",
            "repo.path": "explicit checkout path managed by ACA",
            "repo.worktree_root": "base directory for managed clones or worktrees",
            "repo.credential_file": "server-side token file used for private clone/push access",
        },
    }


def _linear_catalog_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("key", "name", "displayName", "title", "id", "slug"):
            text = _linear_catalog_text(value.get(key))
            if text:
                return text
        return ""
    return str(value).strip()


def _normalize_linear_team(team: Dict[str, Any]) -> Dict[str, Any]:
    key = _linear_catalog_text(team.get("key") or team.get("teamKey"))
    name = _linear_catalog_text(team.get("name") or team.get("displayName") or key)
    team_id = _linear_catalog_text(team.get("id"))
    return {
        "id": team_id or key or name,
        "key": key,
        "name": name or key or team_id,
        "display": f"{name} ({key})" if name and key and name != key else name or key or team_id,
        "raw": team,
    }


def _normalize_linear_project(project: Dict[str, Any]) -> Dict[str, Any]:
    project_id = _linear_catalog_text(project.get("id"))
    name = _linear_catalog_text(project.get("name") or project.get("title"))
    slug = _linear_catalog_text(project.get("slug") or project.get("urlKey"))
    team = project.get("team") if isinstance(project.get("team"), dict) else {}
    teams = project.get("teams") if isinstance(project.get("teams"), list) else []
    first_team = next((entry for entry in teams if isinstance(entry, dict)), {})
    team_id = _linear_catalog_text(project.get("teamId") or project.get("team_id") or team.get("id") or first_team.get("id"))
    team_key = _linear_catalog_text(project.get("teamKey") or project.get("team_key") or team.get("key") or first_team.get("key"))
    team_name = _linear_catalog_text(
        project.get("teamName") or project.get("team_name") or team.get("name") or first_team.get("name")
    )
    return {
        "id": project_id or slug or name,
        "name": name or slug or project_id,
        "slug": slug,
        "team_id": team_id,
        "team_key": team_key,
        "team_name": team_name,
        "issue_count": project.get("issueCount") or project.get("issue_count") or project.get("issuesCount"),
        "raw": project,
    }


def _dedupe_linear_catalog_entries(entries: list[Dict[str, Any]], keys: tuple[str, ...]) -> list[Dict[str, Any]]:
    deduped: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        parts = [str(entry.get(key) or "").strip().lower() for key in keys]
        identity = next((part for part in parts if part), json.dumps(entry, sort_keys=True, default=str))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(entry)
    return deduped


def _linear_auth_challenge(server: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(server, dict):
        return {}
    challenge = server.get("last_auth_challenge") or server.get("lastAuthChallenge")
    if not isinstance(challenge, dict):
        pending = server.get("pending_auth_by_tool") or server.get("pendingAuthByTool")
        if isinstance(pending, dict):
            for value in pending.values():
                if isinstance(value, dict):
                    challenge = value
                    break
    return challenge if isinstance(challenge, dict) else {}


@app.get("/linear/catalog")
async def linear_catalog(
    team: Optional[str] = None,
    query: Optional[str] = None,
    include_archived: bool = False,
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        from src.tandem_agents.core.integrations.linear_mcp import (
            get_mcp_server,
            linear_count_issues,
            linear_list_issues,
            linear_list_projects,
            linear_list_teams,
            linear_mcp_server_name,
        )

        server_name = linear_mcp_server_name(cfg)
        server = await asyncio.to_thread(get_mcp_server, cfg, server_name)
        if server is None:
            return {
                "ok": False,
                "server": server_name,
                "connected": False,
                "auth_required": False,
                "teams": [],
                "projects": [],
                "message": (
                    f"Linear MCP server '{server_name}' is not configured in the connected "
                    "Tandem engine. Add it in the MCP settings first."
                ),
            }
        if not bool(server.get("connected")):
            challenge = _linear_auth_challenge(server)
            authorization_url = _linear_catalog_text(
                challenge.get("authorization_url")
                or challenge.get("authorizationUrl")
                or server.get("authorizationUrl")
            )
            return {
                "ok": True,
                "server": server_name,
                "connected": False,
                "auth_required": str(server.get("auth_kind") or "").strip().lower() == "oauth",
                "auth_status": _linear_catalog_text(challenge.get("status") or "pending"),
                "authorization_url": authorization_url,
                "last_auth_challenge": challenge,
                "teams": [],
                "projects": [],
                "message": (
                    "Linear MCP is configured but not connected. Open MCP settings, connect "
                    "the Linear server, finish OAuth, then refresh the Linear catalog."
                ),
            }
        team_filter = str(team or "").strip()
        teams_raw = await asyncio.to_thread(linear_list_teams, cfg, query=str(query or "").strip(), limit=100)
        projects_raw = await asyncio.to_thread(
            linear_list_projects,
            cfg,
            team=team_filter,
            query=str(query or "").strip(),
            include_archived=include_archived,
            limit=100,
        )
        teams = _dedupe_linear_catalog_entries(
            [_normalize_linear_team(entry) for entry in teams_raw],
            ("id", "key", "name"),
        )
        projects = _dedupe_linear_catalog_entries(
            [_normalize_linear_project(entry) for entry in projects_raw],
            ("id", "slug", "name"),
        )
        for project in projects:
            if project.get("issue_count") not in (None, ""):
                continue
            selector = str(project.get("id") or project.get("name") or "").strip()
            if not selector:
                continue
            try:
                project["issue_count"] = await asyncio.to_thread(
                    linear_count_issues,
                    cfg,
                    team=team_filter or str(project.get("team_key") or project.get("team_name") or ""),
                    project=selector,
                )
            except Exception:
                project["issue_count"] = None
        return {
            "ok": True,
            "server": server_name,
            "connected": bool(server.get("connected")),
            "auth_required": False,
            "teams": teams,
            "projects": projects,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not read Linear teams/projects through Tandem's connected Linear MCP server: "
                f"{exc}"
            ),
        )


@app.post("/projects")
async def add_project(
    slug: str,
    repo_url: Optional[str] = None,
    repo_path: Optional[str] = None,
    worktree_root: Optional[str] = None,
    default_branch: Optional[str] = None,
    remote_name: Optional[str] = None,
    credential_file: Optional[str] = None,
    name: Optional[str] = None,
    task_source: Optional[Dict[str, Any]] = Body(default=None),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    if (repo_path and not _is_safe_managed_path(repo_path)) or (
        worktree_root and not _is_safe_managed_path(worktree_root)
    ):
        raise HTTPException(status_code=400, detail="Managed repo paths must stay within the workspace root")
    workspace = load_workspace(root)
    record = {
        "id": slug,
        "name": name or slug,
        "repo": {
            "slug": "",
            "default_branch": default_branch or "main",
            "path": repo_path or "",
            "remote_name": remote_name or "origin",
            "worktree_root": worktree_root or "",
            "credential_file": credential_file or "",
            "clone_url": repo_url or "",
        },
        "source": task_source or {},
        "repo_url": repo_url or "",
    }
    workspace = register_project(workspace, record, project_id=slug)
    save_workspace(root, workspace)
    project = workspace_get_project(workspace, slug)
    return project or project_binding_to_compat(record)

@app.get("/projects/{slug:path}/tasks")
async def get_project_tasks(slug: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    _, cfg = _project_config(root, slug)
    from src.tandem_agents.runtime.task_sources import preview_task
    try:
        return await asyncio.to_thread(preview_task, cfg)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _operator_linear_status_name(cfg, target_state: str) -> str:
    key = normalize_linear_key(target_state)
    explicit = {
        "backlog": "Backlog",
        "todo": "Todo",
        "to_do": "Todo",
        "ready": "Ready",
        "triage": "Triage",
    }
    if key in explicit:
        return explicit[key]
    return linear_status_name_for_task_state(cfg, key)


@app.post("/projects/{slug:path}/tasks/{item}/state")
async def update_project_task_state(
    slug: str,
    item: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    _, cfg = _project_config(root, slug)
    if cfg.task_source.type != "linear":
        raise HTTPException(status_code=400, detail="Task state updates are currently supported for Linear task sources.")
    target_state = str(payload.get("state") or payload.get("status") or "").strip()
    if not target_state:
        raise HTTPException(status_code=400, detail="state is required.")
    target_status = _operator_linear_status_name(cfg, target_state)
    task = {
        "task_id": item,
        "source": {
            "type": "linear",
            "item": item,
            "identifier": item,
            "issue_id": item,
        },
    }
    try:
        warning = await asyncio.to_thread(
            linear_update_issue,
            cfg,
            task,
            {
                "status": target_status,
                "state": target_status,
                "state_name": target_status,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not update Linear issue state: {exc}") from exc
    if warning:
        raise HTTPException(status_code=400, detail=warning)
    return {"ok": True, "item": item, "state": target_state, "status": target_status}


@app.post("/projects/{slug:path}/repo/sync")
async def sync_project_repo(slug: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    project, cfg = _project_config(root, slug)
    try:
        repo = await asyncio.to_thread(resolve_repository, cfg)
        return {
            "ok": True,
            "project_slug": slug,
            "project": project,
            "repo": repo,
            "message": "Repository is ready.",
        }
    except RuntimeError as exc:
        message = str(exc)
        status_code = 409 if "uncommitted changes" in message.lower() else 400
        raise HTTPException(status_code=status_code, detail=message)


@app.get("/projects/{slug:path}/board")
async def get_project_board(slug: str, refresh: bool = False, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    _, cfg = _project_config(root, slug)
    from src.tandem_agents.runtime.task_sources import task_source_board_snapshot

    try:
        snapshot = await asyncio.to_thread(task_source_board_snapshot, cfg, force_refresh=refresh)
        snapshot["project_slug"] = slug
        snapshot["task_source_type"] = cfg.task_source.type
        return snapshot
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

from fastapi.responses import FileResponse

# ... (inside app definition)

@app.get("/runs/{run_id}/artifacts/{file_path:path}")
async def get_run_artifact(run_id: str, file_path: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    artifact_path = cfg.output_root() / run_id / "artifacts" / file_path
    
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    
    return FileResponse(artifact_path)

@app.get("/runs")
async def list_runs(token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    return {"runs": _list_run_snapshots(cfg)}


def _scheduler_filtered_source_items(root: Path, project_slug: Optional[str], items: list[str]) -> list[str]:
    _, cfg = _project_config(root, project_slug) if project_slug else (None, resolve_config(root))
    source_type = str(cfg.task_source.type or "").strip()
    if source_type not in {"github_project", "linear"}:
        return items

    from src.tandem_agents.runtime.task_sources import task_source_board_snapshot

    snapshot = task_source_board_snapshot(cfg, force_refresh=True)
    requested = set(items)

    def matches_requested(row: dict[str, Any]) -> bool:
        haystacks = {
            str(row.get("id") or ""),
            str(row.get("project_item_id") or ""),
            str(row.get("issue_id") or ""),
            str(row.get("identifier") or ""),
            str(row.get("issue_number") or ""),
            str(row.get("issue_url") or ""),
            str(row.get("title") or ""),
        }
        return any(item == hay or (item and item in hay) for item in requested for hay in haystacks if hay)

    scheduled_items = [
        str(row.get("identifier") or row.get("project_item_id") or row.get("issue_id") or row.get("id") or "").strip()
        for row in snapshot.get("items", [])
        if matches_requested(row) and row.get("actionable") is True
    ]
    scheduled_items = [item for item in dict.fromkeys(scheduled_items) if item]
    if not scheduled_items:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "No selected task-source items are currently scheduler-actionable.",
                "scheduler": snapshot.get("scheduler") or {},
            },
        )
    return scheduled_items


@app.post("/runs/trigger")
async def trigger_run(project_slug: Optional[str] = None, task_source_type: Optional[str] = None, item: Optional[str] = None, overrides: Dict[str, str] = {}, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    if item:
        try:
            item = (
                await asyncio.to_thread(
                    _scheduler_filtered_source_items,
                    root,
                    project_slug,
                    [str(item).strip()],
                )
            )[0]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not apply ACA scheduler policy: {exc}") from exc
    return _start_run(
        root,
        project_slug=project_slug,
        task_source_type=task_source_type,
        item=item,
        overrides=overrides,
    )


@app.post("/runs/trigger-batch")
async def trigger_runs_batch(
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    project_slug = str(payload.get("project_slug") or "").strip() or None
    task_source_type = str(payload.get("task_source_type") or "").strip() or None
    overrides = payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {}
    respect_scheduler = payload.get("respect_scheduler") is not False
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail="items must be a list")

    items = [str(item or "").strip() for item in raw_items if str(item or "").strip()]
    if not items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    if respect_scheduler:
        try:
            items = await asyncio.to_thread(_scheduler_filtered_source_items, root, project_slug, items)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not apply ACA scheduler policy: {exc}") from exc

    started = [
        _start_run(
            root,
            project_slug=project_slug,
            task_source_type=task_source_type,
            item=item,
            overrides=overrides,
        )
        for item in items
    ]
    return {"status": "started", "count": len(started), "runs": started}

@app.get("/runs/{run_id}")
async def get_run(run_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    run_dir = _run_dir(cfg, run_id)
    active_state = run_manager.runs.get(run_id)
    if not run_dir.exists():
        if active_state:
            return {
                "run_id": run_id,
                "project_slug": active_state.project_slug,
                "is_running": active_state.is_running,
                "status": {"run": {"status": "starting"}},
                "blackboard": {},
                "error": active_state.error,
            }
        raise HTTPException(status_code=404, detail="Run not found")
    status_payload = load_status(run_dir / "status.json")
    snapshot = _build_run_snapshot(run_id, run_dir, active_state)
    return {
        "run_id": run_id,
        "project_slug": snapshot.get("project_slug", "unknown"),
        "is_running": snapshot.get("is_running", False),
        "status": status_payload,
        "blackboard": load_blackboard(run_dir / "blackboard.yaml"),
        "events": _run_events(run_dir),
        "diff": _run_diff_snapshot(run_dir),
        "error": snapshot.get("error"),
        "summary": _run_summary(run_dir),
        "snapshot": snapshot,
    }


def _external_action_linear_fields(target_status: str, labels: list[str] | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "status": target_status,
        "state": target_status,
        "state_name": target_status,
    }
    clean_labels = [str(label).strip() for label in (labels or []) if str(label).strip()]
    if clean_labels:
        fields["labels"] = clean_labels
        fields["label_names"] = clean_labels
    return fields


def _finalize_external_action_linear_status(cfg, *, run_id: str, run_dir: Path) -> dict[str, Any]:
    blackboard = load_blackboard(run_dir / "blackboard.yaml")
    task = blackboard.get("task") if isinstance(blackboard, dict) else None
    if not isinstance(task, dict):
        return {"skipped": True, "reason": "missing task"}
    source_type = str((task.get("source") or {}).get("type") or "").strip()
    if source_type != "linear":
        return {"skipped": True, "reason": "source is not Linear"}
    target_status = str(cfg.linear_mcp.done_status or "").strip() or linear_status_name_for_task_state(cfg, "done")
    labels = [cfg.linear_mcp.done_label] if str(cfg.linear_mcp.done_label or "").strip() else []
    try:
        warning = linear_update_issue(cfg, task, _external_action_linear_fields(target_status, labels))
    except Exception as exc:
        warning = str(exc)
    if warning:
        append_event(
            run_dir / "events.jsonl",
            "linear_issue.status_update_failed",
            run_id,
            {"status": target_status, "warning": warning},
        )
        return {"updated": False, "status": target_status, "warning": warning}
    append_event(run_dir / "events.jsonl", "linear_issue.status_updated", run_id, {"status": target_status})
    return {"updated": True, "status": target_status}


@app.get("/approvals")
async def list_external_action_approvals(
    run_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 100,
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    approvals = store.list_external_action_approvals(
        run_id=run_id,
        status=status_filter,
        limit=max(1, min(limit, 500)),
    )
    return {"approvals": approvals, "count": len(approvals)}


@app.get("/approvals/pending")
async def list_pending_external_action_approvals(limit: int = 100, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    approvals = store.list_external_action_approvals(status="pending", limit=max(1, min(limit, 500)))
    return {"approvals": approvals, "count": len(approvals)}


@app.get("/runs/{run_id}/approvals")
async def list_run_external_action_approvals(run_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    approvals = store.list_external_action_approvals(run_id=run_id, limit=500)
    return {"run_id": run_id, "approvals": approvals, "count": len(approvals)}


@app.post("/approvals/{approval_id}/approve")
async def approve_external_action(
    approval_id: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    actor = str(payload.get("actor") or "operator").strip()
    reason = str(payload.get("reason") or "").strip()
    approval = store.decide_external_action_approval(approval_id, decision="approve", actor=actor, reason=reason)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"approval": approval}


@app.post("/approvals/{approval_id}/reject")
async def reject_external_action(
    approval_id: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    actor = str(payload.get("actor") or "operator").strip()
    reason = str(payload.get("reason") or "").strip()
    approval = store.decide_external_action_approval(approval_id, decision="reject", actor=actor, reason=reason)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"approval": approval}


@app.post("/approvals/{approval_id}/retry")
async def retry_external_action(
    approval_id: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    actor = str(payload.get("actor") or "operator").strip()
    reason = str(payload.get("reason") or "retry failed external action").strip()
    approval = store.retry_external_action_approval(approval_id, actor=actor, reason=reason)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"approval": approval}


@app.post("/runs/{run_id}/approvals/approve-pending")
async def approve_pending_external_actions(
    run_id: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    actor = str(payload.get("actor") or "operator").strip()
    reason = str(payload.get("reason") or "batch approve pending external actions").strip()
    approvals = store.approve_pending_external_action_approvals(run_id=run_id, actor=actor, reason=reason)
    return {"run_id": run_id, "approvals": approvals, "count": len(approvals)}


@app.post("/runs/{run_id}/approvals/retry-failed")
async def retry_failed_external_actions(
    run_id: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    actor = str(payload.get("actor") or "operator").strip()
    reason = str(payload.get("reason") or "retry failed external actions").strip()
    approvals = store.retry_failed_external_action_approvals(run_id=run_id, actor=actor, reason=reason)
    return {"run_id": run_id, "approvals": approvals, "count": len(approvals)}


@app.post("/runs/{run_id}/resume-approved-actions")
async def resume_approved_external_actions(run_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    result = await asyncio.to_thread(execute_approved_actions, cfg, store, run_id=run_id)
    run_dir = _run_dir(cfg, run_id)
    if run_dir.exists():
        append_event(run_dir / "events.jsonl", "external_actions.resumed", run_id, result)
        status_path = run_dir / "status.json"
        status_payload = load_status(status_path)
        if result.get("complete"):
            status_payload.setdefault("run", {})["status"] = "completed"
            status_payload.setdefault("phase", {})["name"] = "handoff"
            status_payload.setdefault("phase", {})["detail"] = "external actions executed and verified"
            blocker = status_payload.setdefault("blocker", {})
            blocker["active"] = False
            blocker["kind"] = None
            blocker["message"] = None
            status_payload.setdefault("metrics", {})["tests_passed"] = True
            write_status(status_path, status_payload)
            save_run_text(
                run_dir / "summary.md",
                "# Run completed\n\nExternal GitHub PR actions were approved, executed, and verified.\n",
            )
            result["linear_finalize"] = await asyncio.to_thread(
                _finalize_external_action_linear_status,
                cfg,
                run_id=run_id,
                run_dir=run_dir,
            )
            append_event(run_dir / "events.jsonl", "run.completed", run_id, {"kind": "external_actions"})
        elif result.get("failed_count"):
            status_payload.setdefault("run", {})["status"] = "blocked"
            status_payload.setdefault("phase", {})["name"] = "external_actions"
            status_payload.setdefault("phase", {})["detail"] = "external action execution failed"
            blocker = status_payload.setdefault("blocker", {})
            blocker["active"] = True
            blocker["kind"] = "external_action_failed"
            blocker["message"] = "One or more approved external actions failed verification."
            write_status(status_path, status_payload)
            append_event(run_dir / "events.jsonl", "run.blocked", run_id, {"kind": "external_action_failed"})
    return result


@app.get("/runs/{run_id}/events/history")
async def get_run_events_history(run_id: str, tail: int = 80, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    run_dir = _run_dir(cfg, run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return {"events": _run_events(run_dir, tail=tail)}


@app.get("/runs/{run_id}/summary")
async def get_run_summary(run_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    run_dir = _run_dir(cfg, run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    summary = _run_summary(run_dir)
    if summary is None:
        raise HTTPException(status_code=404, detail="Summary not found")
    return {"content": summary}

@app.get("/runs/{run_id}/logs")
async def list_run_logs(run_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    logs_dir = cfg.output_root() / run_id / "logs"
    if not logs_dir.exists(): return {"logs": []}
    return {"logs": [{"name": f.name, "size": f.stat().st_size, "last_modified": f.stat().st_mtime} for f in logs_dir.glob("*.log")]}

@app.get("/runs/{run_id}/logs/{log_name}")
async def get_run_log(run_id: str, log_name: str, tail: int = 100, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    log_path = cfg.output_root() / run_id / "logs" / log_name
    if not log_path.exists(): raise HTTPException(status_code=404, detail="Log not found")
    try:
        return {"lines": log_path.read_text(encoding="utf-8").splitlines()[-tail:]}
    except Exception as e: return {"lines": [f"Error reading log: {e}"]}

def _run_worker(cfg, run_state: RunState):
    try:
        run_manager.broadcast_global("run_started", {"run_id": run_state.run_id, "project": run_state.project_slug})
        result = run_coordinator(cfg)
        run_state.result = result
        run_manager.broadcast_run(run_state.run_id, "run_completed", result)
        run_manager.broadcast_global("run_completed", {"run_id": run_state.run_id})
    except Exception as e:
        logger.exception(f"Run {run_state.run_id} failed")
        run_state.error = str(e)
        run_manager.broadcast_run(run_state.run_id, "run_failed", {"error": str(e)})
        run_manager.broadcast_global("run_failed", {"run_id": run_state.run_id, "error": str(e)})
    finally: run_state.is_running = False


def _start_run(
    root: Path,
    project_slug: Optional[str] = None,
    task_source_type: Optional[str] = None,
    item: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    run_env: Dict[str, str] = {}

    if project_slug:
        projects = _all_projects(root)
        if project_slug in projects:
            run_env.update(_project_runtime_env(root, projects[project_slug], fallback_slug=project_slug))
        else:
            run_env["ACA_REPO_SLUG"] = project_slug

    if task_source_type:
        run_env["ACA_TASK_SOURCE_TYPE"] = task_source_type
    if item:
        run_env["ACA_TASK_SOURCE_ITEM"] = item
        run_env["ACA_TASK_SOURCE_CARD_ID"] = item

    run_env.update(overrides or {})
    run_id = new_run_id()
    run_env["ACA_RUN_ID"] = run_id

    cfg = resolve_config(root, env=run_env)
    errors = list(validate_config(cfg))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    state = run_manager.create_run(run_id, project_slug or "default")
    threading.Thread(target=_run_worker, args=(cfg, state), daemon=True).start()
    return {"run_id": run_id, "status": "started"}

@app.post("/runs/qa")
async def trigger_qa_run(
    project_slug: str,
    pr_number: int,
    overrides: Dict[str, str] = {},
    token: str = Depends(get_token)
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    projects = _all_projects(root)
    if project_slug not in projects:
        raise HTTPException(status_code=404, detail="Project not found")
    
    p = projects[project_slug]
    run_env = _project_runtime_env(root, p, fallback_slug=project_slug)
    
    run_env.update(overrides)
    run_id = new_run_id(prefix="qa")
    run_env["ACA_RUN_ID"] = run_id
    
    cfg = resolve_config(root, env=run_env)
    errors = list(validate_config(cfg))
    if errors: raise HTTPException(status_code=400, detail={"errors": errors})

    state = run_manager.create_run(run_id, project_slug)
    
    def _qa_worker():
        try:
            run_manager.broadcast_global("run_started", {"run_id": run_id, "project": project_slug, "type": "qa"})
            result = run_qa(cfg, pr_number)
            state.result = result
            run_manager.broadcast_run(run_id, "run_completed", result)
            run_manager.broadcast_global("run_completed", {"run_id": run_id})
        except Exception as e:
            logger.exception(f"QA Run {run_id} failed")
            state.error = str(e)
            run_manager.broadcast_run(run_id, "run_failed", {"error": str(e)})
            run_manager.broadcast_global("run_failed", {"run_id": run_id, "error": str(e)})
        finally:
            state.is_running = False

    threading.Thread(target=_qa_worker, daemon=True).start()
    return {"run_id": run_id, "status": "started"}

@app.get("/events")
async def global_events(request: Request):
    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(run_manager.global_queue.get(), timeout=1.0)
                yield {
                    "data": json.dumps(
                        {
                            "event_type": item.get("event"),
                            "payload": item.get("data"),
                        }
                    )
                }
            except asyncio.TimeoutError:
                yield {"data": json.dumps({"event_type": "ping", "payload": "keep-alive"})}
    return EventSourceResponse(event_generator())

@app.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, token: str = Depends(get_token)):
    if run_id not in run_manager.runs:
        raise HTTPException(status_code=404, detail="Run not active")
    state = run_manager.runs[run_id]
    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(state.event_queue.get(), timeout=1.0)
                yield {
                    "data": json.dumps(
                        {
                            "run_id": run_id,
                            "event_type": item.get("event"),
                            "payload": item.get("data"),
                        }
                    )
                }
            except asyncio.TimeoutError:
                yield {
                    "data": json.dumps(
                        {
                            "run_id": run_id,
                            "event_type": "ping",
                            "payload": "keep-alive",
                        }
                    )
                }
    return EventSourceResponse(event_generator())

@app.get("/engine")
async def get_engine(token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    return engine_status_report(cfg)

@app.get("/config")
async def get_config(token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    return cfg.config_summary()


@app.get("/coordination")
async def get_coordination(limit: int = 25, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    return store.snapshot(limit=max(1, min(limit, 100)))


@app.get("/coordination/leases")
async def get_coordination_leases(limit: int = 25, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    snapshot = store.snapshot(limit=max(1, min(limit, 100)))
    return {"db_path": snapshot["db_path"], "leases": snapshot["leases"], "summary": snapshot["summary"]}


@app.get("/coordination/workers")
async def get_coordination_workers(limit: int = 25, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    snapshot = store.snapshot(limit=max(1, min(limit, 100)))
    return {
        "db_path": snapshot["db_path"],
        "workers": store.list_workers(limit=max(1, min(limit, 100))),
        "summary": snapshot["summary"],
    }


@app.get("/operator/summary")
async def get_operator_summary(limit: int = 25, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    return build_operator_summary(cfg, coordination=store, limit=max(1, min(limit, 100)))


@app.get("/operator/coder-runs")
async def get_operator_coder_runs(limit: int = 100, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    return {"coder_runs": list_active_coder_runs(cfg, limit=max(1, min(limit, 500)))}


@app.post("/operator/coder-runs/{run_id}/reconcile")
async def reconcile_operator_coder_run(run_id: str, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    try:
        return await asyncio.to_thread(reconcile_coder_run, cfg, run_id, coordination=store)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/operator/coder-runs/{run_id}/cancel")
async def cancel_operator_coder_run(
    run_id: str,
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    reason = str(payload.get("reason") or "cancelled by ACA operator").strip()
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    try:
        return await asyncio.to_thread(reconcile_coder_run, cfg, run_id, coordination=store, cancel_reason=reason)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/operator/dashboard", response_class=HTMLResponse)
async def get_operator_dashboard(limit: int = 25, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    summary = build_operator_summary(cfg, coordination=store, limit=max(1, min(limit, 100)))
    return render_operator_dashboard(summary)


@app.get("/scheduler/plan")
async def get_scheduler_plan(limit: int = 25, token: str = Depends(get_token)):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    snapshot = scheduler_snapshot(cfg, coordination=store, limit=max(1, min(limit, 100)))
    plan = plan_task_admissions(cfg, coordination=store, limit=max(1, min(limit, 100)))
    return {"snapshot": snapshot, "plan": plan}


@app.post("/scheduler/dispatch")
async def dispatch_scheduler_batch(
    limit: int = 25,
    wait: bool = False,
    token: str = Depends(get_token),
):
    root = Path(os.environ.get("ACA_ROOT", "."))
    cfg = resolve_config(root)
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    store.ensure_schema()
    result = dispatch_scheduled_runs(cfg, coordination=store, limit=max(1, min(limit, 100)), wait=wait)
    return result

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ACA_API_PORT", 39735)))
