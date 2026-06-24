from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import (
    connect_mcp_server as _connect_mcp_server,
    disconnect_mcp_server as _disconnect_mcp_server,
    execute_engine_tool,
    list_engine_tool_ids,
    list_mcp_servers as _list_mcp_servers,
    set_mcp_enabled as _set_engine_mcp_enabled,
)


def normalize_status_key(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


GITHUB_PROJECT_ACTIONABLE_STATUS_KEYS = {"ready", "backlog", "todo", "todos"}
GITHUB_PROJECT_TERMINAL_STATUS_KEYS = {"done", "completed", "closed"}
GITHUB_PROJECT_ACTIVE_STATUS_KEYS = {
    "claimed",
    "active",
    "running",
    "in_progress",
    "planning",
    "worker_execution",
    "coder_execution",
    "review",
    "testing",
}
GITHUB_PROJECT_STATUS_BY_TASK_STATE = {
    "ready": "Ready",
    "queued": "Ready",
    "backlog": "Backlog",
    "todo": "Todo",
    "claimed": "In progress",
    "active": "In progress",
    "running": "In progress",
    "in_progress": "In progress",
    "planning": "In progress",
    "worker_execution": "In progress",
    "coder_execution": "In progress",
    "review": "In review",
    "testing": "Testing",
    "blocked": "Blocked",
    "stale": "Blocked",
    "failed": "Blocked",
    "cancelled": "Blocked",
    "done": "Done",
}

GITHUB_PROJECT_OPERATOR_ACTIONS = {
    "connect_github_project": "Connect GitHub Project",
    "resync_outward": "Re-sync outward",
    "ignore_remote_drift": "Ignore remote drift",
    "start_new_run_from_reopened_item": "Start new run from reopened item",
}


def github_project_status_name_for_task_state(task_state: str | None) -> str:
    key = normalize_status_key(task_state)
    return GITHUB_PROJECT_STATUS_BY_TASK_STATE.get(key, "Backlog")


def github_project_status_name_for_outcome(outcome: str | None) -> str:
    key = normalize_status_key(outcome)
    if key == "completed":
        return "Review"
    if key == "blocked":
        return "Blocked"
    if key in {"failed", "cancelled"}:
        return "Blocked"
    if key == "done":
        return "Done"
    return github_project_status_name_for_task_state(key)


def github_project_status_key_is_actionable(status_name: str | None) -> bool:
    return normalize_status_key(status_name) in GITHUB_PROJECT_ACTIONABLE_STATUS_KEYS


def github_project_operator_actions() -> list[dict[str, str]]:
    return [
        {
            "id": "connect_github_project",
            "label": GITHUB_PROJECT_OPERATOR_ACTIONS["connect_github_project"],
            "description": "Connect or refresh the GitHub MCP server with Project read/write scopes.",
        },
        {
            "id": "resync_outward",
            "label": GITHUB_PROJECT_OPERATOR_ACTIONS["resync_outward"],
            "description": "Apply ACA's intended status or comment to the remote GitHub Project item.",
        },
        {
            "id": "ignore_remote_drift",
            "label": GITHUB_PROJECT_OPERATOR_ACTIONS["ignore_remote_drift"],
            "description": "Keep ACA's local run state and leave the remote Project item unchanged.",
        },
        {
            "id": "start_new_run_from_reopened_item",
            "label": GITHUB_PROJECT_OPERATOR_ACTIONS["start_new_run_from_reopened_item"],
            "description": "Treat a reopened actionable Project item as a fresh task source candidate.",
        },
    ]


def github_projects_readiness_message(
    mode: str,
    detail: str,
    *,
    actions: list[str] | tuple[str, ...] | None = None,
) -> str:
    mode_text = str(mode or "read").strip().lower()
    detail_text = str(detail or "capability unavailable").strip()
    selected_actions = list(actions or ["connect_github_project"])
    labels = [
        GITHUB_PROJECT_OPERATOR_ACTIONS.get(action, action.replace("_", " "))
        for action in selected_actions
        if action
    ]
    suffix = f" Operator actions: {', '.join(labels)}." if labels else ""
    return f"GitHub Projects {mode_text} readiness degraded: {detail_text}.{suffix}"


def _github_project_status_cache_path(cfg: ResolvedConfig) -> Path:
    return cfg.output_root() / "state" / "github_project_statuses.json"


def _github_project_cache_key(owner: str, project_number: int | str, item_id: int | str) -> str:
    return f"{str(owner).strip().lower()}:{int(project_number)}:{int(item_id)}"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _bootstrap_project_status_cache(cfg: ResolvedConfig) -> dict[str, Any]:
    cache: dict[str, Any] = {}
    output_root = cfg.output_root()
    if not output_root.exists():
        return cache
    for run_dir in sorted(output_root.glob("run-*")):
        status_path = run_dir / "status.json"
        events_path = run_dir / "events.jsonl"
        try:
            status_payload = _load_json(status_path)
        except Exception:
            status_payload = {}
        run_status = str(
            ((status_payload.get("run") or {}).get("status"))
            or status_payload.get("status")
            or ""
        ).strip().lower()
        confidence = 2 if run_status == "completed" else 1
        source = dict(((status_payload.get("task") or {}).get("source") or {}))
        if str(source.get("type") or "") != "github_project":
            continue
        owner = str(source.get("owner") or "").strip()
        project = source.get("project")
        item_id = source.get("project_item_id")
        if not owner or project in (None, "") or item_id in (None, ""):
            continue
        latest_status = ""
        try:
            for raw_line in events_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("type") != "github_project.status_updated":
                    continue
                payload = row.get("payload")
                if isinstance(payload, dict):
                    latest_status = str(payload.get("status") or latest_status).strip()
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        if not latest_status:
            latest_status = str(source.get("status") or source.get("initial_status_name") or "").strip()
        if not latest_status:
            continue
        key = _github_project_cache_key(owner, project, item_id)
        existing = cache.get(key)
        existing_confidence = int(existing.get("_confidence") or 0) if isinstance(existing, dict) else 0
        if existing_confidence > confidence:
            continue
        cache[key] = {
            "owner": owner,
            "project": int(project),
            "project_item_id": int(item_id),
            "status_name": latest_status,
            "status_key": normalize_status_key(latest_status),
            "source": f"bootstrap:{run_dir.name}",
            "_confidence": confidence,
        }
    return cache


def _load_project_status_cache(cfg: ResolvedConfig) -> dict[str, Any]:
    cache_path = _github_project_status_cache_path(cfg)
    cache = _load_json(cache_path)
    if cache:
        return cache
    bootstrapped = _bootstrap_project_status_cache(cfg)
    if bootstrapped:
        _write_json(cache_path, bootstrapped)
    return bootstrapped


def remember_project_item_status(
    cfg: ResolvedConfig,
    *,
    owner: str,
    project_number: int | str,
    item_id: int | str,
    status_name: str,
    source: str = "runtime",
) -> None:
    owner_text = str(owner).strip()
    status_text = str(status_name or "").strip()
    if not owner_text or project_number in (None, "") or item_id in (None, "") or not status_text:
        return
    cache = _load_project_status_cache(cfg)
    key = _github_project_cache_key(owner_text, project_number, item_id)
    cache[key] = {
        "owner": owner_text,
        "project": int(project_number),
        "project_item_id": int(item_id),
        "status_name": status_text,
        "status_key": normalize_status_key(status_text),
        "source": source,
        "updated_at_epoch_ms": int(time.time() * 1000),
    }
    _write_json(_github_project_status_cache_path(cfg), cache)


def cached_project_item_status(
    cfg: ResolvedConfig,
    *,
    owner: str,
    project_number: int | str,
    item_id: int | str,
) -> str:
    owner_text = str(owner).strip()
    if not owner_text or project_number in (None, "") or item_id in (None, ""):
        return ""
    cache = _load_project_status_cache(cfg)
    key = _github_project_cache_key(owner_text, project_number, item_id)
    record = cache.get(key)
    if not isinstance(record, dict):
        return ""
    return str(record.get("status_name") or "").strip()


def github_mcp_scope(cfg: ResolvedConfig, source_type: str) -> str:
    if not cfg.github_mcp.enabled:
        return "none"
    scope = (cfg.github_mcp.scope or "none").strip().lower()
    if source_type != "github_project" and scope != "always":
        return "none"
    return scope


def github_remote_sync_mode(cfg: ResolvedConfig, source_type: str) -> str:
    if github_mcp_scope(cfg, source_type) == "none":
        return "off"
    if source_type != "github_project":
        return "off"
    return (cfg.github_mcp.remote_sync or "off").strip().lower()


def list_mcp_servers(cfg: ResolvedConfig) -> dict[str, Any]:
    return _list_mcp_servers(cfg)


def get_mcp_server(cfg: ResolvedConfig, name: str) -> dict[str, Any] | None:
    payload = list_mcp_servers(cfg)
    server = payload.get(name)
    return server if isinstance(server, dict) else None


def _tool_ids(cfg: ResolvedConfig) -> list[str]:
    return list_engine_tool_ids(cfg)


def _set_mcp_enabled(cfg: ResolvedConfig, name: str, enabled: bool) -> dict[str, Any]:
    return _set_engine_mcp_enabled(cfg, name, enabled)


def connect_mcp_server(cfg: ResolvedConfig, name: str) -> dict[str, Any]:
    return _connect_mcp_server(cfg, name)


def disconnect_mcp_server(cfg: ResolvedConfig, name: str) -> dict[str, Any]:
    return _disconnect_mcp_server(cfg, name)


def ensure_github_mcp_connected(cfg: ResolvedConfig) -> dict[str, Any]:
    server = get_mcp_server(cfg, "github")
    if server is None:
        raise RuntimeError("GitHub MCP server is not configured in the connected Tandem engine.")
    if not server.get("enabled"):
        _set_mcp_enabled(cfg, "github", True)
        server = get_mcp_server(cfg, "github") or server
    if not server.get("connected"):
        connect_mcp_server(cfg, "github")
    deadline = time.time() + 10.0
    require_projects = "projects" in {part.strip().lower() for part in (cfg.github_mcp.toolsets or "").split(",") if part.strip()}
    while time.time() < deadline:
        server = get_mcp_server(cfg, "github") or server
        if not server.get("connected"):
            time.sleep(0.25)
            continue
        if not require_projects:
            return server
        ids = _tool_ids(cfg)
        if "mcp.github.projects_get" in ids and "mcp.github.projects_list" in ids:
            return server
        time.sleep(0.25)
    return server


def ensure_github_mcp_disconnected(cfg: ResolvedConfig) -> dict[str, Any] | None:
    server = get_mcp_server(cfg, "github")
    if server is None:
        return None
    if server.get("connected"):
        disconnect_mcp_server(cfg, "github")
        server = get_mcp_server(cfg, "github") or server
    return server


def _tool_failed(result: dict[str, Any]) -> bool:
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        inner = metadata.get("result")
        if isinstance(inner, dict) and inner.get("isError") is True:
            return True
    output = str(result.get("output") or "").strip().lower()
    return (
        output.startswith("failed")
        or output.startswith("unknown method")
        or output.startswith("unknown tool")
        or output.startswith("missing required")
    )


def _tool_error_message(result: dict[str, Any]) -> str:
    output = str(result.get("output") or "").strip()
    if output:
        return output
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        inner = metadata.get("result")
        if isinstance(inner, dict):
            content = inner.get("content")
            if isinstance(content, list):
                for entry in content:
                    if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry["text"].strip():
                        return entry["text"].strip()
    return "unknown GitHub MCP error"


def _parse_json_output(result: dict[str, Any]) -> Any:
    output = result.get("output")
    if isinstance(output, str) and output.strip():
        try:
            parsed = json.loads(output)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        inner = metadata.get("result")
        if isinstance(inner, dict):
            content = inner.get("content")
            if isinstance(content, list):
                for entry in content:
                    if not isinstance(entry, dict):
                        continue
                    text = entry.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        continue
                    if isinstance(parsed, (dict, list)):
                        return parsed
    return {}


def _flatten_comment_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("comments", "items", "nodes", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
            elif isinstance(value, dict) and isinstance(value.get("nodes"), list):
                entries.extend(entry for entry in value["nodes"] if isinstance(entry, dict))
    return entries


def _comment_body_text(comment: dict[str, Any]) -> str:
    for key in ("body", "text", "content", "message"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _comment_url_text(comment: dict[str, Any]) -> str:
    for key in ("html_url", "url", "web_url"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _flatten_pull_request_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("pullRequests", "pull_requests", "items", "nodes", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
            elif isinstance(value, dict) and isinstance(value.get("nodes"), list):
                entries.extend(entry for entry in value["nodes"] if isinstance(entry, dict))
    return entries


def _flatten_pull_request_file_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("files", "pullRequestFiles", "pull_request_files", "items", "nodes", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
            elif isinstance(value, dict) and isinstance(value.get("nodes"), list):
                entries.extend(entry for entry in value["nodes"] if isinstance(entry, dict))
    return entries


def _flatten_review_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("reviews", "pullRequestReviews", "pull_request_reviews", "items", "nodes", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
            elif isinstance(value, dict) and isinstance(value.get("nodes"), list):
                entries.extend(entry for entry in value["nodes"] if isinstance(entry, dict))
    return entries


def _flatten_review_comment_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("comments", "reviewComments", "pull_request_review_comments", "items", "nodes", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
            elif isinstance(value, dict) and isinstance(value.get("nodes"), list):
                entries.extend(entry for entry in value["nodes"] if isinstance(entry, dict))
    return entries


def _pull_request_head_ref(pr: dict[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, dict):
        for key in ("ref", "name", "branch"):
            value = head.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("headRefName", "head_ref_name", "head_branch", "ref"):
        value = pr.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pull_request_url(pr: dict[str, Any]) -> str:
    for key in ("html_url", "url", "web_url"):
        value = pr.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pull_request_number(pr: dict[str, Any]) -> int | None:
    for key in ("number", "pull_number", "pullNumber"):
        value = pr.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    match = re.search(r"/pull/(\d+)(?:\D*$|$)", _pull_request_url(pr))
    if match:
        return int(match.group(1))
    return None


def _pull_request_base_ref(pr: dict[str, Any]) -> str:
    base = pr.get("base")
    if isinstance(base, dict):
        for key in ("ref", "name", "branch"):
            value = base.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("baseRefName", "base_ref_name", "base_branch", "base"):
        value = pr.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pull_request_base_repo(pr: dict[str, Any], fallback: str = "") -> str:
    base = pr.get("base")
    repo = base.get("repo") if isinstance(base, dict) else None
    if isinstance(repo, dict):
        for key in ("full_name", "nameWithOwner", "name_with_owner"):
            value = repo.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        owner = repo.get("owner")
        owner_name = ""
        if isinstance(owner, dict):
            owner_name = str(owner.get("login") or owner.get("name") or "").strip()
        repo_name = str(repo.get("name") or "").strip()
        if owner_name and repo_name:
            return f"{owner_name}/{repo_name}"
    for key in ("base_repo", "baseRepo", "repository", "repo"):
        value = pr.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _pull_request_reviews_state(pr: dict[str, Any]) -> str:
    for key in ("reviewDecision", "review_decision", "review_state", "reviewState"):
        value = normalize_status_key(str(pr.get(key) or ""))
        if value:
            if value in {"changes_requested", "requested_changes"}:
                return "changes_requested"
            if value in {"approved", "approve"}:
                return "approved"
            if value in {"review_required", "requires_review"}:
                return "review_required"
            return value
    reviews = pr.get("reviews") or pr.get("reviewThreads") or []
    if isinstance(reviews, dict):
        reviews = reviews.get("nodes") or reviews.get("items") or []
    states: list[str] = []
    if isinstance(reviews, list):
        for review in reviews:
            if not isinstance(review, dict):
                continue
            state = normalize_status_key(str(review.get("state") or review.get("status") or ""))
            if state:
                states.append(state)
    if any(state in {"changes_requested", "requested_changes"} for state in states):
        return "changes_requested"
    if any(state in {"approved", "approve"} for state in states):
        return "approved"
    return "review_required"


def _flatten_check_entries(value: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        for key in (
            "checks",
            "check_runs",
            "checkRuns",
            "checkSuites",
            "statusCheckRollup",
            "status_check_rollup",
            "nodes",
            "items",
        ):
            nested = value.get(key)
            if isinstance(nested, list):
                entries.extend(entry for entry in nested if isinstance(entry, dict))
            elif isinstance(nested, dict):
                entries.extend(_flatten_check_entries(nested))
    return entries


def _pull_request_check_entries(pr: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key in ("checks", "check_runs", "checkSuites", "statusCheckRollup", "status_check_rollup", "combined_status"):
        entries.extend(_flatten_check_entries(pr.get(key)))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        name = str(entry.get("name") or entry.get("context") or entry.get("title") or "").strip()
        url = str(entry.get("detailsUrl") or entry.get("details_url") or entry.get("url") or "").strip()
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _pull_request_checks_state(pr: dict[str, Any]) -> str:
    for key in ("checks_status", "check_status", "checksState", "checks_state"):
        value = normalize_status_key(str(pr.get(key) or ""))
        if value:
            if value in {"success", "successful", "passed", "pass"}:
                return "success"
            if value in {"failure", "failed", "error", "cancelled", "canceled", "timed_out"}:
                return "failure"
            if value in {"pending", "queued", "in_progress", "running", "waiting"}:
                return "pending"
            return value
    rollup = pr.get("statusCheckRollup") or pr.get("status_check_rollup") or pr.get("combined_status")
    if isinstance(rollup, dict):
        value = normalize_status_key(str(rollup.get("state") or rollup.get("status") or rollup.get("conclusion") or ""))
        if value in {"success", "successful", "passed", "pass"}:
            return "success"
        if value in {"failure", "failed", "error", "cancelled", "canceled", "timed_out"}:
            return "failure"
        if value in {"pending", "queued", "in_progress", "running", "waiting", "expected"}:
            return "pending"
    checks = _pull_request_check_entries(pr)
    if checks:
        states = [
            normalize_status_key(str((check or {}).get("conclusion") or (check or {}).get("status") or ""))
            for check in checks
            if isinstance(check, dict)
        ]
        if any(state in {"failure", "failed", "error", "cancelled", "canceled", "timed_out"} for state in states):
            return "failure"
        if any(state in {"pending", "queued", "in_progress", "running", "waiting", "expected", "requested"} for state in states):
            return "pending"
        known_states = [state for state in states if state]
        if known_states and all(state in {"success", "successful", "passed", "completed", "neutral", "skipped"} for state in known_states):
            return "success"
        return "pending"
    return "unknown"


def pull_request_lifecycle_state(pr: dict[str, Any]) -> str:
    state = normalize_status_key(str(pr.get("state") or "open"))
    merged = bool(pr.get("merged") or pr.get("merged_at") or pr.get("mergedAt"))
    draft = bool(pr.get("draft") or pr.get("isDraft"))
    review_state = _pull_request_reviews_state(pr)
    checks_state = _pull_request_checks_state(pr)
    if merged:
        return "merged"
    if state in {"closed", "cancelled", "canceled"}:
        return "blocked"
    if review_state == "changes_requested" or checks_state == "failure":
        return "needs-repair"
    if draft or checks_state == "pending":
        return "running"
    if review_state == "approved" and checks_state == "success":
        return "ready-to-merge"
    return "waiting-for-review"


def normalize_pull_request_metadata(
    pr: dict[str, Any],
    *,
    head_branch: str = "",
    base_repo: str = "",
    base_branch: str = "",
) -> dict[str, Any]:
    number = _pull_request_number(pr)
    url = _pull_request_url(pr)
    normalized: dict[str, Any] = {
        "url": url,
        "number": number,
        "head_branch": _pull_request_head_ref(pr) or head_branch,
        "base_branch": _pull_request_base_ref(pr) or base_branch,
        "base_repo": _pull_request_base_repo(pr, fallback=base_repo),
        "state": str(pr.get("state") or "open").strip().lower() or "open",
        "draft": bool(pr.get("draft") or pr.get("isDraft")),
        "merged": bool(pr.get("merged") or pr.get("merged_at") or pr.get("mergedAt")),
        "review_state": _pull_request_reviews_state(pr),
        "checks_state": _pull_request_checks_state(pr),
    }
    normalized["lifecycle_state"] = pull_request_lifecycle_state(pr)
    normalized["terminal"] = normalized["lifecycle_state"] in {"merged", "blocked"}
    return normalized


def _tool_result_payloads(result: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and "result" in metadata:
        values.append(metadata["result"])
    output = result.get("output")
    if isinstance(output, str) and output.strip():
        try:
            values.append(json.loads(output))
        except Exception:
            pass
    return values


def _execute_github_tool_attempts(
    cfg: ResolvedConfig,
    attempts: list[tuple[str, dict[str, Any]]],
) -> list[Any]:
    for tool, args in attempts:
        try:
            result = execute_engine_tool(cfg, tool, args)
        except RuntimeError:
            continue
        if _tool_failed(result):
            continue
        return _tool_result_payloads(result)
    return []


def _issue_comment_marker(run_id: str) -> str:
    run_text = str(run_id or "").strip()
    return f"<!-- aca:issue-comment:{run_text} -->" if run_text else ""


def _pull_request_marker(run_id: str, head_branch: str) -> str:
    run_text = str(run_id or "").strip()
    branch_text = str(head_branch or "").strip()
    if not run_text or not branch_text:
        return ""
    return f"<!-- aca:pull-request:{run_text}:{branch_text} -->"


def _project_field_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("raw", "html", "text", "title", "name", "value"):
            text = _project_field_text(value.get(key))
            if text:
                return text
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _project_item_status_name(value: Any) -> str:
    if isinstance(value, dict):
        status = value.get("status")
        if isinstance(status, dict):
            name = status.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        elif isinstance(status, str) and status.strip():
            return status.strip()
        status_name = value.get("status_name") or value.get("statusName")
        if isinstance(status_name, str) and status_name.strip():
            return status_name.strip()
        field_values = value.get("field_values") or value.get("fieldValues")
        if isinstance(field_values, dict):
            nested = field_values.get("status")
            if isinstance(nested, dict):
                name = nested.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        elif isinstance(field_values, list):
            for field in field_values:
                if not isinstance(field, dict):
                    continue
                if normalize_status_key(_project_field_text(field.get("name"))) != "status":
                    continue
                status_name = _project_field_text(
                    field.get("value")
                    or field.get("name")
                    or field.get("option")
                    or field.get("displayValue")
                )
                if status_name:
                    return status_name
        fields = value.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                if normalize_status_key(_project_field_text(field.get("name"))) != "status":
                    continue
                status_name = _project_field_text(field.get("value") or field.get("name") or field.get("option"))
                if status_name:
                    return status_name
        content = value.get("content")
        if isinstance(content, dict):
            status_name = _project_item_status_name(content)
            if status_name:
                return status_name
        for nested in value.values():
            status_name = _project_item_status_name(nested)
            if status_name:
                return status_name
    elif isinstance(value, list):
        for row in value:
            status_name = _project_item_status_name(row)
            if status_name:
                return status_name
    return ""


def update_project_item_status(cfg: ResolvedConfig, task: dict[str, Any], status_name: str) -> str | None:
    source = dict(task.get("source") or {})
    status_field_id = source.get("status_field_id")
    project_item_id = source.get("project_item_id")
    option_map = dict(source.get("status_option_map") or {})
    option_id = option_map.get(normalize_status_key(status_name))
    if not status_field_id or not project_item_id or not option_id:
        missing = []
        if not status_field_id:
            missing.append("status_field_id")
        if not project_item_id:
            missing.append("project_item_id")
        if not option_id:
            missing.append(f"status option for '{status_name}'")
        return github_projects_readiness_message(
            "write",
            "missing GitHub Project status metadata for target status "
            f"'{status_name}' ({', '.join(missing)})",
            actions=["connect_github_project"],
        )
    current_status = str(
        cached_project_item_status(
            cfg,
            owner=str(source.get("owner") or ""),
            project_number=source.get("project") or 0,
            item_id=project_item_id,
        )
    ).strip()
    live_status = ""
    try:
        live_item = fetch_project_item(
            cfg,
            str(source.get("owner") or ""),
            int(source.get("project") or 0),
            int(project_item_id),
            fields=[str(status_field_id)],
        )
        live_status = _project_item_status_name(live_item)
    except Exception:
        pass

    target_key = normalize_status_key(status_name)
    current_key = normalize_status_key(current_status)
    live_key = normalize_status_key(live_status)
    if live_status:
        if live_key == target_key:
            remember_project_item_status(
                cfg,
                owner=str(source.get("owner") or ""),
                project_number=source.get("project") or 0,
                item_id=project_item_id,
                status_name=status_name,
                source="github_mcp.update_project_item_status.live",
            )
            return None
        if current_status and live_key != current_key:
            return github_projects_readiness_message(
                "write",
                "remote divergence detected for Project item "
                f"{project_item_id}: cached status '{current_status}', live status '{live_status}', "
                f"target status '{status_name}'",
                actions=["resync_outward", "ignore_remote_drift", "start_new_run_from_reopened_item"],
            )
        if live_key in GITHUB_PROJECT_TERMINAL_STATUS_KEYS and target_key in GITHUB_PROJECT_ACTIVE_STATUS_KEYS:
            return github_projects_readiness_message(
                "write",
                "remote divergence detected for Project item "
                f"{project_item_id}: cached status '{current_status}', live status '{live_status}', "
                f"target status '{status_name}'",
                actions=["resync_outward", "ignore_remote_drift", "start_new_run_from_reopened_item"],
            )
    if current_key == target_key and not live_status:
        return None
    result = execute_engine_tool(
        cfg,
        "mcp.github.projects_write",
        {
            "method": "update_project_item",
            "owner": source.get("owner"),
            "project_number": int(source.get("project")),
            "item_id": int(project_item_id),
            "updated_field": {"id": int(status_field_id), "value": str(option_id)},
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))
    remember_project_item_status(
        cfg,
        owner=str(source.get("owner") or ""),
        project_number=source.get("project") or 0,
        item_id=project_item_id,
        status_name=status_name,
        source="github_mcp.update_project_item_status",
    )
    return None


def _fetch_issue_comments(cfg: ResolvedConfig, task: dict[str, Any]) -> list[dict[str, Any]]:
    source = dict(task.get("source") or {})
    owner = str(source.get("owner") or "").strip()
    issue_number = source.get("issue_number")
    repo_name = str(source.get("repo_name") or "").strip()
    if (not owner or not repo_name) and source.get("issue_url"):
        parsed = urlparse(str(source.get("issue_url") or ""))
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 4 and parts[2] == "issues":
            owner = owner or parts[0]
            repo_name = repo_name or parts[1]
            issue_number = issue_number or parts[3]
    if not owner or not repo_name or not issue_number:
        return []
    attempts = [
        ("mcp.github.issue_read", {"owner": owner, "repo": repo_name, "issue_number": int(issue_number), "method": "get_comments"}),
        ("mcp.github.issue_read", {"owner": owner, "repo": repo_name, "issueNumber": int(issue_number), "method": "get_comments"}),
        ("mcp.github.list_issue_comments", {"owner": owner, "repo": repo_name, "issue_number": int(issue_number)}),
        ("mcp.github.list_issue_comments", {"owner": owner, "repo": repo_name, "issueNumber": int(issue_number)}),
        ("mcp.github.get_issue_comments", {"owner": owner, "repo": repo_name, "issue_number": int(issue_number)}),
        ("mcp.github.get_issue_comments", {"owner": owner, "repo": repo_name, "issueNumber": int(issue_number)}),
    ]
    for tool, args in attempts:
        try:
            result = execute_engine_tool(cfg, tool, args)
        except RuntimeError:
            continue
        if _tool_failed(result):
            continue
        comments: list[dict[str, Any]] = []
        for payload in _tool_result_payloads(result):
            comments.extend(_flatten_comment_entries(payload))
        if comments:
            return comments
    return []


def _issue_comment_already_posted(cfg: ResolvedConfig, task: dict[str, Any], marker: str, body: str) -> bool:
    if not marker:
        return False
    comments = _fetch_issue_comments(cfg, task)
    normalized_body = body.strip()
    for comment in comments:
        comment_body = _comment_body_text(comment)
        if marker and marker in comment_body:
            return True
        if normalized_body and comment_body == normalized_body:
            return True
    return False


def add_issue_comment(cfg: ResolvedConfig, task: dict[str, Any], body: str) -> str | None:
    source = dict(task.get("source") or {})
    owner = str(source.get("owner") or "").strip()
    issue_number = source.get("issue_number")
    repo_name = str(source.get("repo_name") or "").strip()
    if (not owner or not repo_name) and source.get("issue_url"):
        parsed = urlparse(str(source.get("issue_url") or ""))
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 4 and parts[2] == "issues":
            owner = owner or parts[0]
            repo_name = repo_name or parts[1]
            issue_number = issue_number or parts[3]
    if not owner or not repo_name or not issue_number:
        return "No linked GitHub issue metadata available for comment sync."
    marker = _issue_comment_marker(str(task.get("run_id") or source.get("run_id") or ""))
    if _issue_comment_already_posted(cfg, task, marker, body):
        return None
    result = execute_engine_tool(
        cfg,
        "mcp.github.add_issue_comment",
        {
            "owner": owner,
            "repo": repo_name,
            "issue_number": int(issue_number),
            "body": body,
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))
    return None


def build_issue_comment_body(
    *,
    run_id: str,
    task_title: str,
    outcome: str,
    summary: str,
    diff_snapshot: str | None = None,
    review_returncode: int | None = None,
    test_returncode: int | None = None,
) -> str:
    lines = [
        f"ACA run `{run_id}` finished with status: **{outcome}**.",
        "",
        f"Task: {task_title}",
    ]
    if summary.strip():
        lines.extend(["", summary.strip()])
    if review_returncode is not None or test_returncode is not None:
        lines.extend(
            [
                "",
                "Validation:",
                f"- review: `{review_returncode if review_returncode is not None else 'n/a'}`",
                f"- test: `{test_returncode if test_returncode is not None else 'n/a'}`",
            ]
        )
    if diff_snapshot:
        excerpt = diff_snapshot.strip().splitlines()[:10]
        if excerpt:
            lines.extend(["", "Diff snapshot:", "```text", "\n".join(excerpt), "```"])
    marker = _issue_comment_marker(run_id)
    if marker:
        lines.extend(["", marker])
    return "\n".join(lines).strip()


def fetch_project_item(
    cfg: ResolvedConfig,
    owner: str,
    project_number: int,
    item_id: int,
    *,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    field_args = {"fields": fields} if fields else {}
    result = execute_engine_tool(
        cfg,
        "mcp.github.projects_get",
        {
            "method": "get_project_item",
            "owner": owner,
            "projectNumber": project_number,
            "itemId": item_id,
            **field_args,
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))
    return _parse_json_output(result)


def get_pull_request(cfg: ResolvedConfig, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    attempts = [
        ("mcp.github.pull_request_read", {"owner": owner, "repo": repo, "pullNumber": pr_number, "method": "get"}),
        ("mcp.github.pull_request_read", {"owner": owner, "repo": repo, "pull_number": pr_number, "method": "get"}),
        ("mcp.github.get_pull_request", {"owner": owner, "repo": repo, "pull_number": pr_number}),
        ("mcp.github.get_pull_request", {"owner": owner, "repo": repo, "pullNumber": pr_number}),
    ]
    for tool, args in attempts:
        try:
            result = execute_engine_tool(cfg, tool, args)
        except RuntimeError:
            continue
        if _tool_failed(result):
            continue
        data = _parse_json_output(result)
        if data:
            return data
    data = _get_pull_request_via_github_api(cfg, owner, repo, pr_number)
    if data:
        return data
    data = _get_pull_request_via_gh_cli(owner, repo, pr_number)
    if data:
        return data
    raise RuntimeError(f"Could not read GitHub pull request {owner}/{repo}#{pr_number} through GitHub MCP.")


def _github_token(cfg: ResolvedConfig) -> str:
    candidates = _github_token_candidates(cfg)
    return candidates[0] if candidates else ""


def _github_token_candidates(cfg: ResolvedConfig) -> list[str]:
    candidates: list[str] = []
    for key in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"):
        value = str(cfg.env.get(key) or "").strip()
        if value:
            candidates.append(value)
    token_files = [
        cfg.env.get("GITHUB_PERSONAL_ACCESS_TOKEN_FILE"),
        cfg.env.get("GITHUB_TOKEN_FILE"),
        cfg.env.get("ACA_REPO_TOKEN_FILE"),
        cfg.repository.credential_file,
        "/run/secrets/github_token",
    ]
    for raw_path in token_files:
        path_text = str(raw_path or "").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = cfg.root_dir / path
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            candidates.append(token)
    deduped: list[str] = []
    seen: set[str] = set()
    for token in candidates:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _get_pull_request_via_github_api(
    cfg: ResolvedConfig,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any]:
    tokens = _github_token_candidates(cfg)
    if not tokens:
        return {}
    query = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      url
      headRefName
      baseRefName
      state
      isDraft
      merged
      reviewDecision
      statusCheckRollup {
        contexts(first: 50) {
          nodes {
            __typename
            ... on CheckRun {
              name
              status
              conclusion
              detailsUrl
            }
            ... on StatusContext {
              context
              state
              targetUrl
            }
          }
        }
      }
    }
  }
}
""".strip()
    body = json.dumps({"query": query, "variables": {"owner": owner, "repo": repo, "number": pr_number}}).encode("utf-8")
    for token in tokens:
        request = Request(
            "https://api.github.com/graphql",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                continue
            return {}
        except (OSError, URLError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict) or parsed.get("errors"):
            return {}
        pr = (((parsed.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
        if not isinstance(pr, dict) or not pr:
            return {}
        rollup = pr.get("statusCheckRollup")
        if isinstance(rollup, dict):
            contexts = rollup.get("contexts")
            nodes = contexts.get("nodes") if isinstance(contexts, dict) else rollup.get("nodes")
            pr["statusCheckRollup"] = _normalize_graphql_check_nodes(nodes)
        pr.setdefault("base_repo", f"{owner}/{repo}")
        return pr
    return {}


def _normalize_graphql_check_nodes(nodes: Any) -> list[dict[str, Any]]:
    if not isinstance(nodes, list):
        return []
    checks: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("__typename") == "StatusContext":
            checks.append(
                {
                    "name": node.get("context"),
                    "status": node.get("state"),
                    "conclusion": node.get("state"),
                    "detailsUrl": node.get("targetUrl"),
                }
            )
        else:
            checks.append(dict(node))
    return checks


def _get_pull_request_via_gh_cli(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    repo_slug = f"{owner}/{repo}"
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo_slug,
                "--json",
                "number,url,headRefName,baseRefName,state,isDraft,mergedAt,reviewDecision,statusCheckRollup",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict) or not payload:
        return {}
    payload.setdefault("base_repo", repo_slug)
    return payload


def get_pull_request_files(cfg: ResolvedConfig, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    attempts = [
        ("mcp.github.pull_request_read", {"owner": owner, "repo": repo, "pullNumber": pr_number, "method": "get_files"}),
        ("mcp.github.pull_request_read", {"owner": owner, "repo": repo, "pull_number": pr_number, "method": "get_files"}),
        ("mcp.github.pull_request_read", {"owner": owner, "repo": repo, "pullNumber": pr_number, "method": "files"}),
        ("mcp.github.get_pull_request_files", {"owner": owner, "repo": repo, "pull_number": pr_number}),
        ("mcp.github.list_pull_request_files", {"owner": owner, "repo": repo, "pull_number": pr_number}),
    ]
    entries: list[dict[str, Any]] = []
    for payload in _execute_github_tool_attempts(cfg, attempts):
        entries.extend(_flatten_pull_request_file_entries(payload))
    return entries


def list_pull_requests(cfg: ResolvedConfig, owner: str, repo: str, state: str = "open") -> list[dict[str, Any]]:
    result = execute_engine_tool(
        cfg,
        "mcp.github.list_pull_requests",
        {
            "owner": owner,
            "repo": repo,
            "state": state,
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))
    data = _parse_json_output(result)
    if isinstance(data, list):
        return data
    return []


def _fetch_pull_requests(cfg: ResolvedConfig, owner: str, repo: str) -> list[dict[str, Any]]:
    pulls: list[dict[str, Any]] = []
    for state in ("all", "open"):
        try:
            pulls.extend(list_pull_requests(cfg, owner, repo, state=state))
        except Exception:
            continue
        if pulls:
            break
    return pulls


def _existing_pull_request_url(
    cfg: ResolvedConfig,
    *,
    owner: str,
    repo_name: str,
    head_branch: str,
    marker: str,
) -> str:
    if not owner or not repo_name or not head_branch:
        return ""
    for pr in _fetch_pull_requests(cfg, owner, repo_name):
        if _pull_request_head_ref(pr) != head_branch:
            continue
        url = _pull_request_url(pr)
        if url:
            return url
    return ""


def _existing_pull_request_metadata(
    cfg: ResolvedConfig,
    *,
    owner: str,
    repo_name: str,
    head_branch: str,
    base_repo: str,
    base_branch: str,
    marker: str,
) -> dict[str, Any]:
    if not owner or not repo_name or not head_branch:
        return {}
    for pr in _fetch_pull_requests(cfg, owner, repo_name):
        if _pull_request_head_ref(pr) != head_branch:
            continue
        metadata = normalize_pull_request_metadata(
            pr,
            head_branch=head_branch,
            base_repo=base_repo,
            base_branch=base_branch,
        )
        if metadata.get("url"):
            metadata["reused"] = True
            return metadata
    return {}


def _github_slug_from_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$", text):
        return text.removesuffix(".git")
    ssh_match = re.match(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$", text)
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2).removesuffix('.git')}"
    parsed = urlparse(text)
    if parsed.netloc.lower() != "github.com":
        return ""
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _git_remote_slug(repo_path: Any, remote_name: str, env: dict[str, str]) -> str:
    path_text = str(repo_path or "").strip()
    if not path_text:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path_text, "remote", "get-url", remote_name or "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return _github_slug_from_url(result.stdout.strip())


def _pull_request_repo_slug(cfg: ResolvedConfig, task: dict[str, Any]) -> str:
    source = dict(task.get("source") or {})
    owner = str(source.get("owner") or "").strip()
    repo_name = str(source.get("repo_name") or source.get("repo") or "").strip()
    if owner and repo_name:
        return f"{owner}/{repo_name}"
    repo = dict(task.get("repo") or {})
    for value in (
        repo.get("slug"),
        repo.get("clone_url"),
        task.get("repo_slug"),
        task.get("repo_url"),
        cfg.repository.slug,
        cfg.repository.clone_url,
    ):
        slug = _github_slug_from_url(value)
        if slug:
            return slug
    remote_name = str(repo.get("remote_name") or cfg.repository.remote_name or "origin")
    for repo_path in (repo.get("path"), cfg.repository.path):
        slug = _git_remote_slug(repo_path, remote_name, cfg.env)
        if slug:
            return slug
    return ""


def create_pull_request_metadata(
    cfg: ResolvedConfig,
    task: dict[str, Any],
    head_branch: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    slug = _pull_request_repo_slug(cfg, task)
    if "/" not in slug:
        return {"error": "Missing repository owner/name for PR creation."}
    owner, repo_name = slug.split("/", 1)

    base_branch = cfg.repository.default_branch or "main"
    base_repo = f"{owner}/{repo_name}"
    marker = _pull_request_marker(str(task.get("run_id") or ""), head_branch)
    existing = _existing_pull_request_metadata(
        cfg,
        owner=owner,
        repo_name=repo_name,
        head_branch=head_branch,
        base_repo=base_repo,
        base_branch=base_branch,
        marker=marker,
    )
    if existing:
        return existing

    if marker and marker not in body:
        body = f"{body.rstrip()}\n\n{marker}".strip()

    result = execute_engine_tool(
        cfg,
        "mcp.github.create_pull_request",
        {
            "owner": owner,
            "repo": repo_name,
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))

    data = _parse_json_output(result)
    metadata = normalize_pull_request_metadata(
        data,
        head_branch=head_branch,
        base_repo=base_repo,
        base_branch=base_branch,
    )
    metadata["reused"] = False
    return metadata


def refresh_pull_request_lifecycle(cfg: ResolvedConfig, pull_request: dict[str, Any]) -> dict[str, Any]:
    base_repo = str(pull_request.get("base_repo") or cfg.repository.slug or "").strip()
    number = pull_request.get("number")
    if not base_repo or "/" not in base_repo or number in (None, ""):
        return {**pull_request, "lifecycle_state": "blocked", "terminal": True, "error": "Missing PR base repo or number."}
    owner, repo_name = base_repo.split("/", 1)
    pr = get_pull_request(cfg, owner, repo_name, int(number))
    refreshed = normalize_pull_request_metadata(
        pr,
        head_branch=str(pull_request.get("head_branch") or ""),
        base_repo=base_repo,
        base_branch=str(pull_request.get("base_branch") or ""),
    )
    return refreshed


def _github_actor_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("login", "name", "email"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
    return str(value or "").strip()


def _review_body(review: dict[str, Any]) -> str:
    for key in ("body", "bodyText", "summary", "text"):
        text = str(review.get(key) or "").strip()
        if text:
            return text
    return ""


def _review_comment_body(comment: dict[str, Any]) -> str:
    for key in ("body", "bodyText", "text"):
        text = str(comment.get(key) or "").strip()
        if text:
            return text
    return ""


def _review_comment_is_stale(comment: dict[str, Any]) -> bool:
    for key in ("isResolved", "resolved", "outdated", "isOutdated", "stale", "isStale"):
        value = comment.get(key)
        if isinstance(value, bool) and value:
            return True
    state = normalize_status_key(str(comment.get("state") or comment.get("status") or ""))
    return state in {"resolved", "outdated", "stale", "dismissed"}


def _review_comment_path(comment: dict[str, Any]) -> str:
    for key in ("path", "file", "filePath"):
        text = str(comment.get(key) or "").strip()
        if text:
            return text
    return ""


def _review_comment_line(comment: dict[str, Any]) -> int | None:
    for key in ("line", "original_line", "originalLine", "position"):
        try:
            return int(comment.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _review_comment_url(comment: dict[str, Any]) -> str:
    for key in ("html_url", "url", "web_url"):
        text = str(comment.get(key) or "").strip()
        if text:
            return text
    return ""


def _failed_checks_from_pr(pr: dict[str, Any]) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for check in _pull_request_check_entries(pr):
        conclusion = normalize_status_key(str(check.get("conclusion") or check.get("status") or ""))
        if conclusion not in {"failure", "failed", "error", "cancelled", "canceled", "timed_out"}:
            continue
        failed.append(
            {
                "kind": "check_failure",
                "name": str(check.get("name") or check.get("context") or check.get("title") or "check").strip(),
                "state": conclusion,
                "workflow": str(check.get("workflowName") or check.get("workflow") or "").strip(),
                "summary": str(check.get("summary") or check.get("details") or check.get("output") or "").strip(),
                "url": str(check.get("detailsUrl") or check.get("details_url") or check.get("url") or "").strip(),
            }
        )
    return failed


def _list_pull_request_reviews(cfg: ResolvedConfig, *, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    payloads = _execute_github_tool_attempts(
        cfg,
        [
            ("mcp.github.list_pull_request_reviews", {"owner": owner, "repo": repo, "pull_number": number}),
            ("mcp.github.list_pull_request_reviews", {"owner": owner, "repo": repo, "pullNumber": number}),
            ("mcp.github.get_pull_request_reviews", {"owner": owner, "repo": repo, "pull_number": number}),
            ("mcp.github.get_pull_request_reviews", {"owner": owner, "repo": repo, "pullNumber": number}),
        ],
    )
    reviews: list[dict[str, Any]] = []
    for payload in payloads:
        reviews.extend(_flatten_review_entries(payload))
    return reviews


def _list_pull_request_review_comments(cfg: ResolvedConfig, *, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    payloads = _execute_github_tool_attempts(
        cfg,
        [
            ("mcp.github.list_pull_request_review_comments", {"owner": owner, "repo": repo, "pull_number": number}),
            ("mcp.github.list_pull_request_review_comments", {"owner": owner, "repo": repo, "pullNumber": number}),
            ("mcp.github.get_pull_request_review_comments", {"owner": owner, "repo": repo, "pull_number": number}),
            ("mcp.github.get_pull_request_review_comments", {"owner": owner, "repo": repo, "pullNumber": number}),
        ],
    )
    comments: list[dict[str, Any]] = []
    for payload in payloads:
        comments.extend(_flatten_review_comment_entries(payload))
    return comments


def collect_pull_request_repair_context(
    cfg: ResolvedConfig,
    pull_request: dict[str, Any],
    *,
    limit: int = 12,
) -> dict[str, Any]:
    base_repo = str(pull_request.get("base_repo") or cfg.repository.slug or "").strip()
    number = pull_request.get("number")
    if not base_repo or "/" not in base_repo or number in (None, ""):
        return {
            "actionable": False,
            "reason": "missing_pull_request_identity",
            "feedback_items": [],
            "pull_request": pull_request,
        }
    owner, repo_name = base_repo.split("/", 1)
    pr = get_pull_request(cfg, owner, repo_name, int(number))
    lifecycle = normalize_pull_request_metadata(
        pr,
        head_branch=str(pull_request.get("head_branch") or ""),
        base_repo=base_repo,
        base_branch=str(pull_request.get("base_branch") or ""),
    )
    items: list[dict[str, Any]] = []
    reviews = _list_pull_request_reviews(cfg, owner=owner, repo=repo_name, number=int(number))
    for review in reviews:
        state = normalize_status_key(str(review.get("state") or review.get("status") or ""))
        if state not in {"changes_requested", "requested_changes"}:
            continue
        body = _review_body(review)
        if not body:
            continue
        items.append(
            {
                "kind": "requested_changes",
                "author": _github_actor_name(review.get("user") or review.get("author")),
                "body": body,
                "url": _review_comment_url(review),
            }
        )
    comments = _list_pull_request_review_comments(cfg, owner=owner, repo=repo_name, number=int(number))
    for comment in comments:
        if _review_comment_is_stale(comment):
            continue
        body = _review_comment_body(comment)
        if not body:
            continue
        items.append(
            {
                "kind": "review_comment",
                "author": _github_actor_name(comment.get("user") or comment.get("author")),
                "body": body,
                "path": _review_comment_path(comment),
                "line": _review_comment_line(comment),
                "url": _review_comment_url(comment),
            }
        )
    items.extend(_failed_checks_from_pr(pr))
    bounded = items[: max(1, int(limit))]
    return {
        "actionable": bool(bounded),
        "reason": "" if bounded else "no_actionable_review_feedback",
        "pull_request": lifecycle,
        "feedback_items": bounded,
        "truncated": len(items) > len(bounded),
    }


def build_pull_request_repair_prompt(context: dict[str, Any]) -> str:
    pull_request = dict(context.get("pull_request") or {})
    lines = [
        "Repair the existing pull request branch using the bounded feedback below.",
        "",
        f"Pull request: {pull_request.get('url') or ''}",
        f"Branch: {pull_request.get('head_branch') or ''}",
        f"Lifecycle: {pull_request.get('lifecycle_state') or ''}",
        "",
        "Feedback:",
    ]
    for index, item in enumerate(context.get("feedback_items") or [], start=1):
        if not isinstance(item, dict):
            continue
        location = ""
        if item.get("path"):
            location = str(item.get("path") or "")
            if item.get("line") is not None:
                location = f"{location}:{item.get('line')}"
        prefix = f"{index}. {item.get('kind') or 'feedback'}"
        if location:
            prefix = f"{prefix} ({location})"
        lines.append(prefix)
        if item.get("name"):
            lines.append(f"Check: {item.get('name')}")
        if item.get("workflow"):
            lines.append(f"Workflow: {item.get('workflow')}")
        if item.get("state"):
            lines.append(f"State: {item.get('state')}")
        if item.get("author"):
            lines.append(f"Author: {item.get('author')}")
        body = str(item.get("body") or item.get("summary") or "").strip()
        if body:
            lines.append(body[:1200])
        if item.get("url"):
            lines.append(f"URL: {item.get('url')}")
        lines.append("")
    if context.get("truncated"):
        lines.append("Additional feedback existed but was truncated for bounded repair context.")
    return "\n".join(lines).strip()


def _allowed_merge_strategies(cfg: ResolvedConfig) -> set[str]:
    return {
        strategy.strip().lower()
        for strategy in str(cfg.review.auto_merge_allowed_strategies or "").split(",")
        if strategy.strip()
    }


def _approval_granted(approvals: dict[str, Any] | None, key: str) -> bool:
    if not isinstance(approvals, dict):
        return False
    value = approvals.get(key)
    if isinstance(value, dict):
        value = value.get("approved") or value.get("status")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"approved", "approve", "granted", "true", "yes", "1"}


def evaluate_auto_merge_gates(
    cfg: ResolvedConfig,
    pull_request: dict[str, Any],
    approvals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy = str(cfg.review.auto_merge_strategy or "squash").strip().lower()
    allowed = _allowed_merge_strategies(cfg)
    base_repo = str(pull_request.get("base_repo") or "").strip()
    head_branch = str(pull_request.get("head_branch") or "").strip()
    denials: list[str] = []
    pending_approvals: list[dict[str, Any]] = []
    if str(cfg.review.policy or "").strip().lower() != "auto_merge":
        denials.append("review.policy is not auto_merge")
    if strategy not in {"merge", "squash", "rebase"}:
        denials.append(f"merge strategy `{strategy}` is not supported")
    if strategy not in allowed:
        denials.append(f"merge strategy `{strategy}` is not allowed by policy")
    if not head_branch.startswith("aca/"):
        denials.append("head branch is not ACA-created")
    expected_repo = str(cfg.repository.slug or "").strip()
    if expected_repo and base_repo and base_repo != expected_repo:
        denials.append(f"PR base repo `{base_repo}` does not match configured repo `{expected_repo}`")
    if str(pull_request.get("lifecycle_state") or "").strip() != "ready-to-merge":
        denials.append("PR lifecycle state is not ready-to-merge")
    if str(pull_request.get("checks_state") or "").strip() != "success":
        denials.append("PR checks are not proven clean")
    if str(pull_request.get("review_state") or "").strip() != "approved":
        denials.append("PR review state is not approved")
    if pull_request.get("terminal") or pull_request.get("merged"):
        denials.append("PR is already terminal")
    if pull_request.get("number") in (None, ""):
        denials.append("PR number is missing")
    if not base_repo or "/" not in base_repo:
        denials.append("PR base repo is missing")
    if not denials and bool(cfg.review.merge_requires_approval) and not _approval_granted(approvals, "merge"):
        pending_approvals.append(
            {
                "action": "merge_pull_request",
                "key": "merge",
                "reason": "Policy requires explicit approval before ACA merges the pull request.",
            }
        )
    if (
        not denials
        and bool(cfg.review.delete_branch_after_merge)
        and bool(cfg.review.branch_delete_requires_approval)
        and not _approval_granted(approvals, "branch_delete")
    ):
        pending_approvals.append(
            {
                "action": "delete_remote_branch",
                "key": "branch_delete",
                "reason": "Policy requires separate approval before ACA deletes the remote branch.",
            }
        )
    return {
        "allowed": not denials
        and not any(item.get("key") == "merge" for item in pending_approvals),
        "denials": denials,
        "pending_approvals": pending_approvals,
        "strategy": strategy,
        "head_branch": head_branch,
        "base_repo": base_repo,
        "pull_request": pull_request,
    }


def merge_pull_request(cfg: ResolvedConfig, pull_request: dict[str, Any], *, strategy: str) -> dict[str, Any]:
    base_repo = str(pull_request.get("base_repo") or cfg.repository.slug or "").strip()
    number = int(pull_request.get("number"))
    owner, repo_name = base_repo.split("/", 1)
    result = execute_engine_tool(
        cfg,
        "mcp.github.merge_pull_request",
        {
            "owner": owner,
            "repo": repo_name,
            "pullNumber": number,
            "merge_method": strategy,
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))
    data = _parse_json_output(result)
    return data if isinstance(data, dict) else {"merged": True}


def delete_remote_branch(cfg: ResolvedConfig, pull_request: dict[str, Any]) -> dict[str, Any]:
    base_repo = str(pull_request.get("base_repo") or cfg.repository.slug or "").strip()
    head_branch = str(pull_request.get("head_branch") or "").strip()
    if not base_repo or "/" not in base_repo or not head_branch:
        return {"deleted": False, "error": "Missing base repo or head branch."}
    owner, repo_name = base_repo.split("/", 1)
    attempts = [
        ("mcp.github.delete_branch", {"owner": owner, "repo": repo_name, "branch": head_branch}),
        ("mcp.github.delete_ref", {"owner": owner, "repo": repo_name, "ref": f"heads/{head_branch}"}),
    ]
    for tool, args in attempts:
        try:
            result = execute_engine_tool(cfg, tool, args)
        except RuntimeError:
            continue
        if _tool_failed(result):
            continue
        data = _parse_json_output(result)
        return data if isinstance(data, dict) else {"deleted": True}
    return {"deleted": False, "error": "Could not delete remote branch through GitHub MCP."}


def guarded_auto_merge(
    cfg: ResolvedConfig,
    pull_request: dict[str, Any],
    approvals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gates = evaluate_auto_merge_gates(cfg, pull_request, approvals=approvals)
    if gates["denials"]:
        return {
            "status": "denied",
            "merged": False,
            "branch_deleted": False,
            "denials": gates["denials"],
            "pending_approvals": [],
            "strategy": gates["strategy"],
            "pull_request": pull_request,
        }
    merge_pending = [item for item in gates["pending_approvals"] if item.get("key") == "merge"]
    if merge_pending:
        return {
            "status": "pending_approval",
            "merged": False,
            "branch_deleted": False,
            "denials": [],
            "pending_approvals": merge_pending,
            "strategy": gates["strategy"],
            "pull_request": pull_request,
        }
    merge_result = merge_pull_request(cfg, pull_request, strategy=str(gates["strategy"]))
    delete_pending = [item for item in gates["pending_approvals"] if item.get("key") == "branch_delete"]
    if delete_pending:
        delete_result = {
            "deleted": False,
            "skipped": True,
            "pending_approvals": delete_pending,
            "reason": "branch_delete_requires_approval is enabled and approval was not granted",
        }
    elif bool(cfg.review.delete_branch_after_merge):
        delete_result = delete_remote_branch(cfg, pull_request)
    else:
        delete_result = {"deleted": False, "skipped": True, "reason": "delete_branch_after_merge is disabled"}
    return {
        "status": "merged",
        "merged": True,
        "branch_deleted": bool(delete_result.get("deleted")),
        "strategy": gates["strategy"],
        "pending_approvals": delete_pending,
        "pull_request": pull_request,
        "merge_result": merge_result,
        "delete_result": delete_result,
    }


def create_pull_request(
    cfg: ResolvedConfig,
    task: dict[str, Any],
    head_branch: str,
    title: str,
    body: str,
) -> str | None:
    metadata = create_pull_request_metadata(
        cfg,
        task,
        head_branch=head_branch,
        title=title,
        body=body,
    )
    return str(metadata.get("url") or metadata.get("error") or "created")
