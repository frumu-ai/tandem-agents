from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def run_linear_graph_dogfood(
    *,
    root: Path,
    api_url: str,
    project_slug: str,
    item: str | None,
    token_file: Path | None = None,
    token: str | None = None,
    overrides: dict[str, str] | None = None,
    wait_seconds: int = 180,
    poll_seconds: float = 2.0,
    expect_graph: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Trigger one ACA run and assert whether planning used repo.context_bundle."""

    api_url = api_url.rstrip("/")
    auth_token = _resolve_token(root=root, token_file=token_file, token=token)
    run_id = _trigger_run(
        api_url=api_url,
        token=auth_token,
        project_slug=project_slug,
        item=item,
        overrides=overrides or {},
    )
    deadline = time.monotonic() + max(1, wait_seconds)
    last_snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_snapshot = _get_json(f"{api_url}/runs/{run_id}", token=auth_token)
        repo_context = _repo_context_from_snapshot(last_snapshot)
        if repo_context:
            summary = _summary(run_id, project_slug, last_snapshot, repo_context)
            ok = _graph_assertion_ok(repo_context) if expect_graph else True
            summary["graph_assertion"] = "passed" if ok else "failed"
            return (0 if ok else 1), summary
        run_status = str((last_snapshot.get("status") or {}).get("run", {}).get("status") or "").strip()
        if run_status in {"blocked", "failed", "completed"}:
            break
        time.sleep(max(0.25, poll_seconds))

    repo_context = _repo_context_from_snapshot(last_snapshot)
    summary = _summary(run_id, project_slug, last_snapshot, repo_context)
    summary["graph_assertion"] = "missing_repo_context" if expect_graph else "not_required"
    return (1 if expect_graph else 0), summary


def _resolve_token(*, root: Path, token_file: Path | None, token: str | None) -> str:
    if token:
        return token.strip()
    env_token = str(os.environ.get("ACA_API_TOKEN") or "").strip()
    if env_token:
        return env_token
    path = token_file or root / "tandem-data" / "aca_api_token"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not read ACA API token file: {path}") from exc
    if not value:
        raise RuntimeError(f"ACA API token file is empty: {path}")
    return value


def _request_json(url: str, *, token: str, method: str = "GET", payload: Any | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ACA API returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach ACA API: {exc}") from exc
    return json.loads(raw) if raw.strip() else {}


def _get_json(url: str, *, token: str) -> dict[str, Any]:
    payload = _request_json(url, token=token)
    return payload if isinstance(payload, dict) else {}


def _trigger_run(
    *,
    api_url: str,
    token: str,
    project_slug: str,
    item: str | None,
    overrides: dict[str, str],
) -> str:
    query = {"project_slug": project_slug}
    if item:
        query["item"] = item
    payload = _request_json(
        f"{api_url}/runs/trigger?{urlencode(query)}",
        token=token,
        method="POST",
        payload=overrides,
    )
    run_id = str((payload or {}).get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError(f"ACA trigger response did not include run_id: {payload}")
    return run_id


def _repo_context_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    repo_context = snapshot.get("repo_context")
    if isinstance(repo_context, dict) and repo_context:
        return repo_context
    status = snapshot.get("status")
    if isinstance(status, dict) and isinstance(status.get("repo_context"), dict):
        return status["repo_context"]
    events = snapshot.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("repo_context"), dict):
                return payload["repo_context"]
    return {}


def _graph_assertion_ok(repo_context: dict[str, Any]) -> bool:
    return (
        str(repo_context.get("source") or "").strip() == "repo.context_bundle"
        and repo_context.get("fallback_used") is False
    )


def _summary(
    run_id: str,
    project_slug: str,
    snapshot: dict[str, Any],
    repo_context: dict[str, Any],
) -> dict[str, Any]:
    status = snapshot.get("status") if isinstance(snapshot.get("status"), dict) else {}
    task = status.get("task") if isinstance(status.get("task"), dict) else {}
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    repair = snapshot.get("repair") if isinstance(snapshot.get("repair"), dict) else status.get("repair") or {}
    events = snapshot.get("events") if isinstance(snapshot.get("events"), list) else []
    first_failure = next(
        (
            event
            for event in events
            if isinstance(event, dict)
            and str(event.get("type") or "").endswith((".failed", ".blocked"))
        ),
        None,
    )
    return {
        "run_id": run_id,
        "project_slug": project_slug,
        "task_id": task.get("id") or task.get("task_id"),
        "task_title": task.get("title"),
        "run_status": run.get("status") or snapshot.get("status"),
        "repo_context": repo_context,
        "partial_diff_state": repair.get("partial_diff_state"),
        "partial_diff_artifacts": repair.get("partial_diff_artifacts") or [],
        "first_failure": first_failure,
    }
