from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.integrations.github_mcp import github_mcp_scope
from src.tandem_agents.core.engine.tandem_client_sdk import sdk_available, sdk_coder_create_run, sdk_coder_execute_all, sdk_coder_get_run


def _repo_has_user_files(repo: dict[str, Any] | None) -> bool:
    repo_path = Path(str((repo or {}).get("path") or "")).expanduser()
    if not repo_path.is_dir():
        return False
    for child in repo_path.iterdir():
        if child.name == ".git":
            continue
        return True
    return False


def _looks_like_bootstrap_task(task: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(task.get("title") or "").strip().lower(),
            str(task.get("description") or "").strip().lower(),
        ]
    )
    if not text.strip():
        return False
    keywords = (
        "set up",
        "setup",
        "app shell",
        "bootstrap",
        "initialize",
        "scaffold",
        "initial web app",
        "create the initial",
        "repo is currently empty",
        "repository is currently empty",
    )
    return any(keyword in text for keyword in keywords)


def _looks_like_feature_delivery_task(task: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(task.get("title") or "").strip().lower(),
            str(task.get("description") or "").strip().lower(),
        ]
    )
    if not text.strip():
        return False
    keywords = (
        "add ",
        "implement",
        "build ",
        "create ",
        "render",
        "ui",
        "frontend",
        "page",
        "screen",
        "layout",
        "todo",
        "list rendering",
        "app shell",
        "form",
        "button",
        "input area",
    )
    return any(keyword in text for keyword in keywords)


def coder_backend_mode(cfg: ResolvedConfig, task: dict[str, Any], repo: dict[str, Any] | None = None) -> str:
    backend = (cfg.execution.backend or "auto").strip().lower()
    if backend in {"legacy", "coder"}:
        return backend
    source = dict(task.get("source") or {})
    source_type = str(source.get("type") or cfg.task_source.type or "").strip().lower()
    issue_number = source.get("issue_number")
    if not (source_type == "github_project" and issue_number and _repo_has_user_files(repo)):
        return "legacy"
    if bool((repo or {}).get("dirty")):
        return "legacy"
    if _looks_like_bootstrap_task(task):
        return "legacy"
    if _looks_like_feature_delivery_task(task):
        return "legacy"
    if not sdk_available():
        return "legacy"
    if source_type == "github_project" and issue_number and _repo_has_user_files(repo):
        return "coder"
    return "legacy"


def coder_workflow_supported(task: dict[str, Any], repo: dict[str, Any] | None = None) -> bool:
    source = dict(task.get("source") or {})
    source_type = str(source.get("type") or "").strip().lower()
    issue_number = source.get("issue_number")
    return (
        source_type == "github_project"
        and bool(issue_number)
        and _repo_has_user_files(repo)
        and not bool((repo or {}).get("dirty"))
        and not _looks_like_bootstrap_task(task)
        and not _looks_like_feature_delivery_task(task)
    )


def ensure_coder_supported(cfg: ResolvedConfig, task: dict[str, Any], repo: dict[str, Any] | None = None) -> None:
    if not sdk_available():
        raise RuntimeError("Tandem Python SDK is required for coder execution mode.")
    if not coder_workflow_supported(task, repo):
        raise RuntimeError(
            "ACA coder execution currently supports GitHub Project tasks backed by a linked issue in a non-empty repository."
        )


def _repo_slug(repo: dict[str, Any], task: dict[str, Any]) -> str:
    source = dict(task.get("source") or {})
    owner = str(source.get("owner") or "").strip()
    repo_name = str(source.get("repo_name") or "").strip()
    if owner and repo_name:
        return f"{owner}/{repo_name}"
    return str(repo.get("slug") or "").strip()


def build_coder_run_payload(
    cfg: ResolvedConfig,
    *,
    run_id: str,
    repo: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    source = dict(task.get("source") or {})
    repo_slug = _repo_slug(repo, task)
    worker_provider, worker_model = cfg.provider_for_role("worker")
    payload: dict[str, Any] = {
        "coder_run_id": run_id,
        "workflow_mode": "issue_fix",
        "repo_binding": {
            "project_id": str(source.get("project") or "aca"),
            "workspace_id": "aca",
            "workspace_root": str(repo.get("path") or ""),
            "repo_slug": repo_slug,
            "default_branch": str(repo.get("default_branch") or cfg.repository.default_branch or "main"),
        },
        "github_ref": {
            "kind": "issue",
            "number": int(source.get("issue_number")),
            "url": source.get("issue_url"),
        },
        "objective": str(task.get("title") or "Issue fix").strip(),
        "source_client": "aca",
        "model_provider": worker_provider,
        "model_id": worker_model,
    }
    if github_mcp_scope(cfg, str(source.get("type") or cfg.task_source.type)) != "none":
        payload["mcp_servers"] = ["github"]
    return payload


def execute_coder_run(
    cfg: ResolvedConfig,
    *,
    run_id: str,
    repo: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    ensure_coder_supported(cfg, task, repo)
    payload = build_coder_run_payload(cfg, run_id=run_id, repo=repo, task=task)
    create_response = sdk_coder_create_run(cfg, payload)
    execute_response: dict[str, Any] = {}
    execute_error = ""
    try:
        response = sdk_coder_execute_all(cfg, run_id, {"max_steps": 16})
        execute_response = response if isinstance(response, dict) else {}
    except Exception as exc:
        # execute_all can time out at the HTTP layer even when the engine has
        # already accepted the run and is actively progressing it in the
        # background; keep polling the run state before treating this as fatal.
        execute_error = str(exc).strip() or repr(exc)
        execute_response = {"error": execute_error}
    run_response: dict[str, Any] = {}
    wait_timeout = max(1, int(getattr(cfg.execution, "coder_wait_timeout_seconds", 3600) or 3600))
    poll_interval = max(1, int(getattr(cfg.execution, "coder_poll_interval_seconds", 15) or 15))
    deadline = time.time() + float(wait_timeout)
    timed_out_waiting = False
    while time.time() < deadline:
        current = sdk_coder_get_run(cfg, run_id)
        run_response = current if isinstance(current, dict) else {}
        final_run = run_response.get("run") or {}
        status = str(final_run.get("status") or "").strip().lower()
        if status in {"completed", "failed", "blocked", "cancelled"}:
            break
        time.sleep(min(float(poll_interval), max(0.0, deadline - time.time())))
    else:
        timed_out_waiting = True
    coder_run = {}
    if isinstance(run_response, dict):
        coder_run = dict(run_response.get("coder_run") or {})
    final_run = {}
    if isinstance(run_response, dict):
        final_run = dict(run_response.get("run") or {})
    if not final_run and isinstance(execute_response, dict):
        final_run = dict(execute_response.get("run") or {})
    last_error = str(final_run.get("last_error") or "").strip()
    if not last_error and execute_error:
        last_error = execute_error
    status = str(final_run.get("status") or "").strip().lower()
    if timed_out_waiting and status not in {"completed", "failed", "blocked", "cancelled"}:
        last_error = (
            f"Coder run did not reach a terminal state within {wait_timeout}s; "
            "Tandem may still be executing it in the background."
        )
    artifacts: list[dict[str, Any]] = []
    if isinstance(run_response, dict):
        for key in ("coder_artifacts", "artifacts"):
            raw = run_response.get(key) or []
            if isinstance(raw, list):
                artifacts.extend(dict(item) for item in raw if isinstance(item, dict))
    return {
        "create_response": create_response,
        "execute_response": execute_response,
        "run_response": run_response,
        "coder_run": coder_run,
        "run": final_run,
        "artifacts": artifacts,
        "status": status,
        "phase": str(final_run.get("phase") or "").strip().lower(),
        "last_error": last_error,
        "monitor_timeout": timed_out_waiting,
        "wait_timeout_seconds": wait_timeout,
        "poll_interval_seconds": poll_interval,
    }


def build_coder_summary(
    *,
    run_id: str,
    task: dict[str, Any],
    repo: dict[str, Any],
    engine_label: str,
    provider_id: str,
    provider_model: str,
    coder_result: dict[str, Any],
) -> str:
    artifacts = coder_result.get("artifacts") or []
    lines = [
        "# Coder run completed",
        "",
        f"- ACA Run ID: `{run_id}`",
        f"- Task: {task.get('title') or 'Untitled task'}",
        f"- Repo: `{repo.get('path') or ''}`",
        f"- Engine: `{engine_label}`",
        f"- Provider: `{provider_id}` / `{provider_model}`",
        f"- Coder workflow: `issue_fix`",
        f"- Coder status: `{coder_result.get('status') or 'unknown'}`",
        f"- Coder phase: `{coder_result.get('phase') or 'unknown'}`",
        "",
    ]
    if coder_result.get("last_error"):
        lines.extend(["## Last Error", "", coder_result["last_error"], ""])
    lines.extend(["## Artifacts", ""])
    if artifacts:
        for artifact in artifacts[:20]:
            path = str(artifact.get("path") or "").strip()
            artifact_type = str(artifact.get("artifact_type") or artifact.get("type") or "artifact").strip()
            lines.append(f"- `{artifact_type}`: `{path}`")
    else:
        lines.append("- _none reported_")
    return "\n".join(lines)
