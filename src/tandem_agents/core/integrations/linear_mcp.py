from __future__ import annotations

import json
import re
import time
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import (
    connect_mcp_server as _connect_mcp_server,
    disconnect_mcp_server as _disconnect_mcp_server,
    execute_engine_tool,
    list_engine_tool_ids,
    list_mcp_servers as _list_mcp_servers,
    set_mcp_enabled as _set_engine_mcp_enabled,
)


LINEAR_ACTIONABLE_STATUS_KEYS = {"backlog", "todo", "to_do", "ready", "unstarted", "triage"}
LINEAR_DONE_STATUS_KEYS = {"done", "complete", "completed", "closed", "canceled", "cancelled"}
LINEAR_ACTIVE_STATUS_KEYS = {"in_progress", "started", "working", "review", "in_review"}


def normalize_linear_key(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def linear_mcp_server_name(cfg: ResolvedConfig) -> str:
    return str(cfg.linear_mcp.server or "linear").strip() or "linear"


def linear_mcp_scope(cfg: ResolvedConfig, source_type: str) -> str:
    if not cfg.linear_mcp.enabled:
        return "none"
    scope = str(cfg.linear_mcp.scope or "none").strip().lower()
    if source_type != "linear" and scope != "always":
        return "none"
    return scope


def linear_remote_sync_mode(cfg: ResolvedConfig, source_type: str) -> str:
    if linear_mcp_scope(cfg, source_type) == "none":
        return "off"
    if source_type != "linear":
        return "off"
    return str(cfg.linear_mcp.remote_sync or "off").strip().lower()


def linear_status_name_for_task_state(cfg: ResolvedConfig, task_state: str | None) -> str:
    key = normalize_linear_key(task_state)
    if key in {"claimed", "active", "running", "in_progress", "planning", "worker_execution", "coder_execution"}:
        return cfg.linear_mcp.claim_status or "In Progress"
    if key in {"review", "testing"}:
        return cfg.linear_mcp.review_status or cfg.linear_mcp.claim_status or "In Review"
    if key in {"blocked", "stale", "failed", "cancelled", "canceled"}:
        return cfg.linear_mcp.blocked_status or "Blocked"
    if key in {"done", "completed"}:
        return cfg.linear_mcp.done_status or "Done"
    return cfg.linear_mcp.claim_status or "In Progress"


def linear_status_name_for_outcome(cfg: ResolvedConfig, outcome: str | None) -> str:
    key = normalize_linear_key(outcome)
    if key == "completed":
        return cfg.linear_mcp.review_status or cfg.linear_mcp.done_status or "In Review"
    if key in {"blocked", "failed", "cancelled", "canceled"}:
        return cfg.linear_mcp.blocked_status or "Blocked"
    return linear_status_name_for_task_state(cfg, outcome)


def linear_status_key_is_actionable(status_name: str | None, state_type: str | None = None) -> bool:
    key = normalize_linear_key(status_name)
    type_key = normalize_linear_key(state_type)
    if type_key:
        if type_key in {"backlog", "unstarted", "triage"}:
            return True
        if type_key in {"started", "completed", "canceled", "cancelled"}:
            return False
    if key in LINEAR_ACTIONABLE_STATUS_KEYS:
        return True
    if key in LINEAR_DONE_STATUS_KEYS or key in LINEAR_ACTIVE_STATUS_KEYS or key == "blocked":
        return False
    return False


def list_mcp_servers(cfg: ResolvedConfig) -> dict[str, Any]:
    payload = _list_mcp_servers(cfg)
    return payload if isinstance(payload, dict) else {}


def get_mcp_server(cfg: ResolvedConfig, name: str | None = None) -> dict[str, Any] | None:
    server_name = str(name or linear_mcp_server_name(cfg)).strip()
    payload = list_mcp_servers(cfg)
    server = payload.get(server_name)
    return server if isinstance(server, dict) else None


def ensure_linear_mcp_connected(cfg: ResolvedConfig) -> dict[str, Any]:
    server_name = linear_mcp_server_name(cfg)
    server = get_mcp_server(cfg, server_name)
    if server is None:
        raise RuntimeError(
            f"Linear MCP server '{server_name}' is not configured in the connected Tandem engine. "
            "Connect Linear in the Tandem control panel first."
        )
    if not server.get("enabled"):
        _set_engine_mcp_enabled(cfg, server_name, True)
        server = get_mcp_server(cfg, server_name) or server
    if not server.get("connected"):
        _connect_mcp_server(cfg, server_name)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        server = get_mcp_server(cfg, server_name) or server
        if not server.get("connected"):
            time.sleep(0.25)
            continue
        if any(tool_id.startswith(f"mcp.{server_name}.") for tool_id in list_engine_tool_ids(cfg)):
            return server
        time.sleep(0.25)
    last_error = str(server.get("last_error") or "").strip()
    pending_auth = server.get("last_auth_challenge") or server.get("pending_auth_by_tool")
    if not server.get("connected"):
        detail = last_error or "authorization is required"
        raise RuntimeError(f"Linear MCP server '{server_name}' is not connected: {detail}")
    if pending_auth:
        raise RuntimeError(f"Linear MCP server '{server_name}' is awaiting authorization.")
    raise RuntimeError(f"Linear MCP server '{server_name}' is connected but exposed no tools.")
    return server


def ensure_linear_mcp_disconnected(cfg: ResolvedConfig) -> dict[str, Any] | None:
    server_name = linear_mcp_server_name(cfg)
    server = get_mcp_server(cfg, server_name)
    if server is None:
        return None
    if server.get("connected"):
        _disconnect_mcp_server(cfg, server_name)
        server = get_mcp_server(cfg, server_name) or server
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
    return "unknown Linear MCP error"


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
    for value in list(values):
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, list):
                for entry in content:
                    if not isinstance(entry, dict):
                        continue
                    text = entry.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    try:
                        values.append(json.loads(text))
                    except Exception:
                        pass
    unique_values: list[Any] = []
    seen: set[str] = set()
    for value in values:
        try:
            key = json.dumps(value, sort_keys=True, default=str)
        except Exception:
            key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        unique_values.append(value)
    return unique_values


def _tool_payload_page(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, ""
    has_next = bool(payload.get("hasNextPage") or payload.get("has_next_page"))
    cursor = str(payload.get("cursor") or payload.get("nextCursor") or payload.get("next_cursor") or "").strip()
    page_info = payload.get("pageInfo") or payload.get("page_info")
    if isinstance(page_info, dict):
        has_next = has_next or bool(page_info.get("hasNextPage") or page_info.get("has_next_page"))
        cursor = cursor or str(page_info.get("endCursor") or page_info.get("end_cursor") or "").strip()
    return has_next, cursor


def _alias_variants(alias: str) -> list[str]:
    alias = str(alias or "").strip()
    if not alias:
        return []
    variants = [alias]
    if alias.startswith("_"):
        variants.append(alias[1:])
    else:
        variants.append(f"_{alias}")
    camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), alias.lstrip("_"))
    if camel and camel not in variants:
        variants.append(camel)
    return list(dict.fromkeys(variants))


def _resolve_linear_tool_id(cfg: ResolvedConfig, aliases: list[str]) -> str:
    server_name = linear_mcp_server_name(cfg)
    ids = list_engine_tool_ids(cfg)
    candidates: list[str] = []
    for alias in aliases:
        for variant in _alias_variants(alias):
            if variant.startswith("_"):
                continue
            candidates.append(f"mcp.{server_name}.{variant}")
    candidates = list(dict.fromkeys(candidates))
    for candidate in candidates:
        if candidate in ids:
            return candidate
    for candidate in candidates:
        suffix = candidate.rsplit(".", 1)[-1]
        for tool_id in ids:
            if tool_id.startswith(f"mcp.{server_name}.") and tool_id.rsplit(".", 1)[-1] == suffix:
                return tool_id
    if not candidates:
        raise RuntimeError("No Linear MCP tool aliases provided.")
    server = get_mcp_server(cfg, server_name) or {}
    last_error = str(server.get("last_error") or "").strip()
    if not server.get("connected"):
        detail = last_error or "authorization is required"
        raise RuntimeError(f"Linear MCP server '{server_name}' is not connected: {detail}")
    raise RuntimeError(
        f"Linear MCP server '{server_name}' did not expose a tool matching any of: {', '.join(candidates)}"
    )


def _execute_linear_tool(cfg: ResolvedConfig, aliases: list[str], args: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    tried: set[str] = set()
    for alias in aliases:
        for variant in _alias_variants(alias):
            try:
                tool_id = _resolve_linear_tool_id(cfg, [variant])
            except RuntimeError as exc:
                last_error = exc
                continue
            if tool_id in tried:
                continue
            tried.add(tool_id)
            try:
                result = execute_engine_tool(cfg, tool_id, args)
            except RuntimeError as exc:
                last_error = exc
                continue
            if _tool_failed(result):
                last_error = RuntimeError(_tool_error_message(result))
                continue
            return result
    if last_error is not None:
        raise last_error
    raise RuntimeError("Could not execute a Linear MCP tool.")


def _flatten_entries(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    entries: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
            elif isinstance(value, dict):
                if isinstance(value.get("nodes"), list):
                    entries.extend(entry for entry in value["nodes"] if isinstance(entry, dict))
                elif isinstance(value.get("items"), list):
                    entries.extend(entry for entry in value["items"] if isinstance(entry, dict))
        if not entries and any(key in payload for key in ("id", "identifier", "title", "name")):
            entries.append(payload)
    return entries


def flatten_issue_entries(payload: Any) -> list[dict[str, Any]]:
    return _flatten_entries(payload, ("issues", "items", "nodes", "data", "results"))


def flatten_status_entries(payload: Any) -> list[dict[str, Any]]:
    return _flatten_entries(payload, ("statuses", "states", "workflowStates", "items", "nodes", "data"))


def flatten_label_entries(payload: Any) -> list[dict[str, Any]]:
    return _flatten_entries(payload, ("labels", "issueLabels", "items", "nodes", "data"))


def flatten_team_entries(payload: Any) -> list[dict[str, Any]]:
    return _flatten_entries(payload, ("teams", "items", "nodes", "data", "results"))


def flatten_project_entries(payload: Any) -> list[dict[str, Any]]:
    return _flatten_entries(payload, ("projects", "items", "nodes", "data", "results"))


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _dedupe_linear_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        identity = str(entry.get("id") or entry.get("identifier") or entry.get("url") or "").strip()
        if not identity:
            try:
                identity = json.dumps(entry, sort_keys=True, default=str)
            except Exception:
                identity = repr(entry)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(entry)
    return deduped


def _linear_issue_attempts(
    *,
    team: str,
    project: str = "",
    statuses: str = "",
    labels: str = "",
    query: str = "",
    limit: int = 50,
    cursor: str = "",
) -> list[dict[str, Any]]:
    base_args: dict[str, Any] = {"limit": min(250, max(1, int(limit)))}
    if cursor:
        base_args["cursor"] = cursor
    if team:
        base_args["team"] = team
    if project:
        base_args["project"] = project
    if query:
        base_args["query"] = query
    status_values = _split_csv(statuses)
    label_values = _split_csv(labels)
    attempts: list[dict[str, Any]] = []
    if statuses or labels:
        combined = dict(base_args)
        if statuses:
            combined["state"] = statuses
        if labels:
            combined["label"] = labels
        attempts.append(combined)
    if statuses and labels:
        attempts.extend(
            [
                {**base_args, "state": statuses, "label": labels},
                {**base_args, "status": statuses, "label": labels},
                {**base_args, "statuses": status_values, "labels": label_values},
            ]
        )
    if statuses:
        attempts.append({**base_args, "state": statuses})
        attempts.append({**base_args, "status": statuses})
        attempts.append({**base_args, "statuses": status_values})
        for status in status_values:
            if labels:
                attempts.append({**base_args, "state": status, "label": labels})
                attempts.append({**base_args, "status": status, "label": labels})
            attempts.append({**base_args, "state": status})
            attempts.append({**base_args, "status": status})
    if labels:
        attempts.append({**base_args, "label": labels})
        attempts.append({**base_args, "labels": label_values})
        for label in label_values:
            attempts.append({**base_args, "label": label})
    attempts.append(dict(base_args))

    deduped_attempts: list[dict[str, Any]] = []
    seen_attempts: set[str] = set()
    for args in attempts:
        key = json.dumps(args, sort_keys=True, default=str)
        if key in seen_attempts:
            continue
        seen_attempts.add(key)
        deduped_attempts.append(args)
    return deduped_attempts


def linear_list_issues(
    cfg: ResolvedConfig,
    *,
    team: str,
    project: str = "",
    statuses: str = "",
    labels: str = "",
    query: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    attempts = _linear_issue_attempts(
        team=team,
        project=project,
        statuses=statuses,
        labels=labels,
        query=query,
        limit=limit,
    )
    for index, args in enumerate(attempts):
        try:
            result = _execute_linear_tool(cfg, ["list_issues", "issues"], args)
        except Exception as exc:
            last_error = exc
            continue
        issues: list[dict[str, Any]] = []
        for payload in _tool_result_payloads(result):
            issues.extend(flatten_issue_entries(payload))
        issues = _dedupe_linear_entries(issues)
        if issues or index == len(attempts) - 1:
            return issues
    if last_error is not None:
        raise last_error
    return []


def linear_count_issues(
    cfg: ResolvedConfig,
    *,
    team: str,
    project: str = "",
    statuses: str = "",
    labels: str = "",
    query: str = "",
    max_pages: int = 20,
) -> int:
    total = 0
    cursor = ""
    seen_issue_ids: set[str] = set()
    for _ in range(max(1, int(max_pages))):
        last_error: Exception | None = None
        page_handled = False
        for args in _linear_issue_attempts(
            team=team,
            project=project,
            statuses=statuses,
            labels=labels,
            query=query,
            limit=250,
            cursor=cursor,
        ):
            try:
                result = _execute_linear_tool(cfg, ["list_issues", "issues"], args)
            except Exception as exc:
                last_error = exc
                continue
            issues: list[dict[str, Any]] = []
            has_next = False
            next_cursor = ""
            for payload in _tool_result_payloads(result):
                issues.extend(flatten_issue_entries(payload))
                payload_has_next, payload_cursor = _tool_payload_page(payload)
                has_next = has_next or payload_has_next
                next_cursor = next_cursor or payload_cursor
            for issue in _dedupe_linear_entries(issues):
                issue_id = str(issue.get("id") or issue.get("identifier") or issue.get("url") or "").strip()
                if not issue_id:
                    issue_id = json.dumps(issue, sort_keys=True, default=str)
                if issue_id in seen_issue_ids:
                    continue
                seen_issue_ids.add(issue_id)
                total += 1
            page_handled = True
            cursor = next_cursor
            if not has_next or not cursor:
                return total
            break
        if not page_handled:
            if last_error is not None:
                raise last_error
            return total
    return total


def linear_list_teams(cfg: ResolvedConfig, *, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"limit": max(1, int(limit))}
    if query:
        args["query"] = query
    result = _execute_linear_tool(cfg, ["list_teams", "teams"], args)
    teams: list[dict[str, Any]] = []
    for payload in _tool_result_payloads(result):
        teams.extend(flatten_team_entries(payload))
    return teams


def linear_list_projects(
    cfg: ResolvedConfig,
    *,
    team: str = "",
    query: str = "",
    include_archived: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"limit": min(50, max(1, int(limit))), "includeArchived": include_archived}
    if team:
        args["team"] = team
    if query:
        args["query"] = query
    result = _execute_linear_tool(cfg, ["list_projects", "projects"], args)
    projects: list[dict[str, Any]] = []
    for payload in _tool_result_payloads(result):
        projects.extend(flatten_project_entries(payload))
    return projects


def linear_fetch_issue(cfg: ResolvedConfig, identifier: str) -> dict[str, Any]:
    selector = str(identifier or "").strip()
    if not selector:
        return {}
    attempts = (
        {"id": selector},
        {"issue_id": selector},
        {"issueId": selector},
        {"identifier": selector},
    )
    for args in attempts:
        try:
            result = _execute_linear_tool(cfg, ["get_issue", "fetch", "issue"], args)
        except Exception:
            continue
        fallback: dict[str, Any] | None = None
        for payload in _tool_result_payloads(result):
            entries = flatten_issue_entries(payload)
            if entries:
                return entries[0]
            if fallback is None and isinstance(payload, dict):
                fallback = payload
        if fallback is not None:
            return fallback
    return {}


def linear_list_issue_statuses(cfg: ResolvedConfig, *, team: str) -> list[dict[str, Any]]:
    args = {"team": team} if team else {}
    result = _execute_linear_tool(cfg, ["list_issue_statuses", "list_statuses", "workflow_states"], args)
    statuses: list[dict[str, Any]] = []
    for payload in _tool_result_payloads(result):
        statuses.extend(flatten_status_entries(payload))
    return statuses


def linear_list_issue_labels(cfg: ResolvedConfig, *, team: str) -> list[dict[str, Any]]:
    args = {"team": team} if team else {}
    result = _execute_linear_tool(cfg, ["list_issue_labels", "list_labels"], args)
    labels: list[dict[str, Any]] = []
    for payload in _tool_result_payloads(result):
        labels.extend(flatten_label_entries(payload))
    return labels


def linear_update_issue(cfg: ResolvedConfig, task: dict[str, Any], fields: dict[str, Any]) -> str | None:
    source = dict(task.get("source") or {})
    issue_id = str(source.get("issue_id") or source.get("id") or source.get("identifier") or source.get("item") or "").strip()
    if not issue_id:
        return "No linked Linear issue metadata available for update sync."
    clean_fields = {key: value for key, value in fields.items() if value not in (None, "", [], {})}
    if not clean_fields:
        return None
    status_value = (
        clean_fields.get("status")
        or clean_fields.get("state")
        or clean_fields.get("state_name")
        or clean_fields.get("stateName")
    )
    labels_value = clean_fields.get("labels") or clean_fields.get("label_names") or clean_fields.get("labelNames")
    field_variants: list[dict[str, Any]] = []
    if status_value:
        field_variants.append({"state": status_value})
    if status_value and labels_value:
        field_variants.extend(
            [
                {"state": status_value, "labels": labels_value},
                {"status": status_value, "labels": labels_value},
                {"stateName": status_value, "labelNames": labels_value},
            ]
        )
    if status_value:
        field_variants.extend(
            [
                {"state": status_value},
                {"status": status_value},
                {"state_name": status_value},
                {"stateName": status_value},
            ]
        )
    if labels_value:
        field_variants.extend(
            [
                {"labels": labels_value},
                {"label_names": labels_value},
                {"labelNames": labels_value},
            ]
        )
    field_variants.append(clean_fields)
    deduped_variants: list[dict[str, Any]] = []
    seen_variants: set[str] = set()
    for variant in field_variants:
        key = json.dumps(variant, sort_keys=True, default=str)
        if key in seen_variants:
            continue
        seen_variants.add(key)
        deduped_variants.append(variant)
    attempts: list[dict[str, Any]] = []
    for variant in deduped_variants:
        attempts.extend(
            [
                {"id": issue_id, **variant},
                {"issue_id": issue_id, **variant},
                {"issueId": issue_id, **variant},
                {"identifier": issue_id, **variant},
            ]
        )
    last_error: Exception | None = None
    for args in attempts:
        try:
            _execute_linear_tool(
                cfg,
                ["update_issue", "updateIssue", "save_issue", "saveIssue"],
                args,
            )
            return None
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "Linear issue update failed.")


def linear_add_comment(cfg: ResolvedConfig, task: dict[str, Any], body: str) -> str | None:
    source = dict(task.get("source") or {})
    issue_id = str(source.get("issue_id") or source.get("id") or source.get("identifier") or source.get("item") or "").strip()
    if not issue_id:
        return "No linked Linear issue metadata available for comment sync."
    body_text = str(body or "").strip()
    if not body_text:
        return None
    attempts = (
        {"issueId": issue_id, "body": body_text},
        {"issueId": issue_id, "comment": body_text},
        {"issue_id": issue_id, "body": body_text},
        {"id": issue_id, "body": body_text},
        {"issue_id": issue_id, "comment": body_text},
    )
    last_error: Exception | None = None
    for args in attempts:
        try:
            _execute_linear_tool(cfg, ["create_comment", "save_comment", "add_comment"], args)
            return None
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "Linear comment creation failed.")


def linear_list_comments(cfg: ResolvedConfig, task: dict[str, Any]) -> list[dict[str, Any]]:
    source = dict(task.get("source") or {})
    issue_id = str(source.get("issue_id") or source.get("id") or source.get("identifier") or source.get("item") or "").strip()
    if not issue_id:
        return []
    attempts = (
        {"issueId": issue_id},
        {"issue_id": issue_id},
        {"id": issue_id},
    )
    last_error: Exception | None = None
    for args in attempts:
        try:
            result = _execute_linear_tool(cfg, ["list_comments"], args)
        except Exception as exc:
            last_error = exc
            continue
        comments: list[dict[str, Any]] = []
        for payload in _tool_result_payloads(result):
            comments.extend(_flatten_entries(payload, ("comments", "items", "nodes", "data", "results")))
        if comments:
            return comments
        return []
    raise RuntimeError(str(last_error) if last_error else "Linear comment listing failed.")


def linear_comment_marker_present(cfg: ResolvedConfig, task: dict[str, Any], marker: str) -> bool:
    marker_text = str(marker or "").strip()
    if not marker_text:
        return False
    for comment in linear_list_comments(cfg, task):
        body = str(comment.get("body") or comment.get("text") or comment.get("content") or "")
        if marker_text in body:
            return True
    return False


def build_linear_comment_body(
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
    marker = str(run_id or "").strip()
    if marker:
        lines.extend(["", f"<!-- aca:linear-comment:{marker} -->"])
    return "\n".join(lines).strip()
