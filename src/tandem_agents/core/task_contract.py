from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Iterable, Mapping


_HEADING_RE = re.compile(r"^\s{0,3}#{2,6}\s+(.*?)\s*$")
_LIST_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+(.*\S)\s*$")
_CHECKBOX_RE = re.compile(r"^(?:[-*]|\d+[.)])\s*\[(?: |x|X)\]\s+(.*\S)\s*$")
_ABSOLUTE_PATH_RE = re.compile(r"(^|[\s\"'`])/[^\s\"'`]+")
_TANDEM_CODER_HANDOFF_RE = re.compile(
    r"<!--\s*tandem:coder_handoff:v1\s*(.*?)\s*-->",
    re.DOTALL,
)

_SECTION_ALIASES = {
    "program_context": "program_goal",
    "local_goal": "local_goal",
    "scope": "in_scope",
    "out_of_scope": "out_of_scope",
    "dependencies": "dependencies",
    "deliverables": "deliverables",
    "target_files": "target_files",
    "files_likely_involved": "target_files",
    "files_likely_touched": "target_files",
    "likely_files": "target_files",
    "likely_files_to_edit": "target_files",
    "verification": "verification_commands",
    "verification_steps": "verification_commands",
    "acceptance": "acceptance_criteria",
    "acceptance_criterion": "acceptance_criteria",
    "acceptance_criteria": "acceptance_criteria",
    "notes_for_agent": "notes_for_agent",
    "recommended_fix": "notes_for_agent",
    "suspected_root_cause": "notes_for_agent",
    "subtasks": "subtasks",
}


def _normalize_heading(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_text_list(value: Any) -> list[str]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Mapping):
        values = list(value.values())
    elif isinstance(value, Iterable):
        values = list(value)
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        text = _normalize_text(item)
        if text:
            result.append(text)
    return result


def _normalize_repo_relative_path(value: Any) -> str:
    text = _normalize_text(value).replace("\\", "/")
    backtick_match = re.search(r"`([^`]+)`", text)
    if backtick_match:
        text = backtick_match.group(1)
    else:
        path_match = re.search(r"([A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)+)", text)
        if path_match:
            text = path_match.group(1)
    text = text.strip().strip("`'\"")
    while text.startswith("./"):
        text = text[2:]
    return text


def _merge_unique(existing: Iterable[Any] | None, additional: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(additional or []):
        text = _normalize_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _extract_tandem_coder_handoff(body: str | None) -> dict[str, Any] | None:
    if not body:
        return None
    match = _TANDEM_CODER_HANDOFF_RE.search(body)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("handoff_type") or "").strip() != "tandem_autonomous_coder_issue":
        return None
    return parsed


def _handoff_notes(handoff: Mapping[str, Any]) -> str:
    rows = []
    for label, key in (
        ("Bug Monitor triage run", "triage_run_id"),
        ("Bug Monitor incident", "incident_id"),
        ("Bug Monitor draft", "draft_id"),
        ("Failure type", "failure_type"),
        ("Risk level", "risk_level"),
    ):
        value = _normalize_text(handoff.get(key))
        if value:
            rows.append(f"{label}: {value}")
    coder_ready = handoff.get("coder_ready")
    if coder_ready is not None:
        rows.append(f"Bug Monitor coder-ready signal: {bool(coder_ready)}")
    missing_scopes = _coerce_text_list(handoff.get("missing_tool_scopes"))
    if missing_scopes:
        rows.append("Missing tool scopes: " + ", ".join(missing_scopes))
    return "\n".join(rows)


def _split_sections(body: str | None) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"__preamble__": []}
    current = "__preamble__"
    if not body:
        return sections
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\n")
        heading = _HEADING_RE.match(line)
        if heading:
            current = _SECTION_ALIASES.get(_normalize_heading(heading.group(1)), _normalize_heading(heading.group(1)))
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _section_text(lines: Iterable[str] | None) -> str:
    if not lines:
        return ""
    parts = [line.strip() for line in lines if line.strip() and not line.strip().startswith("```")]
    return "\n".join(parts).strip()


def _section_items(lines: Iterable[str] | None) -> list[str]:
    if not lines:
        return []
    items: list[str] = []
    in_fence = False
    for raw_line in lines:
        line = str(raw_line or "").rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            items.append(stripped)
            continue
        checkbox = _CHECKBOX_RE.match(stripped)
        if checkbox:
            items.append(checkbox.group(1).strip())
            continue
        bullet = _LIST_RE.match(stripped)
        if bullet:
            items.append(bullet.group(1).strip())
            continue
        items.append(stripped)
    return [item for item in (entry.strip() for entry in items) if item]


def _fallback_acceptance_criteria(body: str | None) -> list[str]:
    if not body:
        return []
    lines = [line.strip() for line in body.splitlines()]
    criteria: list[str] = []
    for line in lines:
        if not line:
            continue
        match = _CHECKBOX_RE.match(line)
        if match:
            criteria.append(match.group(1).strip())
            continue
        if line.startswith(("- [ ]", "* [ ]", "- [x]", "* [x]", "- [X]", "* [X]")):
            criteria.append(line.split("]", 1)[-1].strip())
            continue
    if criteria:
        return [entry for entry in criteria if entry]
    criteria = [
        line.lstrip("-* ").strip()
        for line in lines
        if line.startswith("- ") or line.startswith("* ")
    ]
    return [entry for entry in criteria if entry]


def _parse_explicit_subtasks(lines: Iterable[str] | None) -> list[dict[str, Any]]:
    items = _section_items(lines)
    subtasks: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        title = item.strip()
        if not title:
            continue
        subtasks.append(
            {
                "id": f"subtask-{index}",
                "title": title,
                "goal": title,
                "description": title,
                "acceptance_criteria": [],
            }
        )
    return subtasks


def parse_task_contract(
    body: str | None,
    *,
    fallback_acceptance_criteria: Iterable[str] | None = None,
) -> dict[str, Any]:
    sections = _split_sections(body)
    has_structured_sections = any(
        key in sections
        for key in (
            "program_goal",
            "local_goal",
            "in_scope",
            "out_of_scope",
            "dependencies",
            "deliverables",
            "target_files",
            "verification_commands",
            "acceptance_criteria",
            "notes_for_agent",
            "subtasks",
        )
    )
    program_goal = _section_text(sections.get("program_goal"))
    local_goal = _section_text(sections.get("local_goal"))
    in_scope = _section_items(sections.get("in_scope"))
    out_of_scope = _section_items(sections.get("out_of_scope"))
    dependencies = _section_items(sections.get("dependencies"))
    deliverables = _section_items(sections.get("deliverables"))
    target_files = [_normalize_repo_relative_path(entry) for entry in _section_items(sections.get("target_files"))]
    verification_commands = _section_items(sections.get("verification_commands"))
    acceptance_criteria = _section_items(sections.get("acceptance_criteria"))
    if not acceptance_criteria:
        acceptance_criteria = [str(entry).strip() for entry in (fallback_acceptance_criteria or []) if str(entry).strip()]
    if not acceptance_criteria and not has_structured_sections:
        acceptance_criteria = _fallback_acceptance_criteria(body)
    notes_for_agent = _section_text(sections.get("notes_for_agent"))
    subtasks = _parse_explicit_subtasks(sections.get("subtasks"))
    raw_issue_body = (body or "").strip()
    tandem_coder_handoff = _extract_tandem_coder_handoff(raw_issue_body)
    if tandem_coder_handoff:
        target_files = _merge_unique(
            target_files,
            [
                _normalize_repo_relative_path(entry)
                for entry in _coerce_text_list(tandem_coder_handoff.get("likely_files_to_edit"))
            ],
        )
        verification_commands = _merge_unique(
            verification_commands,
            _coerce_text_list(tandem_coder_handoff.get("verification_steps")),
        )
        acceptance_criteria = _merge_unique(
            acceptance_criteria,
            _coerce_text_list(tandem_coder_handoff.get("acceptance_criteria")),
        )
        handoff_notes = _handoff_notes(tandem_coder_handoff)
        if handoff_notes:
            notes_for_agent = "\n".join(
                row for row in (notes_for_agent, handoff_notes) if row.strip()
            )
    return {
        "program_goal": program_goal,
        "local_goal": local_goal,
        "in_scope": in_scope,
        "out_of_scope": out_of_scope,
        "dependencies": dependencies,
        "deliverables": deliverables,
        "target_files": target_files,
        "verification_commands": verification_commands,
        "acceptance_criteria": acceptance_criteria,
        "notes_for_agent": notes_for_agent,
        "subtasks": subtasks,
        "raw_issue_body": raw_issue_body,
        "has_structured_sections": has_structured_sections,
        "tandem_coder_handoff": tandem_coder_handoff,
    }


def task_contract_payload(task: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        return {}
    payload = task.get("task_contract")
    if isinstance(payload, dict) and payload:
        return deepcopy(payload)
    body = _normalize_text(task.get("raw_issue_body") or task.get("description") or task.get("body"))
    parsed = parse_task_contract(body, fallback_acceptance_criteria=task.get("acceptance_criteria") or [])
    if not parsed["local_goal"]:
        title = _normalize_text(task.get("title"))
        if title:
            parsed["local_goal"] = title
    for field in (
        "program_goal",
        "local_goal",
        "in_scope",
        "out_of_scope",
        "dependencies",
        "deliverables",
        "target_files",
        "verification_commands",
        "acceptance_criteria",
        "notes_for_agent",
        "subtasks",
        "tandem_coder_handoff",
    ):
        direct_value = task.get(field)
        if direct_value in (None, "", [], (), {}):
            continue
        if field == "subtasks":
            if isinstance(direct_value, Iterable) and not isinstance(direct_value, (str, bytes, Mapping)):
                parsed[field] = deepcopy(list(direct_value))
            else:
                parsed[field] = []
        elif field in {"in_scope", "out_of_scope", "dependencies", "deliverables", "target_files", "verification_commands", "acceptance_criteria"}:
            direct_values = _coerce_text_list(direct_value)
            if direct_values:
                parsed[field] = direct_values
        elif field == "notes_for_agent":
            parsed[field] = _normalize_text(direct_value)
        elif field == "tandem_coder_handoff":
            parsed[field] = (
                deepcopy(dict(direct_value)) if isinstance(direct_value, Mapping) else None
            )
        else:
            parsed[field] = _normalize_text(direct_value)
    return parsed


def apply_task_contract(task: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        return {}
    result = deepcopy(dict(task))
    source = deepcopy(dict(result.get("source") or {}))
    repo = deepcopy(dict(result.get("repo") or {}))
    contract = task_contract_payload(result)
    title = _normalize_text(result.get("title")) or "Untitled task"
    description = _normalize_text(result.get("description") or result.get("body") or contract.get("raw_issue_body"))
    if contract.get("raw_issue_body") and not description:
        description = str(contract["raw_issue_body"]).strip()
    if not contract.get("local_goal"):
        contract["local_goal"] = title
    result["title"] = title
    result["description"] = description
    result["raw_issue_body"] = contract.get("raw_issue_body") or description
    result["program_goal"] = contract.get("program_goal") or None
    result["local_goal"] = contract.get("local_goal") or None
    result["in_scope"] = list(contract.get("in_scope") or [])
    result["out_of_scope"] = list(contract.get("out_of_scope") or [])
    result["dependencies"] = list(contract.get("dependencies") or [])
    result["deliverables"] = list(contract.get("deliverables") or [])
    result["target_files"] = list(contract.get("target_files") or [])
    result["verification_commands"] = list(contract.get("verification_commands") or [])
    result["acceptance_criteria"] = list(contract.get("acceptance_criteria") or [])
    result["notes_for_agent"] = contract.get("notes_for_agent") or None
    result["subtasks"] = list(contract.get("subtasks") or [])
    result["tandem_coder_handoff"] = contract.get("tandem_coder_handoff") or None
    result["task_contract"] = contract
    result["source"] = source
    result["repo"] = repo
    result["execution_kind"] = classify_task_execution_kind(result)
    return result


def classify_task_execution_kind(task: Mapping[str, Any] | None) -> str:
    """Classify what ACA should actually do for a task.

    Most tasks are repository code edits. Some Linear issues are operational
    queue work where the requested artifact is a Linear note/comment rather
    than a git diff. Those must not be forced through the repo worker path.
    """
    if not isinstance(task, Mapping):
        return "code_edit"
    contract = task_contract_payload(task)
    source = dict(task.get("source") or {})
    source_type = _normalize_text(source.get("type")).lower()
    target_files = _coerce_text_list(contract.get("target_files") or task.get("target_files"))
    verification_commands = _coerce_text_list(
        contract.get("verification_commands") or task.get("verification_commands")
    )
    if target_files or verification_commands or contract.get("tandem_coder_handoff"):
        return "code_edit"
    text = "\n".join(
        [
            _normalize_text(task.get("title")),
            _normalize_text(task.get("description")),
            "\n".join(_coerce_text_list(contract.get("acceptance_criteria"))),
            _normalize_text(contract.get("notes_for_agent")),
        ]
    ).lower()
    if source_type == "linear":
        github_pr_action_markers = (
            "close duplicate",
            "close clear duplicates",
            "close stale",
            "close/supersede",
            "github comment",
            "re-check whether any candidate has become mergeable",
            "do not close #",
        )
        if any(marker in text for marker in github_pr_action_markers):
            return "github_pr_action"
        linear_note_markers = (
            "posted in this linear issue",
            "post decision notes",
            "decision notes are posted",
            "linked follow-up comments",
            "record close/merge decision",
            "record close merge decision",
            "record decision per pr",
            "inventory",
        )
        repo_edit_markers = (
            "implement",
            "fix bug",
            "add ",
            "build ",
            "create ",
            "update code",
            "refactor",
            "test ",
        )
        if any(marker in text for marker in linear_note_markers) and not any(
            marker in text for marker in repo_edit_markers
        ):
            return "linear_comment"
    return "code_edit"


def _normalized_status(value: Any) -> str:
    return _normalize_text(value).lower()


def _task_is_done(task: Mapping[str, Any] | None) -> bool:
    if not isinstance(task, Mapping):
        return False
    state = _normalized_status(task.get("state") or task.get("status"))
    return state in {"done", "completed"}


def _task_reference_tokens(task: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(task, Mapping):
        return set()
    source = dict(task.get("source") or {})
    metadata = dict(task.get("metadata") or {})
    source_task = dict(metadata.get("task") or {})
    source_source = dict(source_task.get("source") or {})
    tokens = {
        _normalize_text(task.get("task_key")).lower(),
        _normalize_text(task.get("task_id")).lower(),
        _normalize_text(task.get("source_ref")).lower(),
        _normalize_text(task.get("task_key")).lower().replace(":", "/"),
        _normalize_text(source.get("card_id")).lower(),
        _normalize_text(source.get("item")).lower(),
        _normalize_text(source.get("project_item_id")).lower(),
        _normalize_text(source.get("issue_number")).lower(),
        _normalize_text(source.get("url")).lower(),
        _normalize_text(source.get("item_url")).lower(),
        _normalize_text(source.get("issue_url")).lower(),
        _normalize_text(source_source.get("card_id")).lower(),
        _normalize_text(source_source.get("item")).lower(),
        _normalize_text(source_source.get("project_item_id")).lower(),
        _normalize_text(source_source.get("issue_number")).lower(),
    }
    cleaned: set[str] = set()
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        cleaned.add(token)
        if token.startswith("#") and len(token) > 1:
            cleaned.add(token[1:])
    return cleaned


def _normalize_dependency_token(value: Any) -> str:
    text = _normalize_text(value).lower()
    text = text.lstrip("#")
    return text


def dependency_status_for_task(
    task: Mapping[str, Any] | None,
    known_tasks: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    contract = task_contract_payload(task)
    declared = [entry for entry in (contract.get("dependencies") or []) if _normalize_text(entry)]
    if not declared:
        return {
            "declared": [],
            "resolved": [],
            "unresolved": [],
            "blocked": False,
            "satisfied": True,
            "unknown": False,
            "blocked_reason": None,
        }
    if known_tasks is None:
        return {
            "declared": declared,
            "resolved": [],
            "unresolved": [],
            "blocked": False,
            "satisfied": None,
            "unknown": True,
            "blocked_reason": None,
        }
    lookup: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in known_tasks:
        if not isinstance(candidate, Mapping):
            continue
        for token in _task_reference_tokens(candidate):
            lookup.setdefault(_normalize_dependency_token(token), []).append(candidate)

    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for dependency in declared:
        token = _normalize_dependency_token(dependency)
        matches = lookup.get(token, [])
        done_match = next((match for match in matches if _task_is_done(match)), None)
        if done_match is not None:
            resolved.append(
                {
                    "dependency": dependency,
                    "task_key": done_match.get("task_key"),
                    "task_id": done_match.get("task_id"),
                    "status": _normalized_status(done_match.get("status") or done_match.get("state")),
                }
            )
            continue
        if matches:
            unresolved.append(
                {
                    "dependency": dependency,
                    "matched_task_keys": [match.get("task_key") for match in matches if match.get("task_key")],
                    "matched_task_ids": [match.get("task_id") for match in matches if match.get("task_id")],
                    "matched_statuses": [
                        _normalized_status(match.get("status") or match.get("state"))
                        for match in matches
                        if match.get("status") or match.get("state")
                    ],
                    "reason": "not_done",
                }
            )
        else:
            unresolved.append(
                {
                    "dependency": dependency,
                    "matched_task_keys": [],
                    "matched_task_ids": [],
                    "matched_statuses": [],
                    "reason": "missing",
                }
            )
    blocked = bool(unresolved)
    return {
        "declared": declared,
        "resolved": resolved,
        "unresolved": unresolved,
        "blocked": blocked,
        "satisfied": not blocked,
        "unknown": False,
        "blocked_reason": f"blocked by {', '.join(entry['dependency'] for entry in unresolved)}" if blocked else None,
    }


def task_contract_completeness(task: Mapping[str, Any] | None) -> dict[str, Any]:
    contract = task_contract_payload(task)
    title = _normalize_text(task.get("title") if isinstance(task, Mapping) else "")
    description = _normalize_text(task.get("description") if isinstance(task, Mapping) else "")
    has_acceptance = bool(contract.get("acceptance_criteria"))
    has_targets = bool(contract.get("target_files"))
    has_deliverables = bool(contract.get("deliverables"))
    has_verification = bool(contract.get("verification_commands"))
    has_subtasks = bool(contract.get("subtasks"))
    has_goal = bool(contract.get("program_goal") or contract.get("local_goal") or title or description)
    issue_codes: list[str] = []
    if not has_goal:
        issue_codes.append("missing_goal")
    if not (has_acceptance or has_targets or has_deliverables or has_verification or has_subtasks):
        issue_codes.append("missing_execution_contract")
    return {
        "ok": not issue_codes,
        "issues": issue_codes,
        "has_goal": has_goal,
        "has_acceptance_criteria": has_acceptance,
        "has_target_files": has_targets,
        "has_deliverables": has_deliverables,
        "has_verification_commands": has_verification,
        "has_subtasks": has_subtasks,
        "blocker_kind": "contract_incomplete" if issue_codes else None,
        "blocker_message": (
            "Task contract is incomplete: "
            + ", ".join(issue_codes)
            if issue_codes
            else None
        ),
    }


def task_plan_validation(task: Mapping[str, Any] | None, subtasks: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    contract = task_contract_payload(task)
    out_of_scope = {_normalize_repo_relative_path(entry) for entry in contract.get("out_of_scope") or [] if _normalize_text(entry)}
    task_requires_changes = bool(contract.get("target_files") or contract.get("deliverables") or contract.get("verification_commands"))
    issues: list[dict[str, Any]] = []
    for index, subtask in enumerate(subtasks or [], start=1):
        if not isinstance(subtask, Mapping):
            continue
        title = _normalize_text(subtask.get("title")) or f"Subtask {index}"
        goal = _normalize_text(subtask.get("goal") or subtask.get("description") or title)
        files = [
            _normalize_text(entry)
            for entry in list(subtask.get("files") or [])
            if _normalize_text(entry)
        ]
        acceptance = _coerce_text_list(
            subtask.get("acceptance_criteria")
            or subtask.get("acceptance")
            or subtask.get("acceptance_checklist")
            or subtask.get("validation")
            or subtask.get("required_work")
            or subtask.get("scope")
            or subtask.get("objective")
            or subtask.get("verification")
            or subtask.get("expected_verification")
            or subtask.get("instructions")
            or subtask.get("handoff")
            or subtask.get("deliverables")
            or subtask.get("deliverable")
        )
        if not goal:
            issues.append({"kind": "missing_goal", "subtask": title, "detail": "Subtask goal is empty."})
        if task_requires_changes and not acceptance:
            issues.append(
                {
                    "kind": "missing_acceptance_criteria",
                    "subtask": title,
                    "detail": "Nontrivial task requires acceptance criteria or checklist items.",
                }
            )
        absolute_files = [path for path in files if path.startswith("/")]
        if absolute_files:
            issues.append(
                {
                    "kind": "absolute_file_path",
                    "subtask": title,
                    "detail": ", ".join(absolute_files),
                }
            )
        conflict = next(
            (
                file_path
                for file_path in files
                if _normalize_repo_relative_path(file_path) in out_of_scope
                or any(
                    _normalize_repo_relative_path(file_path).startswith(scope + "/")
                    for scope in out_of_scope
                    if scope
                )
            ),
            None,
        )
        if conflict:
            issues.append(
                {
                    "kind": "out_of_scope_conflict",
                    "subtask": title,
                    "detail": conflict,
                }
            )
    blocker_kind = "contract_incomplete" if issues else None
    blocker_message = None
    if issues:
        blocker_message = "Subtask plan is incomplete or unsafe: " + "; ".join(
            f"{issue['kind']} ({issue['subtask']})" for issue in issues
        )
    return {
        "ok": not issues,
        "issues": issues,
        "blocker_kind": blocker_kind,
        "blocker_message": blocker_message,
    }
