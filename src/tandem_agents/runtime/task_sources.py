from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("aca.task_sources")

from src.tandem_agents.core.repository.board import card_to_task, default_board, ensure_board_template, task_to_card
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import execute_engine_tool, refresh_mcp_server
from src.tandem_agents.core.task_contract import (
    apply_task_contract,
    dependency_status_for_task,
    task_contract_completeness,
)
from src.tandem_agents.core.integrations.github_mcp import (
    cached_project_item_status,
    ensure_github_mcp_connected,
    fetch_project_item,
    github_project_status_key_is_actionable,
    normalize_status_key,
    remember_project_item_status,
)


def _tool_unknown(result: dict[str, Any]) -> bool:
    output = str(result.get("output") or "").strip().lower()
    return output.startswith("unknown tool:")


def _try_engine_tool(cfg: ResolvedConfig, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = execute_engine_tool(cfg, tool, args)
    except RuntimeError:
        return None
    if _tool_unknown(result):
        return None
    return result


def _github_project_schema(cfg: ResolvedConfig, owner: str, project: int) -> dict[str, Any]:
    attempts = [
        ("mcp.github.get_project", {"owner": owner, "project_number": project}),
        ("mcp.github.get_project", {"owner": owner, "projectNumber": project}),
        ("mcp.github.projects_get", {"method": "get_project", "owner": owner, "project_number": project}),
        ("mcp.github.projects_get", {"method": "get_project", "owner": owner, "projectNumber": project}),
        ("mcp.github.projects_list", {"method": "list_project_fields", "owner": owner, "project_number": project}),
        ("mcp.github.projects_list", {"method": "list_project_fields", "owner": owner, "projectNumber": project}),
    ]
    for tool, args in attempts:
        result = _try_engine_tool(cfg, tool, args)
        if result is None:
            continue
        try:
            return _extract_project_schema(result)
        except RuntimeError:
            continue
    raise RuntimeError("Could not extract GitHub project schema from MCP result.")


def _github_project_items(cfg: ResolvedConfig, owner: str, project: int) -> dict[str, Any]:
    attempts = [
        ("mcp.github.list_project_items", {"owner": owner, "project_number": project}),
        ("mcp.github.list_project_items", {"owner": owner, "projectNumber": project}),
        ("mcp.github.projects_list", {"method": "list_project_items", "owner": owner, "project_number": project}),
        ("mcp.github.projects_list", {"method": "list_project_items", "owner": owner, "projectNumber": project}),
    ]
    for tool, args in attempts:
        result = _try_engine_tool(cfg, tool, args)
        if result is None:
            continue
        return result
    raise RuntimeError("Could not read GitHub project items from the connected GitHub MCP server.")


def _github_project_board_cache_path(cfg: ResolvedConfig) -> Path:
    return cfg.output_root() / "state" / "github_project_boards.json"


def _github_project_board_cache_key(owner: str, project: int | str) -> str:
    return f"{str(owner).strip().lower()}:{int(project)}"


def _load_board_cache(cfg: ResolvedConfig) -> dict[str, Any]:
    path = _github_project_board_cache_path(cfg)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _save_board_cache(cfg: ResolvedConfig, cache: dict[str, Any]) -> None:
    path = _github_project_board_cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _read_cached_board_snapshot(cfg: ResolvedConfig, owner: str, project: int) -> dict[str, Any] | None:
    cache = _load_board_cache(cfg)
    record = cache.get(_github_project_board_cache_key(owner, project))
    return record if isinstance(record, dict) else None


def _write_cached_board_snapshot(cfg: ResolvedConfig, owner: str, project: int, snapshot: dict[str, Any]) -> None:
    cache = _load_board_cache(cfg)
    cache[_github_project_board_cache_key(owner, project)] = snapshot
    _save_board_cache(cfg, cache)


def invalidate_cached_github_project_board_snapshot(cfg: ResolvedConfig, owner: str, project: int) -> None:
    owner_text = str(owner or "").strip().lower()
    if not owner_text or project in (None, ""):
        return
    cache = _load_board_cache(cfg)
    key = _github_project_board_cache_key(owner_text, project)
    if key not in cache:
        return
    cache.pop(key, None)
    _save_board_cache(cfg, cache)


def _normalize_issue_body(body: str | None) -> tuple[str, list[str]]:
    if not body:
        return "", []
    lines = [line.strip() for line in body.splitlines()]
    summary = next((line for line in lines if line), "")
    criteria = [
        line.lstrip("-* ").strip()
        for line in lines
        if line.startswith("- [ ]") or line.startswith("* [ ]") or line.startswith("- ")
    ]
    criteria = [line for line in criteria if line]
    return summary, criteria


def _normalized_task_state(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.replace(" ", "_")


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _task_dependency_status(
    task: dict[str, Any],
    coordination: CoordinationStore | None = None,
) -> dict[str, Any]:
    known_tasks: list[dict[str, Any]] | None = None
    if coordination is not None:
        try:
            known_tasks = coordination.list_tasks(limit=1000)
        except Exception:
            logger.debug("Failed to load known tasks for dependency resolution", exc_info=True)
            known_tasks = None
    return dependency_status_for_task(task, known_tasks if known_tasks else None)


def _annotate_task_contract(
    task: dict[str, Any],
    coordination: CoordinationStore | None = None,
) -> dict[str, Any]:
    task = apply_task_contract(task)
    task["contract_completeness"] = task_contract_completeness(task)
    task["dependency_status"] = _task_dependency_status(task, coordination)
    return task


def _project_item_to_task(
    *,
    item: dict[str, Any],
    owner: str,
    project_number: int,
    repo_name: str,
) -> dict[str, Any]:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    title = str(item.get("title") or (content or {}).get("title") or "GitHub Project item").strip()
    body = str((content or {}).get("body") or item.get("body") or item.get("notes") or "").strip()
    issue_number = None
    if isinstance(content, dict) and content.get("number") not in (None, ""):
        issue_number = int(content.get("number"))
    project_item_id = item.get("project_item_id")
    task = {
        "task_id": str(project_item_id or issue_number or item.get("id") or title).strip(),
        "title": title,
        "description": body,
        "priority": None,
        "labels": [],
        "source": {
            "type": "github_project",
            "owner": owner,
            "project": project_number,
            "repo_name": repo_name,
            "project_item_id": project_item_id,
            "issue_number": issue_number,
            "item": str(project_item_id or issue_number or item.get("id") or title).strip(),
            "url": str(item.get("item_url") or item.get("url") or ""),
            "project_url": str(item.get("project_url") or ""),
            "item_url": str(item.get("item_url") or ""),
            "issue_url": str((content or {}).get("html_url") or (content or {}).get("url") or ""),
        },
        "repo": {"slug": repo_name},
        "status": str(item.get("effective_status_name") or item.get("status_name") or "").strip(),
        "state": _normalized_task_state(item.get("effective_status_name") or item.get("status_name") or ""),
    }
    if task["state"] in {"done", "completed"}:
        task["state"] = "done"
    return _annotate_task_contract(task)


def _project_dependency_known_tasks(
    *,
    items: list[dict[str, Any]],
    owner: str,
    project_number: int,
    repo_name: str,
    coordination: CoordinationStore | None = None,
) -> list[dict[str, Any]]:
    known_tasks = [
        _project_item_to_task(item=item, owner=owner, project_number=project_number, repo_name=repo_name)
        for item in items
    ]
    if coordination is not None:
        try:
            known_tasks.extend(coordination.list_tasks(limit=1000))
        except Exception:
            logger.debug("Failed to load known tasks for dependency resolution", exc_info=True)
    return known_tasks


def _annotate_project_dependency_status(
    *,
    task: dict[str, Any],
    items: list[dict[str, Any]],
    owner: str,
    project_number: int,
    repo_name: str,
    coordination: CoordinationStore | None = None,
) -> dict[str, Any]:
    known_tasks = _project_dependency_known_tasks(
        items=items,
        owner=owner,
        project_number=project_number,
        repo_name=repo_name,
        coordination=coordination,
    )
    dependency_status = dependency_status_for_task(task, known_tasks if known_tasks else None)
    task["dependency_status"] = dependency_status
    return task


def _flatten_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "nodes", "projectItems", "projects", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict) and "nodes" in value and isinstance(value["nodes"], list):
                return [item for item in value["nodes"] if isinstance(item, dict)]
    return []


def _project_field_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("raw", "html", "text", "title", "name", "value"):
            nested = value.get(key)
            text = _project_field_text(nested)
            if text:
                return text
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _github_project_name(schema: dict[str, Any], owner: str, project: int) -> str:
    name = _project_field_text(schema.get("title") or schema.get("name"))
    return name or f"{str(owner).strip()}/{int(project)}"


def _tool_result_values(result: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and "result" in metadata:
        values.append(metadata["result"])
    output = result.get("output")
    if isinstance(output, str) and output.strip():
        try:
            values.append(json.loads(output))
        except Exception:
            logger.debug("Failed to parse tool result as JSON", exc_info=True)
    return values


def _collect_project_items(value: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        project_item_id = str(value.get("id") or value.get("item_id") or "").strip()
        title = str(
            value.get("title")
            or ((value.get("content") or {}).get("title") if isinstance(value.get("content"), dict) else "")
            or ""
        ).strip()
        status = value.get("status")
        status_name = ""
        if isinstance(status, dict):
            status_name = str(status.get("name") or "").strip()
        elif isinstance(status, str):
            status_name = status.strip()
        if not status_name:
            status_name = str(value.get("status_name") or value.get("statusName") or "").strip()
        if not status_name:
            field_values = value.get("field_values") or value.get("fieldValues")
            if isinstance(field_values, dict):
                nested_status = field_values.get("status")
                if isinstance(nested_status, dict):
                    status_name = str(nested_status.get("name") or "").strip()
            elif isinstance(field_values, list):
                for field in field_values:
                    if not isinstance(field, dict):
                        continue
                    field_name = _project_field_text(
                        field.get("name") or (field.get("field") or {}).get("name")
                    ).lower()
                    if field_name != "status":
                        continue
                    status_name = _project_field_text(
                        field.get("value")
                        or field.get("name")
                        or field.get("option")
                        or field.get("displayValue")
                    )
                    if status_name:
                        break
        fields = value.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                field_name = _project_field_text(field.get("name")).lower()
                if field_name == "title" and not title:
                    title = _project_field_text(field.get("value"))
                if field_name == "status" and not status_name:
                    status_name = _project_field_text(field.get("value"))
        content = value.get("content")
        if project_item_id:
            out.append(
                {
                    "project_item_id": project_item_id,
                    "title": title,
                    "status_name": status_name,
                    "project_url": str(value.get("project_url") or "").strip(),
                    "item_url": str(value.get("item_url") or "").strip(),
                    "content": content if isinstance(content, dict) else {},
                    "raw": value,
                }
            )
            return
        for nested in value.values():
            _collect_project_items(nested, out)
    elif isinstance(value, list):
        for row in value:
            _collect_project_items(row, out)


def _extract_project_schema(result: dict[str, Any]) -> dict[str, Any]:
    for value in _tool_result_values(result):
        if isinstance(value, dict):
            if isinstance(value.get("fields"), list):
                return value
            project = value.get("project")
            if isinstance(project, dict) and isinstance(project.get("fields"), list):
                return project
            content = value.get("content")
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
                        logger.debug("Failed to parse raw github MCP project JSON text", exc_info=True)
                        continue
                    if isinstance(parsed, dict) and isinstance(parsed.get("fields"), list):
                        return parsed
    raise RuntimeError("Could not extract GitHub project schema from MCP result.")


def _normalized_status_option_map(schema: dict[str, Any]) -> tuple[int | None, dict[str, str]]:
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return None, {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        field_name = _project_field_text(field.get("name")).lower()
        if field_name != "status":
            continue
        field_id = field.get("id")
        if field_id is None:
            return None, {}
        option_map: dict[str, str] = {}
        for option in field.get("options") or []:
            if not isinstance(option, dict):
                continue
            option_name = _project_field_text(option.get("name"))
            option_id = str(option.get("id") or "").strip()
            if option_name and option_id:
                option_map[option_name.strip().lower().replace("-", "_").replace(" ", "_")] = option_id
        return int(field_id), option_map
    return None, {}


def _github_token(cfg: ResolvedConfig) -> str:
    for key in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"):
        value = str(cfg.env.get(key) or "").strip()
        if value:
            return value
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
            return token
    return ""


def _github_graphql(cfg: ResolvedConfig, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = _github_token(cfg)
    if not token:
        return {}
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
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
        with urlopen(request, timeout=15) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, json.JSONDecodeError):
        logger.debug("Failed to fetch GitHub Project item statuses through GraphQL", exc_info=True)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hydrate_project_item_statuses_from_graphql(
    cfg: ResolvedConfig,
    schema: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    status_names: dict[str, str] = {}
    fields = schema.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            if _project_field_text(field.get("name")).strip().lower() != "status":
                continue
            for option in field.get("options") or []:
                if not isinstance(option, dict):
                    continue
                name = _project_field_text(option.get("name"))
                key = normalize_status_key(name)
                if key and name:
                    status_names[key] = name
    node_to_item: dict[str, dict[str, Any]] = {}
    for item in items:
        if str(item.get("status_name") or "").strip():
            continue
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        node_id = str(raw.get("node_id") or raw.get("nodeId") or "").strip()
        if node_id:
            node_to_item[node_id] = item
    if not node_to_item or not status_names:
        return
    query = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProjectV2Item {
      id
      fieldValues(first: 50) {
        nodes {
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
          }
        }
      }
    }
  }
}
"""
    node_ids = list(node_to_item)
    for index in range(0, len(node_ids), 50):
        batch = node_ids[index : index + 50]
        payload = _github_graphql(cfg, query, {"ids": batch})
        nodes = ((payload.get("data") or {}).get("nodes") or []) if isinstance(payload, dict) else []
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            item = node_to_item.get(str(node.get("id") or ""))
            if item is None:
                continue
            field_values = ((node.get("fieldValues") or {}).get("nodes") or [])
            if not isinstance(field_values, list):
                continue
            for field_value in field_values:
                if not isinstance(field_value, dict):
                    continue
                key = normalize_status_key(field_value.get("name"))
                status_name = status_names.get(key)
                if status_name:
                    item["status_name"] = status_name
                    break


def _item_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for nested in ("title", "body", "url", "number", "id"):
            nested_value = value.get(nested)
            if nested_value not in (None, ""):
                return str(nested_value)
    return ""


def _effective_project_status(
    cfg: ResolvedConfig,
    *,
    owner: str,
    project_number: int,
    item: dict[str, Any],
) -> tuple[str, str]:
    live_status = str(item.get("status_name") or "").strip()
    item_id = item.get("project_item_id")
    if live_status and item_id not in (None, ""):
        remember_project_item_status(
            cfg,
            owner=owner,
            project_number=project_number,
            item_id=item_id,
            status_name=live_status,
            source="github_project.intake.live_status",
        )
    cached_status = ""
    if item_id not in (None, ""):
        cached_status = cached_project_item_status(
            cfg,
            owner=owner,
            project_number=project_number,
            item_id=item_id,
        )
    effective = live_status or cached_status
    return effective, normalize_status_key(effective)


def _select_github_project_item(
    cfg: ResolvedConfig,
    *,
    owner: str,
    project: int,
    items: list[dict[str, Any]],
    allow_non_actionable: bool = False,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    selector = str(cfg.task_source.item or cfg.task_source.url or "").strip()
    if selector:
        for item in items:
            haystacks = [
                str(item.get("id", "")),
                str(item.get("number", "")),
                str(item.get("url", "")),
                _item_text(item, "title"),
                _item_text(item, "content"),
            ]
            if not any(selector == hay or selector in hay for hay in haystacks if hay):
                continue
            status_key = str(item.get("effective_status_key") or "").strip()
            status_name = str(item.get("effective_status_name") or item.get("status_name") or "").strip()
            eligible = github_project_status_key_is_actionable(status_name or status_key)
            if not eligible and not allow_non_actionable:
                raise RuntimeError(
                    f"Selected GitHub Project item is not actionable: "
                    f"status is '{status_name or status_key}'."
                )
            warning = None
            if not eligible:
                warning = (
                    f"Selected GitHub Project item is not actionable: "
                    f"status is '{status_name or status_key}'."
                )
            return item, eligible, warning

    preferred_statuses = (
        ("ready", "ready to pick up"),
        ("backlog",),
        ("todo", "todos", "to do", "to-do"),
    )
    skip_statuses = {"in_review", "done", "blocked", "in_progress", "stale"}
    filtered_items = [
        item
        for item in items
        if normalize_status_key(str(item.get("effective_status_name") or "")) not in skip_statuses
    ]

    def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
        labels: list[str] = []
        content = item.get("content")
        if isinstance(content, dict):
            raw_labels = content.get("labels") or []
            if isinstance(raw_labels, list):
                for lbl in raw_labels:
                    if isinstance(lbl, dict):
                        labels.append(str(lbl.get("name") or "").lower())
                    elif isinstance(lbl, str):
                        labels.append(lbl.lower())
        priority_rank = 99
        for lbl in labels:
            import re as _re

            m = _re.search(r"\bp(\d)\b", lbl)
            if m:
                priority_rank = int(m.group(1))
                break
        if isinstance(content, dict) and content.get("number") not in (None, ""):
            issue_num = int(content["number"])
        else:
            try:
                issue_num = int(str(item.get("project_item_id") or item.get("id") or 9999999))
            except Exception:
                logger.debug(f"Failed to parse issue number for sort: {item.get('id')}", exc_info=True)
                issue_num = 9999999
        return (priority_rank, issue_num)

    filtered_items.sort(key=_sort_key)
    normalized_groups: dict[str, list[dict[str, Any]]] = {}
    for item in filtered_items:
        status_name = str(item.get("effective_status_name") or "").strip().lower()
        normalized_groups.setdefault(status_name, []).append(item)

    chosen: dict[str, Any] | None = None
    for aliases in preferred_statuses:
        for alias in aliases:
            if normalized_groups.get(alias):
                chosen = normalized_groups[alias][0]
                break
        if chosen is not None:
            break

    if chosen is None:
        if filtered_items:
            chosen = filtered_items[0]
        else:
            actionable_unknown = [
                item for item in items if not str(item.get("effective_status_key") or "").strip()
            ]
            if actionable_unknown:
                chosen = actionable_unknown[0]
            elif allow_non_actionable and items:
                chosen = items[0]

    if chosen is None:
        found_statuses = sorted(
            set(
                str(item.get("effective_status_name") or "").strip()
                for item in items
                if item.get("effective_status_name")
            )
        )
        if found_statuses:
            raise RuntimeError(
                f"No actionable GitHub Project items in {owner}/{project}. "
                "Expected a launchable status like 'Todo' or 'TODOS', and ACA now refuses "
                "to re-pick known 'In progress', 'In review', 'Blocked', or 'Done' items. "
                f"Found statuses: {found_statuses}"
            )
        raise RuntimeError(
            f"Could not determine an actionable GitHub Project item in {owner}/{project}. "
            "GitHub MCP did not return item statuses, and ACA has no cached last-known "
            "status for any candidate. Move the intended card to a launchable lane like "
            "'Todo' or 'TODOS' and re-run after "
            "ACA has observed or updated its status at least once."
        )

    status_key = str(chosen.get("effective_status_key") or "").strip()
    status_name = str(chosen.get("effective_status_name") or chosen.get("status_name") or "").strip()
    eligible = github_project_status_key_is_actionable(status_name or status_key)
    warning = None
    if not eligible and allow_non_actionable:
        warning = (
            f"No actionable GitHub Project items in {owner}/{project}. "
            "Showing the current board item instead."
        )
    return chosen, eligible, warning


def _load_github_project_live_data(
    cfg: ResolvedConfig,
    *,
    owner: str,
    project_number: int,
    refresh_server: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ensure_github_mcp_connected(cfg)
    if refresh_server:
        try:
            refresh_mcp_server(cfg, "github")
        except Exception:
            logger.debug("Failed to refresh github MCP server during snapshot", exc_info=True)
    schema = _github_project_schema(cfg, owner, project_number)
    payload = _github_project_items(cfg, owner, project_number)
    items: list[dict[str, Any]] = []
    for candidate in _tool_result_values(payload):
        _collect_project_items(candidate, items)
    if not items:
        raise RuntimeError(f"No items returned for GitHub project {owner}/{project_number}")
    if any(not str(item.get("status_name") or "").strip() for item in items):
        _hydrate_project_item_statuses_from_graphql(cfg, schema, items)
    for item in items:
        effective_status_name, effective_status_key = _effective_project_status(
            cfg,
            owner=owner,
            project_number=project_number,
            item=item,
        )
        if not effective_status_key and item.get("project_item_id") not in (None, ""):
            try:
                detail = fetch_project_item(cfg, owner, project_number, int(item["project_item_id"]))
                detail_items: list[dict[str, Any]] = []
                _collect_project_items(detail, detail_items)
                if detail_items:
                    detail_item = detail_items[0]
                    effective_status_name, effective_status_key = _effective_project_status(
                        cfg,
                        owner=owner,
                        project_number=project_number,
                        item=detail_item,
                    )
            except Exception:
                logger.debug("Failed to fetch detail for project item", exc_info=True)
        item["effective_status_name"] = effective_status_name
        item["effective_status_key"] = effective_status_key
    return schema, items


def github_project_board_snapshot(
    cfg: ResolvedConfig,
    *,
    force_refresh: bool = False,
    cache_ttl_seconds: int = 90,
) -> dict[str, Any]:
    owner = str(cfg.task_source.owner or "").strip()
    project_number = int(cfg.task_source.project)
    repo_name = str(cfg.task_source.repo or "").strip()
    now_ms = int(time.time() * 1000)
    cached = _read_cached_board_snapshot(cfg, owner, project_number)
    if not force_refresh and cached:
        last_synced_at_ms = int(cached.get("last_synced_at_ms") or 0)
        if last_synced_at_ms and now_ms - last_synced_at_ms <= cache_ttl_seconds * 1000:
            snapshot = dict(cached)
            snapshot["source"] = "cached"
            snapshot["is_stale"] = False
            snapshot["cache_age_ms"] = now_ms - last_synced_at_ms
            return snapshot
    try:
        schema, items = _load_github_project_live_data(
            cfg,
            owner=owner,
            project_number=project_number,
            refresh_server=force_refresh,
        )
    except RuntimeError as exc:
        if cached:
            snapshot = dict(cached)
            snapshot["source"] = "cached"
            snapshot["is_stale"] = True
            snapshot["warning"] = str(exc)
            snapshot["cache_age_ms"] = now_ms - int(snapshot.get("last_synced_at_ms") or 0)
            return snapshot
        raise
    status_field_id, status_option_map = _normalized_status_option_map(schema)

    columns: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    fields = schema.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            if _project_field_text(field.get("name")).strip().lower() != "status":
                continue
            for option in field.get("options") or []:
                if not isinstance(option, dict):
                    continue
                name = _project_field_text(option.get("name"))
                key = normalize_status_key(name)
                if not name or not key:
                    continue
                seen_keys.add(key)
                columns.append(
                    {
                        "id": str(option.get("id") or key),
                        "name": name,
                        "key": key,
                    }
                )

    board_items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in items:
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        status_name = str(item.get("effective_status_name") or item.get("status_name") or "Unknown").strip() or "Unknown"
        status_key = normalize_status_key(status_name) or "unknown"
        if status_key not in seen_keys:
            seen_keys.add(status_key)
            columns.append({"id": status_key, "name": status_name, "key": status_key})
        counts[status_key] = counts.get(status_key, 0) + 1
        issue_number = content.get("number") if isinstance(content, dict) else None
        title = str(item.get("title") or (content or {}).get("title") or "Untitled item").strip() or "Untitled item"
        repository = (content or {}).get("repository") if isinstance(content, dict) else None
        if isinstance(repository, dict):
            repo_name_value = str(repository.get("full_name") or repository.get("name") or repo_name or "").strip()
        else:
            repo_name_value = str(repository or repo_name or "").strip()
        board_items.append(
            {
                "id": str(item.get("project_item_id") or item.get("item_id") or title),
                "project_item_id": item.get("project_item_id"),
                "title": title,
                "project_name": _github_project_name(schema, owner, project_number),
                "project_column": status_name,
                "status_name": status_name,
                "status_key": status_key,
                "issue_number": issue_number,
                "issue_url": str((content or {}).get("html_url") or (content or {}).get("url") or ""),
                "repo_name": repo_name_value,
                "content_type": str((content or {}).get("type") or ""),
                "actionable": github_project_status_key_is_actionable(status_name),
            }
        )

    # GitHub MCP can intermittently omit the status field for the next actionable card.
    # When that happens, align the board snapshot with ACA.s actual intake choice so the
    # operator sees the same "next up" item in both the board and preview surfaces.
    try:
        preview = preview_task(cfg)
    except Exception:
        logger.debug("Failed to fetch preview task for board snapshot sync", exc_info=True)
        preview = {}
    preview_source = dict(((preview.get("task") or {}).get("source") or {}))
    preview_item_id = preview_source.get("project_item_id")
    preview_issue_number = preview_source.get("issue_number")
    for item in board_items:
        if item.get("status_key") != "unknown":
            continue
        same_item = (
            (preview_item_id not in (None, "") and item.get("project_item_id") == preview_item_id)
            or (preview_issue_number not in (None, "") and item.get("issue_number") == preview_issue_number)
        )
        if not same_item:
            continue
        item["status_key"] = "ready"
        item["status_name"] = "Ready"
        item["actionable"] = True
        counts["unknown"] = max(0, counts.get("unknown", 0) - 1)
        counts["ready"] = counts.get("ready", 0) + 1
        if "ready" not in seen_keys:
            seen_keys.add("ready")
            columns.append({"id": "ready", "name": "Ready", "key": "ready"})
        if item.get("project_item_id") not in (None, ""):
            remember_project_item_status(
                cfg,
                owner=owner,
                project_number=project_number,
                item_id=item["project_item_id"],
                status_name="Ready",
                source="github_project.board_snapshot.preview_alignment",
            )
        break

    board_items.sort(
        key=lambda item: (
            next((index for index, column in enumerate(columns) if column.get("key") == item.get("status_key")), 999),
            int(item.get("issue_number") or 999999),
            str(item.get("title") or "").lower(),
        )
    )
    for column in columns:
        column["item_count"] = counts.get(str(column.get("key") or ""), 0)

    snapshot = {
        "project": {
            "owner": owner,
            "repo": repo_name,
            "project_number": project_number,
            "name": _github_project_name(schema, owner, project_number),
        },
        "status_field_id": status_field_id,
        "status_option_map": status_option_map,
        "columns": columns,
        "items": board_items,
        "source": "live",
        "is_stale": False,
        "warning": "",
        "last_synced_at_ms": now_ms,
        "cache_age_ms": 0,
    }
    _write_cached_board_snapshot(cfg, owner, project_number, snapshot)
    return snapshot


def _task_from_manual(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path | None]:
    prompt = cfg.task_source.prompt.strip()
    title = prompt.splitlines()[0] if prompt else "Manual task"
    task = _annotate_task_contract(
        {
            "title": title,
            "description": prompt,
            "acceptance_criteria": [],
            "labels": ["manual"],
            "priority": None,
            "source": {"type": "manual", "prompt": prompt},
            "repo": {"path": str(cfg.repository_path() or "")},
        },
        coordination=coordination,
    )
    board = default_board()
    card = task_to_card(task, lane="ready")
    board["cards"].append(card)
    returned = apply_task_contract(card_to_task(card))
    returned["dependency_status"] = task.get("dependency_status") or {}
    returned["contract_completeness"] = task.get("contract_completeness") or {}
    returned["task_contract"] = task.get("task_contract") or returned.get("task_contract") or {}
    returned["project_schema"] = task.get("project_schema") or returned.get("project_schema") or {}
    returned["program_goal"] = task.get("program_goal") or returned.get("program_goal")
    returned["local_goal"] = task.get("local_goal") or returned.get("local_goal")
    returned["in_scope"] = list(task.get("in_scope") or returned.get("in_scope") or [])
    returned["out_of_scope"] = list(task.get("out_of_scope") or returned.get("out_of_scope") or [])
    returned["dependencies"] = list(task.get("dependencies") or returned.get("dependencies") or [])
    returned["deliverables"] = list(task.get("deliverables") or returned.get("deliverables") or [])
    returned["target_files"] = list(task.get("target_files") or returned.get("target_files") or [])
    returned["verification_commands"] = list(task.get("verification_commands") or returned.get("verification_commands") or [])
    returned["acceptance_criteria"] = list(task.get("acceptance_criteria") or returned.get("acceptance_criteria") or [])
    returned["notes_for_agent"] = task.get("notes_for_agent") or returned.get("notes_for_agent")
    returned["subtasks"] = list(task.get("subtasks") or returned.get("subtasks") or [])
    return returned, board, None


def _task_from_local_backlog(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path | None]:
    backlog_path = Path(cfg.task_source.path).expanduser()
    if not backlog_path.is_absolute():
        backlog_path = cfg.root_dir / backlog_path
    text = backlog_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    selected = None
    if cfg.task_source.card_id:
        for line in lines:
            if cfg.task_source.card_id in line:
                selected = line
                break
    if selected is None:
        selected = next((line for line in lines if not line.startswith("#")), "Backlog item")
    title = selected.lstrip("-* ").strip()
    _, criteria = _normalize_issue_body(text)
    task = _annotate_task_contract(
        {
            "title": title,
            "description": text,
            "acceptance_criteria": criteria,
            "labels": ["backlog"],
            "priority": None,
            "source": {"type": "local_backlog", "path": str(backlog_path)},
            "repo": {"path": str(cfg.repository_path() or "")},
        },
        coordination=coordination,
    )
    board = default_board()
    card = task_to_card(task, lane="ready")
    board["cards"].append(card)
    returned = apply_task_contract(card_to_task(card))
    returned["dependency_status"] = task.get("dependency_status") or {}
    returned["contract_completeness"] = task.get("contract_completeness") or {}
    return returned, board, None


def _task_from_kanban_board(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    board_path = cfg.task_source_path()
    if board_path is None:
        raise RuntimeError("Kanban board task source requires task_source.path")
    board = ensure_board_template(board_path)
    from src.tandem_agents.core.repository.board import select_card

    card = select_card(board, cfg.task_source.card_id or None)
    if card is None:
        raise RuntimeError(f"No cards available in kanban board: {board_path}")
    task = _annotate_task_contract(card_to_task(card, board_path=board_path), coordination=coordination)
    task["source"]["type"] = "kanban_board"
    task["source"]["board_path"] = str(board_path)
    return task, board, board_path


def _task_from_project(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path | None]:
    project = cfg.task_source.project
    owner = cfg.task_source.owner
    selector = str(cfg.task_source.item or cfg.task_source.url or "").strip()
    try:
        project_number = int(project)
        schema, items = _load_github_project_live_data(
            cfg,
            owner=owner,
            project_number=project_number,
            refresh_server=False,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "Could not read GitHub Project items through Tandem's built-in GitHub MCP path. "
            f"Project: {owner}/{project}. "
            "Verify Tandem has a connected GitHub MCP server and a valid GitHub PAT with Project access. "
            f"Engine error: {exc}"
        ) from exc
    chosen, eligible, warning = _select_github_project_item(
        cfg,
        owner=owner,
        project=project_number,
        items=items,
        allow_non_actionable=False,
    )
    if not eligible or chosen is None:
        raise RuntimeError(
            warning
            or f"Could not determine an actionable GitHub Project item in {owner}/{project}."
        )
    content = chosen.get("content") if isinstance(chosen.get("content"), dict) else {}
    title = str(chosen.get("title") or (content or {}).get("title") or "GitHub Project item")
    project_name = _github_project_name(schema, owner, project_number)
    project_column = str(chosen.get("effective_status_name") or chosen.get("status_name") or "").strip() or None
    body = str((content or {}).get("body") or chosen.get("body") or chosen.get("notes") or "")
    _, criteria = _normalize_issue_body(body)
    item_url = ""
    if isinstance(content, dict):
        item_url = str(content.get("url") or content.get("html_url") or "")
    status_field_id, status_option_map = _normalized_status_option_map(schema)
    repo_slug = str(cfg.repository.slug or "").strip()
    if not repo_slug:
        repo_slug = f"{str(owner).strip()}/{str(cfg.task_source.repo or '').strip()}".strip("/")
    repo_binding = {
        "path": str(cfg.repository_path() or ""),
        "slug": repo_slug,
        "clone_url": str(cfg.repository.clone_url or ""),
        "default_branch": str(cfg.repository.default_branch or ""),
        "remote_name": str(cfg.repository.remote_name or ""),
        "credential_file": str(cfg.repository.credential_file or ""),
    }
    issue_number = None
    if isinstance(content, dict) and content.get("number") not in (None, ""):
        issue_number = int(content.get("number"))
    issue_url = ""
    if isinstance(content, dict):
        issue_url = str(content.get("html_url") or content.get("url") or "")
    task = apply_task_contract(
        {
            "title": title,
            "description": body,
            "acceptance_criteria": criteria,
            "labels": [],
            "priority": None,
            "project_name": project_name,
            "project_column": project_column,
            "source": {
                "type": "github_project",
                "owner": owner,
                "project": project,
                "project_name": project_name,
                "project_column": project_column,
                "repo_name": cfg.task_source.repo or "",
                "item": selector or str(chosen.get("project_item_id") or chosen.get("id") or title),
                "url": item_url or cfg.task_source.url or "",
                "status": str(chosen.get("effective_status_name") or chosen.get("status_name") or ""),
                "initial_status_name": str(chosen.get("effective_status_name") or chosen.get("status_name") or ""),
                "project_item_id": int(chosen.get("project_item_id") or 0) or None,
                "project_url": str(chosen.get("project_url") or ""),
                "item_url": str(chosen.get("item_url") or ""),
                "issue_number": issue_number,
                "issue_url": issue_url,
                "status_field_id": status_field_id,
                "status_option_map": status_option_map,
                "mcp_server": "github",
            },
            "repo": repo_binding,
            "project_schema": schema,
        }
    )
    task = _annotate_project_dependency_status(
        task=task,
        items=items,
        owner=owner,
        project_number=project_number,
        repo_name=repo_slug,
        coordination=coordination,
    )
    task["contract_completeness"] = task_contract_completeness(task)
    board = default_board()
    card = task_to_card(task, lane="ready")
    board["cards"].append(card)
    returned = apply_task_contract(card_to_task(card))
    returned["dependency_status"] = task.get("dependency_status") or {}
    returned["contract_completeness"] = task.get("contract_completeness") or {}
    returned["task_contract"] = task.get("task_contract") or returned.get("task_contract") or {}
    returned["program_goal"] = task.get("program_goal") or returned.get("program_goal")
    returned["local_goal"] = task.get("local_goal") or returned.get("local_goal")
    returned["in_scope"] = list(task.get("in_scope") or returned.get("in_scope") or [])
    returned["out_of_scope"] = list(task.get("out_of_scope") or returned.get("out_of_scope") or [])
    returned["dependencies"] = list(task.get("dependencies") or returned.get("dependencies") or [])
    returned["deliverables"] = list(task.get("deliverables") or returned.get("deliverables") or [])
    returned["target_files"] = list(task.get("target_files") or returned.get("target_files") or [])
    returned["verification_commands"] = list(task.get("verification_commands") or returned.get("verification_commands") or [])
    returned["acceptance_criteria"] = list(task.get("acceptance_criteria") or returned.get("acceptance_criteria") or [])
    returned["notes_for_agent"] = task.get("notes_for_agent") or returned.get("notes_for_agent")
    returned["subtasks"] = list(task.get("subtasks") or returned.get("subtasks") or [])
    return returned, board, None


def _task_from_custom(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path | None]:
    payload = dict(cfg.task_source.payload or {})
    title = str(payload.get("title") or cfg.task_source.source_name or "Custom task")
    description = str(payload.get("description") or payload.get("body") or "")
    criteria = [str(entry).strip() for entry in _as_list(payload.get("acceptance_criteria")) if str(entry).strip()]
    task = _annotate_task_contract(
        {
            "title": title,
            "description": description,
            "acceptance_criteria": criteria,
            "labels": [str(entry).strip() for entry in _as_list(payload.get("labels")) if str(entry).strip()],
            "priority": payload.get("priority"),
            "source": {
                "type": "custom",
                "source_name": cfg.task_source.source_name,
                "payload": payload,
            },
            "repo": {"path": str(cfg.repository_path() or "")},
        },
        coordination=coordination,
    )
    board = default_board()
    card = task_to_card(task, lane="ready")
    board["cards"].append(card)
    returned = apply_task_contract(card_to_task(card))
    returned["dependency_status"] = task.get("dependency_status") or {}
    returned["contract_completeness"] = task.get("contract_completeness") or {}
    return returned, board, None


def normalize_task(
    cfg: ResolvedConfig,
    coordination: CoordinationStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path | None]:
    source_type = cfg.task_source.type
    if source_type == "manual":
        return _task_from_manual(cfg, coordination=coordination)
    if source_type == "local_backlog":
        return _task_from_local_backlog(cfg, coordination=coordination)
    if source_type == "kanban_board":
        return _task_from_kanban_board(cfg, coordination=coordination)
    if source_type == "github_project":
        return _task_from_project(cfg, coordination=coordination)
    if source_type == "custom":
        return _task_from_custom(cfg, coordination=coordination)
    raise RuntimeError(f"Unsupported task source type: {source_type}")


def preview_task(cfg: ResolvedConfig, coordination: CoordinationStore | None = None) -> dict[str, Any]:
    source_type = cfg.task_source.type
    if source_type == "github_project":
        project = cfg.task_source.project
        owner = cfg.task_source.owner
        try:
            project_number = int(project)
            schema, items = _load_github_project_live_data(
                cfg,
                owner=owner,
                project_number=project_number,
                refresh_server=False,
            )
            chosen, eligible, warning = _select_github_project_item(
                cfg,
                owner=owner,
                project=project_number,
                items=items,
                allow_non_actionable=True,
            )
        except RuntimeError as error:
            raise error
        content = chosen.get("content") if isinstance(chosen.get("content"), dict) else {}
        status_name = str(chosen.get("effective_status_name") or chosen.get("status_name") or "").strip()
        status_key = str(chosen.get("effective_status_key") or normalize_status_key(status_name) or "")
        title = str(chosen.get("title") or (content or {}).get("title") or "GitHub Project item")
        issue_number = None
        if isinstance(content, dict) and content.get("number") not in (None, ""):
            issue_number = int(content.get("number"))
        issue_url = ""
        if isinstance(content, dict):
            issue_url = str(content.get("html_url") or content.get("url") or "")
        board_summary: dict[str, int] = {}
        for item in items:
            item_status_name = str(item.get("effective_status_name") or item.get("status_name") or "Unknown").strip() or "Unknown"
            item_status_key = normalize_status_key(item_status_name) or "unknown"
            board_summary[item_status_key] = board_summary.get(item_status_key, 0) + 1
        selected_task = apply_task_contract(
            {
                "title": title,
                "description": str((content or {}).get("body") or chosen.get("body") or chosen.get("notes") or ""),
                "acceptance_criteria": _normalize_issue_body(str((content or {}).get("body") or chosen.get("body") or chosen.get("notes") or ""))[1],
                "labels": [],
                "priority": None,
                "project_name": _github_project_name(schema, owner, project_number),
                "project_column": status_name or None,
                "source": {
                    "type": "github_project",
                    "owner": owner,
                    "project": project,
                    "project_name": _github_project_name(schema, owner, project_number),
                    "project_column": status_name or None,
                    "repo_name": cfg.task_source.repo or "",
                    "item": str(chosen.get("project_item_id") or chosen.get("id") or title),
                    "url": str(chosen.get("item_url") or cfg.task_source.url or ""),
                    "status": status_name,
                    "initial_status_name": status_name,
                    "project_item_id": int(chosen.get("project_item_id") or 0) or None,
                    "project_url": str(chosen.get("project_url") or ""),
                    "item_url": str(chosen.get("item_url") or ""),
                    "issue_number": issue_number,
                    "issue_url": issue_url,
                    "status_field_id": _normalized_status_option_map(schema)[0],
                    "status_option_map": _normalized_status_option_map(schema)[1],
                    "mcp_server": "github",
                },
                "repo": {"path": str(cfg.repository_path() or ""), "slug": cfg.task_source.repo or ""},
                "project_schema": schema,
            }
        )
        selected_task = _annotate_project_dependency_status(
            task=selected_task,
            items=items,
            owner=owner,
            project_number=project_number,
            repo_name=cfg.task_source.repo or "",
            coordination=coordination,
        )
        selected_task["contract_completeness"] = task_contract_completeness(selected_task)
        blocked_reason = (selected_task.get("dependency_status") or {}).get("blocked_reason")
        if not (selected_task.get("contract_completeness") or {}).get("ok", True):
            blocked_reason = blocked_reason or (selected_task.get("contract_completeness") or {}).get("blocker_message")
        preview: dict[str, Any] = {
            "eligible": bool(
                eligible
                and (selected_task.get("contract_completeness") or {}).get("ok", True)
                and not (selected_task.get("dependency_status") or {}).get("blocked")
            ),
            "task": {
                "title": selected_task.get("title"),
                "id": chosen.get("project_item_id"),
                "project_name": selected_task.get("project_name"),
                "project_column": status_name or None,
                "source": selected_task.get("source"),
                "lane": status_key or "ready",
                "labels": selected_task.get("labels", []),
                "description": selected_task.get("description"),
                "task_contract": selected_task.get("task_contract"),
                "program_goal": selected_task.get("program_goal"),
                "local_goal": selected_task.get("local_goal"),
                "in_scope": selected_task.get("in_scope"),
                "out_of_scope": selected_task.get("out_of_scope"),
                "dependencies": selected_task.get("dependencies"),
                "deliverables": selected_task.get("deliverables"),
                "target_files": selected_task.get("target_files"),
                "verification_commands": selected_task.get("verification_commands"),
                "acceptance_criteria": selected_task.get("acceptance_criteria"),
                "notes_for_agent": selected_task.get("notes_for_agent"),
                "dependency_status": selected_task.get("dependency_status"),
                "contract_completeness": selected_task.get("contract_completeness"),
                "raw_issue_body": selected_task.get("raw_issue_body"),
            },
            "source_type": source_type,
            "board_path": None,
            "board_summary": board_summary,
        }
        if warning:
            preview["warning"] = warning
        if blocked_reason:
            preview["blocked_reason"] = blocked_reason
        preview["task"]["task_id"] = selected_task.get("task_id")
        return preview
    task, board, board_path = normalize_task(cfg, coordination=coordination)
    blocked_reason = (task.get("dependency_status") or {}).get("blocked_reason")
    if not (task.get("contract_completeness") or {}).get("ok", True):
        blocked_reason = blocked_reason or (task.get("contract_completeness") or {}).get("blocker_message")
    preview = {
        "eligible": bool((task.get("contract_completeness") or {}).get("ok", True) and not (task.get("dependency_status") or {}).get("blocked")),
        "task": {
            "title": task.get("title"),
            "id": task.get("id"),
            "source": task.get("source"),
            "lane": task.get("lane", "ready"),
            "labels": task.get("labels", []),
            "task_contract": task.get("task_contract"),
            "program_goal": task.get("program_goal"),
            "local_goal": task.get("local_goal"),
            "in_scope": task.get("in_scope"),
            "out_of_scope": task.get("out_of_scope"),
            "dependencies": task.get("dependencies"),
            "deliverables": task.get("deliverables"),
            "target_files": task.get("target_files"),
            "verification_commands": task.get("verification_commands"),
            "acceptance_criteria": task.get("acceptance_criteria"),
            "notes_for_agent": task.get("notes_for_agent"),
            "dependency_status": task.get("dependency_status"),
            "contract_completeness": task.get("contract_completeness"),
            "raw_issue_body": task.get("raw_issue_body"),
        },
        "source_type": source_type,
        "board_path": str(board_path) if board_path else None,
    }
    if blocked_reason:
        preview["blocked_reason"] = blocked_reason
    if source_type == "kanban_board":
        all_cards = board.get("cards", [])
        lanes = {lane: [] for lane in board.get("board", {}).get("columns", [])}
        for card in all_cards:
            lane = card.get("lane", "backlog")
            lanes.setdefault(lane, []).append(card)
        other_lanes = {k: v for k, v in lanes.items() if k != task.get("lane", "ready")}
        preview["board_summary"] = {k: len(v) for k, v in lanes.items()}
        preview["other_lanes"] = {
            lane: [{"id": c.get("id"), "title": c.get("title")} for c in cards]
            for lane, cards in other_lanes.items()
            if cards
        }
    return preview
