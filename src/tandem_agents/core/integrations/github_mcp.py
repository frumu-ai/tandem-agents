from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    return output.startswith("failed") or output.startswith("unknown method") or output.startswith("missing required")


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


def _parse_json_output(result: dict[str, Any]) -> dict[str, Any]:
    output = result.get("output")
    if isinstance(output, str) and output.strip():
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
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
                    if isinstance(parsed, dict):
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
        return f"Missing GitHub Project status metadata for target status '{status_name}'."
    current_status = str(
        cached_project_item_status(
            cfg,
            owner=str(source.get("owner") or ""),
            project_number=source.get("project") or 0,
            item_id=project_item_id,
        )
    ).strip()
    if normalize_status_key(current_status) != normalize_status_key(status_name):
        try:
            live_item = fetch_project_item(
                cfg,
                str(source.get("owner") or ""),
                int(source.get("project") or 0),
                int(project_item_id),
                fields=[str(status_field_id)],
            )
            live_status = _project_item_status_name(live_item)
            if normalize_status_key(live_status) == normalize_status_key(status_name):
                remember_project_item_status(
                    cfg,
                    owner=str(source.get("owner") or ""),
                    project_number=source.get("project") or 0,
                    item_id=project_item_id,
                    status_name=status_name,
                    source="github_mcp.update_project_item_status.live",
                )
                return None
        except Exception:
            pass
    else:
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
    result = execute_engine_tool(
        cfg,
        "mcp.github.get_pull_request",
        {
            "owner": owner,
            "repo": repo,
            "pull_number": pr_number,
        },
    )
    if _tool_failed(result):
        raise RuntimeError(_tool_error_message(result))
    return _parse_json_output(result)


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


def create_pull_request(
    cfg: ResolvedConfig,
    task: dict[str, Any],
    head_branch: str,
    title: str,
    body: str,
) -> str | None:
    source = dict(task.get("source") or {})
    owner = str(source.get("owner") or "").strip()
    repo_name = str(source.get("repo_name") or "").strip()
    
    if not owner or not repo_name:
        slug = cfg.repository.slug
        if slug and "/" in slug:
            owner, repo_name = slug.split("/", 1)
            
    if not owner or not repo_name:
        return "Missing repository owner/name for PR creation."

    marker = _pull_request_marker(str(task.get("run_id") or ""), head_branch)
    existing_url = _existing_pull_request_url(
        cfg,
        owner=owner,
        repo_name=repo_name,
        head_branch=head_branch,
        marker=marker,
    )
    if existing_url:
        return existing_url
        
    base_branch = cfg.repository.default_branch or "main"
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
    return str(data.get("html_url") or data.get("url") or "created")
