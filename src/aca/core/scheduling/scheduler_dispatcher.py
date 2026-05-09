from __future__ import annotations

import threading
from typing import Any

from src.aca.config.config import resolve_config, validate_config
from src.aca.config.config_types import ResolvedConfig
from src.aca.core.coordination.coordination import CoordinationStore, default_host_id, short_id
from src.aca.core.execution.runtime_entrypoints import run_worker
from src.aca.core.scheduling.scheduler import plan_task_admissions
from src.aca.runtime.workspace_registry import load_workspace, record_run_reference, save_workspace
from src.aca.utils.utils import slugify
from src.aca.runtime.runstate import new_run_id

_WORKSPACE_LOCK = threading.Lock()


def _nonempty(value: Any) -> str:
    return str(value or "").strip()


def _task_source_overrides(task: dict[str, Any]) -> dict[str, str]:
    metadata = dict(task.get("metadata") or {})
    source_task = dict(metadata.get("task") or {})
    source = dict(task.get("source") or source_task.get("source") or {})
    repo = dict(task.get("repo") or source_task.get("repo") or {})
    overrides: dict[str, str] = {}
    source_type = _nonempty(source.get("type")) or _nonempty(task.get("source_type"))
    if source_type:
        overrides["ACA_TASK_SOURCE_TYPE"] = source_type
    for key, env_name in [
        ("owner", "ACA_TASK_SOURCE_OWNER"),
        ("repo", "ACA_TASK_SOURCE_REPO"),
        ("project", "ACA_TASK_SOURCE_PROJECT"),
        ("item", "ACA_TASK_SOURCE_ITEM"),
        ("url", "ACA_TASK_SOURCE_URL"),
        ("path", "ACA_TASK_SOURCE_PATH"),
        ("prompt", "ACA_TASK_SOURCE_PROMPT"),
        ("source_name", "ACA_TASK_SOURCE_SOURCE_NAME"),
        ("card_id", "ACA_TASK_SOURCE_CARD_ID"),
    ]:
        value = _nonempty(source.get(key))
        if value:
            overrides[env_name] = value
    repo_slug = _nonempty(repo.get("slug")) or _nonempty(task.get("repo_slug"))
    repo_path = _nonempty(repo.get("path")) or _nonempty(task.get("repo_path"))
    repo_url = _nonempty(repo.get("clone_url")) or _nonempty(task.get("repo_url"))
    if repo_slug:
        overrides["ACA_REPO_SLUG"] = repo_slug
    if repo_path:
        overrides["ACA_REPO_PATH"] = repo_path
    if repo_url:
        overrides["ACA_REPO_URL"] = repo_url
    return overrides


def _task_run_env(cfg: ResolvedConfig, task: dict[str, Any], run_id: str) -> dict[str, str]:
    env = dict(cfg.env)
    env.update(_task_source_overrides(task))
    env["ACA_RUN_ID"] = run_id
    env["ACA_COORDINATION_ROLE"] = "worker"
    env["ACA_RUNTIME_ROLE"] = "worker"
    execution_backend = _nonempty(task.get("execution_backend")).lower()
    if execution_backend in {"auto", "legacy", "coder"}:
        env["ACA_EXECUTION_BACKEND"] = execution_backend
    env["ACA_WORKER_ID"] = f"scheduler-{slugify(task.get('task_key') or task.get('title') or run_id)}-{short_id()}"
    env["ACA_HOST_ID"] = default_host_id(cfg)
    return env


def _workspace_project_id(task_item: dict[str, Any]) -> str:
    return (
        _nonempty(task_item.get("project_id"))
        or _nonempty(task_item.get("project_key"))
        or _nonempty(dict(task_item.get("task") or {}).get("project_id"))
        or _nonempty(dict(task_item.get("task") or {}).get("project_key"))
        or _nonempty(task_item.get("task_key"))
        or "task"
    )


def _record_workspace_run(cfg: ResolvedConfig, task_item: dict[str, Any], run_id: str, *, status: str) -> None:
    with _WORKSPACE_LOCK:
        workspace = load_workspace(cfg.root_dir)
        task = dict(task_item.get("task") or {})
        execution_backend = _nonempty(task_item.get("execution_backend"))
        execution_path = "tandem_coder" if execution_backend == "coder" else "aca_admission_only"
        updated = record_run_reference(
            workspace,
            run_id=run_id,
            project_id=_workspace_project_id(task_item),
            project_key=_nonempty(task_item.get("project_key")),
            status=status,
            execution_backend=execution_backend,
            admission_role="aca_scheduler",
            execution_path=execution_path,
            task_key=_nonempty(task_item.get("task_key")),
            task_title=_nonempty(task.get("title") or task_item.get("title")),
        )
        save_workspace(cfg.root_dir, updated)


def dispatch_scheduled_runs(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    plan = plan_task_admissions(cfg, coordination=store, limit=limit)
    admitted = list(plan.get("admitted") or [])
    launched: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    launch_errors: list[dict[str, Any]] = []
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    def _run(task_item: dict[str, Any]) -> None:
        task = dict(task_item.get("task") or {})
        run_id = str(task_item.get("run_id") or "")
        execution_backend = task_item.get("execution_backend")
        execution_path = "tandem_coder" if _nonempty(execution_backend).lower() == "coder" else "aca_admission_only"
        try:
            run_cfg = resolve_config(cfg.root_dir, env=_task_run_env(cfg, task, run_id))
            errors = validate_config(run_cfg)
            if errors:
                raise RuntimeError("; ".join(errors))
            result = run_worker(run_cfg)
            _record_workspace_run(cfg, task_item, run_id, status="completed")
            with lock:
                completed.append(
                    {
                        "run_id": run_id,
                        "task_key": task_item.get("task_key"),
                        "project_key": task_item.get("project_key"),
                        "execution_backend": execution_backend,
                        "execution_path": execution_path,
                        "result": result,
                    }
                )
        except Exception as exc:
            _record_workspace_run(cfg, task_item, run_id, status="failed")
            with lock:
                launch_errors.append(
                    {
                        "run_id": run_id,
                        "task_key": task_item.get("task_key"),
                        "project_key": task_item.get("project_key"),
                        "execution_backend": execution_backend,
                        "execution_path": execution_path,
                        "error": str(exc),
                    }
                )

    for task_item in admitted:
        run_id = new_run_id(prefix="sched")
        task_item["run_id"] = run_id
        execution_backend = task_item.get("execution_backend")
        execution_path = "tandem_coder" if _nonempty(execution_backend).lower() == "coder" else "aca_admission_only"
        _record_workspace_run(cfg, task_item, run_id, status="starting")
        launched.append(
            {
                "run_id": run_id,
                "task_key": task_item.get("task_key"),
                "project_key": task_item.get("project_key"),
                "repo_key": task_item.get("repo_key"),
                "execution_backend": execution_backend,
                "execution_path": execution_path,
            }
        )
        thread = threading.Thread(target=_run, args=(task_item,), daemon=not wait)
        threads.append(thread)
        thread.start()

    if wait:
        for thread in threads:
            thread.join()

    dispatch_payload = {
        "policy": plan.get("policy"),
        "limits": plan.get("limits"),
        "started": launched,
        "completed": completed,
        "errors": launch_errors,
        "waited": wait,
    }
    store.record_scheduler_event("scheduler.dispatch", dispatch_payload)
    return {
        "plan": plan,
        "started": launched,
        "completed": completed,
        "errors": launch_errors,
        "waited": wait,
    }
