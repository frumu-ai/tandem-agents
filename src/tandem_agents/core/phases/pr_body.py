from __future__ import annotations

import json
import re
from typing import Any

from src.tandem_agents.core.phases.context import RunContext


def build_pull_request_body(ctx: RunContext, final_diff_snapshot: str) -> str:
    task = ctx.task if isinstance(ctx.task, dict) else {}
    blackboard = ctx.blackboard if isinstance(ctx.blackboard, dict) else {}
    manager_plan = _dict_value(ctx.manager_plan) or _dict_value(blackboard.get("manager_plan"))
    repo_validation = _dict_value(ctx.repo_validation) or _dict_value(blackboard.get("repo_validation"))
    workers = ctx.worker_results or _list_of_dicts(blackboard.get("workers"))
    review = _dict_value(ctx.review_result) or _dict_value(blackboard.get("review"))
    test = _dict_value(ctx.test_result) or _dict_value(blackboard.get("test"))
    task_contract = _dict_value(task.get("task_contract"))
    source = _dict_value(task.get("source"))

    lines: list[str] = [
        "## Summary",
        "",
        _summary_text(task, manager_plan, workers),
        "",
        "## Why",
        "",
    ]
    why = _why_text(task, task_contract, manager_plan)
    lines.append(why or "ACA could not find task context beyond the title; review the linked source item for background.")

    changed_files = _changed_files(workers, repo_validation, ctx.expected_repo_files)
    lines.extend(["", "## What Changed", ""])
    change_notes = _change_notes(workers, manager_plan)
    if change_notes:
        lines.extend(f"- {note}" for note in change_notes[:8])
    elif changed_files:
        lines.append("- Updated the files listed below to satisfy the task contract.")
    else:
        lines.append("- ACA did not record a structured change summary.")
    if changed_files:
        lines.extend(["", "Changed files:"])
        lines.extend(f"- `{path}`" for path in changed_files[:20])
        if len(changed_files) > 20:
            lines.append(f"- ...and {len(changed_files) - 20} more")

    acceptance = _acceptance_items(task, task_contract)
    if acceptance:
        lines.extend(["", "## Acceptance Coverage", ""])
        lines.extend(f"- {item}" for item in acceptance[:12])
        if len(acceptance) > 12:
            lines.append(f"- ...and {len(acceptance) - 12} more")

    verification = _verification_lines(repo_validation, review, test)
    lines.extend(["", "## Verification", ""])
    if verification:
        lines.extend(f"- {item}" for item in verification[:12])
    else:
        lines.append("- ACA did not record command-level verification.")

    review_notes = _review_notes(review, test)
    if review_notes:
        lines.extend(["", "## Review Notes", ""])
        lines.extend(f"- {note}" for note in review_notes[:8])

    risks = _risk_lines(manager_plan, task_contract)
    if risks:
        lines.extend(["", "## Known Limitations", ""])
        lines.extend(f"- {risk}" for risk in risks[:8])

    diff_excerpt = _bounded_text(final_diff_snapshot, 1800)
    if diff_excerpt:
        lines.extend(["", "## Diff Snapshot", "", "```text", diff_excerpt, "```"])

    metadata = _metadata_lines(ctx, task, source)
    if metadata:
        lines.extend(["", "## ACA Run Metadata", ""])
        lines.extend(f"- {item}" for item in metadata)

    body = "\n".join(lines).strip()
    return body if body else f"ACA automated PR for task: {_text(task.get('title')) or 'Untitled task'}"


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_text(value: Any, limit: int) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 4)].rstrip() + "\n..."


def _clean_inline(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", text.replace("`", "'")).strip(" -")
    return _bounded_text(text, limit)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _summary_text(task: dict[str, Any], manager_plan: dict[str, Any], workers: list[dict[str, Any]]) -> str:
    summary = _text(manager_plan.get("summary"))
    if summary:
        return _bounded_text(summary, 700)
    for worker in workers:
        title = _text(worker.get("title"))
        if title:
            return f"ACA completed: {title}."
    title = _text(task.get("title"))
    return f"ACA automated PR for task: {title or 'Untitled task'}."


def _markdown_section(markdown: str, heading: str) -> str:
    if not markdown:
        return ""
    pattern = re.compile(
        rf"(?ims)^##+\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##+\s+|\Z)"
    )
    match = pattern.search(markdown)
    if not match:
        return ""
    body = match.group("body").strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return _bounded_text(" ".join(lines), 900)


def _why_text(task: dict[str, Any], task_contract: dict[str, Any], manager_plan: dict[str, Any]) -> str:
    markdown = _text(task.get("description") or task.get("raw_issue_body") or task_contract.get("raw_issue_body"))
    context = _markdown_section(markdown, "Context")
    if context:
        return context
    local_goal = _text(task_contract.get("local_goal") or task.get("local_goal"))
    if local_goal:
        return local_goal
    return _text(manager_plan.get("summary"))


def _changed_files(
    workers: list[dict[str, Any]],
    repo_validation: dict[str, Any],
    expected_repo_files: list[str],
) -> list[str]:
    files: list[str] = []
    for worker in workers:
        raw_files = worker.get("changed_files") or worker.get("files") or []
        if isinstance(raw_files, list):
            files.extend(_text(item) for item in raw_files)
    for key in ("checked_files", "present_files", "expected_files"):
        raw_files = repo_validation.get(key)
        if isinstance(raw_files, list):
            files.extend(_text(item) for item in raw_files)
    files.extend(_text(item) for item in expected_repo_files or [])
    return _unique(files)


def _extract_worker_bullets(output: str) -> list[str]:
    output = re.sub(r"(?s)^.*?Worker completion note:\s*", "", output).strip()
    bullets: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].strip()
        if not body or body.startswith("**") or body.startswith("`"):
            continue
        if body.lower().startswith(("verification", "remaining implementation blockers", "changed files")):
            continue
        bullets.append(_clean_inline(body))
    return [item for item in _unique(bullets) if item]


def _change_notes(workers: list[dict[str, Any]], manager_plan: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for worker in workers:
        title = _clean_inline(_text(worker.get("title")), 180)
        status = _text(worker.get("status"))
        if title:
            notes.append(f"{title}{f' ({status})' if status else ''}.")
        notes.extend(_extract_worker_bullets(_text(worker.get("output_excerpt") or worker.get("stdout"))))
    for subtask in _list_of_dicts(manager_plan.get("subtasks")):
        goal = _clean_inline(_text(subtask.get("goal") or subtask.get("title")), 260)
        if goal:
            notes.append(goal)
    return _unique(notes)


def _acceptance_items(task: dict[str, Any], task_contract: dict[str, Any]) -> list[str]:
    raw = task.get("acceptance_criteria") or task_contract.get("acceptance_criteria") or []
    items = raw if isinstance(raw, list) else []
    return [_clean_inline(_text(item), 260) for item in items if _text(item)]


def _parse_stdout_json(result: dict[str, Any]) -> dict[str, Any]:
    stdout = _text(result.get("stdout"))
    if not stdout:
        return {}
    try:
        payload = json.loads(stdout)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _verification_lines(repo_validation: dict[str, Any], review: dict[str, Any], test: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for check in _list_of_dicts(repo_validation.get("command_checks")):
        command = _text(check.get("command"))
        status = _text(check.get("status")) or ("passed" if check.get("returncode") == 0 else "failed")
        if command:
            lines.append(f"`{command}`: {status}")
    if review:
        rc = review.get("returncode")
        if rc is not None:
            lines.append(f"ACA review pass return code: `{rc}`")
    if test:
        rc = test.get("returncode")
        if rc is not None:
            lines.append(f"ACA test pass return code: `{rc}`")
    test_payload = _parse_stdout_json(test)
    for command in _list_of_dicts(test_payload.get("commands")):
        cmd = _text(command.get("command"))
        result = _text(command.get("result"))
        if cmd and result:
            lines.append(f"`{cmd}`: {result}")
    return _unique(lines)


def _review_notes(review: dict[str, Any], test: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    review_payload = _parse_stdout_json(review)
    for key in ("findings", "notes", "required_fixes"):
        raw_items = review_payload.get(key)
        if isinstance(raw_items, list):
            notes.extend(_clean_inline(_text(item), 320) for item in raw_items if _text(item))
    test_payload = _parse_stdout_json(test)
    results = test_payload.get("results")
    if isinstance(results, dict):
        for key, value in results.items():
            notes.append(f"{_clean_inline(str(key), 80)}: {_clean_inline(_text(value), 280)}")
    return _unique([note for note in notes if note])


def _risk_lines(manager_plan: dict[str, Any], task_contract: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    raw_risks = manager_plan.get("risks")
    if isinstance(raw_risks, list):
        for item in raw_risks:
            if isinstance(item, dict):
                risk = _clean_inline(_text(item.get("risk")), 260)
                mitigation = _clean_inline(_text(item.get("mitigation")), 260)
                if risk and mitigation:
                    risks.append(f"{risk} Mitigation: {mitigation}")
                elif risk:
                    risks.append(risk)
            else:
                risk = _clean_inline(_text(item), 260)
                if risk:
                    risks.append(risk)
    verification = task_contract.get("verification_commands")
    if isinstance(verification, list):
        for item in verification:
            text = _clean_inline(_text(item), 260)
            if text and not text.startswith(("cargo ", "pnpm ", "npm ", "python", "pytest", "uv ")):
                risks.append(f"Task verification note: {text}")
    return _unique(risks)


def _metadata_lines(ctx: RunContext, task: dict[str, Any], source: dict[str, Any]) -> list[str]:
    lines = [f"ACA run: `{ctx.run_id}`", f"Branch: `{ctx.branch_name}`"]
    task_id = _text(task.get("task_id") or source.get("identifier") or source.get("item"))
    if task_id:
        lines.append(f"Task: `{task_id}`")
    source_url = _text(source.get("url") or source.get("issue_url"))
    if source_url:
        lines.append(f"Source: {source_url}")
    return lines
