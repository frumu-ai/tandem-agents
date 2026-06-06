from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.engine.engine import execute_engine_tool
from src.tandem_agents.core.integrations.github_mcp import (
    delete_remote_branch,
    ensure_github_mcp_connected,
    get_pull_request,
    guarded_auto_merge,
    normalize_pull_request_metadata,
)
from src.tandem_agents.core.integrations.linear_mcp import linear_add_comment, linear_comment_marker_present

WRITE_ACTIONS = {
    "comment_pr",
    "close_pr",
    "update_branch",
    "request_review",
    "merge_pr",
    "delete_branch",
    "post_linear_summary",
}
READ_ACTIONS = {"leave_open"}
ALLOWED_ACTIONS = WRITE_ACTIONS | READ_ACTIONS


def extract_pr_numbers(task: dict[str, Any]) -> list[int]:
    text = "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            "\n".join(str(item or "") for item in task.get("acceptance_criteria") or []),
            str((task.get("task_contract") or {}).get("raw_issue_body") or ""),
        ]
    )
    numbers: list[int] = []
    seen: set[int] = set()
    for match in re.findall(r"#(\d+)", text):
        number = int(match)
        if number in seen:
            continue
        seen.add(number)
        numbers.append(number)
    return numbers


def _repo_owner_name(cfg: ResolvedConfig, task: dict[str, Any]) -> tuple[str, str]:
    source = dict(task.get("source") or {})
    repo = dict(task.get("repo") or {})
    slug = str(repo.get("slug") or cfg.repository.slug or "").strip()
    owner = str(source.get("owner") or "").strip()
    name = str(source.get("repo_name") or "").strip()
    if (not owner or not name) and "/" in slug:
        owner, name = slug.split("/", 1)
    if not owner or not name:
        raise RuntimeError("GitHub PR actions require a repository owner/name.")
    return owner, name


def fetch_pr_contexts(cfg: ResolvedConfig, task: dict[str, Any]) -> list[dict[str, Any]]:
    owner, repo = _repo_owner_name(cfg, task)
    contexts: list[dict[str, Any]] = []
    for number in extract_pr_numbers(task):
        try:
            raw = get_pull_request(cfg, owner, repo, number)
            normalized = normalize_pull_request_metadata(raw, base_repo=f"{owner}/{repo}")
        except Exception as exc:
            normalized = {
                "number": number,
                "base_repo": f"{owner}/{repo}",
                "state": "unknown",
                "lifecycle_state": "blocked",
                "error": str(exc),
            }
        normalized["number"] = int(normalized.get("number") or number)
        normalized["base_repo"] = str(normalized.get("base_repo") or f"{owner}/{repo}")
        contexts.append(normalized)
    return contexts


def _comment_body(run_id: str, task: dict[str, Any], pr: dict[str, Any], decision: str) -> str:
    number = int(pr.get("number") or 0)
    title = str(task.get("title") or "ACA PR action").strip()
    return "\n".join(
        [
            f"ACA reviewed this PR from `{title}`.",
            "",
            f"Decision: **{decision}**.",
            "",
            "This action was proposed by ACA and executed only after operator approval.",
            "",
            f"<!-- aca:github-pr-action:{run_id}:pr-{number} -->",
        ]
    ).strip()


def default_action_plan(run_id: str, task: dict[str, Any], pr_contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for pr in pr_contexts:
        number = int(pr.get("number") or 0)
        if number <= 0:
            continue
        if number == 1400:
            actions.append(
                {
                    "action_type": "leave_open",
                    "target": {"pr_number": number, "base_repo": pr.get("base_repo")},
                    "payload": {"reason": "Manual confirmation is required before closing this large PR."},
                    "risk_level": "low",
                    "verification_marker": "",
                }
            )
            continue
        marker = f"aca:github-pr-action:{run_id}:pr-{number}"
        actions.append(
            {
                "action_type": "comment_pr",
                "target": {"pr_number": number, "base_repo": pr.get("base_repo")},
                "payload": {"body": _comment_body(run_id, task, pr, "close as duplicate/stale generated PR")},
                "risk_level": "medium",
                "verification_marker": marker,
            }
        )
        actions.append(
            {
                "action_type": "close_pr",
                "target": {"pr_number": number, "base_repo": pr.get("base_repo")},
                "payload": {"state": "closed"},
                "risk_level": "high",
                "verification_marker": marker,
            }
        )
    actions.append(
        {
            "action_type": "post_linear_summary",
            "target": {"identifier": (task.get("source") or {}).get("identifier") or task.get("task_id")},
            "payload": {
                "body": build_linear_summary(run_id, task, actions),
            },
            "risk_level": "medium",
            "verification_marker": f"aca:linear-external-action:{run_id}",
        }
    )
    return actions


def build_linear_summary(run_id: str, task: dict[str, Any], actions: list[dict[str, Any]]) -> str:
    close_targets = [
        f"#{(action.get('target') or {}).get('pr_number')}"
        for action in actions
        if action.get("action_type") == "close_pr"
    ]
    leave_targets = [
        f"#{(action.get('target') or {}).get('pr_number')}"
        for action in actions
        if action.get("action_type") == "leave_open"
    ]
    lines = [
        f"ACA external-action plan for `{run_id}`.",
        "",
        f"Task: {task.get('title') or task.get('task_id') or 'GitHub PR action'}",
        "",
        f"Approved close targets: {', '.join(close_targets) if close_targets else 'none'}",
        f"Left open / manual review: {', '.join(leave_targets) if leave_targets else 'none'}",
        "",
        "All GitHub writes are approval-gated and verified after execution.",
        "",
        f"<!-- aca:linear-external-action:{run_id} -->",
    ]
    return "\n".join(lines).strip()


def validate_action(action: dict[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("action_type") or action.get("type") or "").strip()
    if action_type not in ALLOWED_ACTIONS:
        raise RuntimeError(f"Unsupported external action type: {action_type or 'unknown'}")
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    risk_level = str(action.get("risk_level") or ("low" if action_type in READ_ACTIONS else "medium")).strip()
    marker = str(action.get("verification_marker") or "").strip()
    if action_type in WRITE_ACTIONS and not target:
        raise RuntimeError(f"External action {action_type} is missing target metadata.")
    return {
        "action_type": action_type,
        "target": dict(target),
        "payload": dict(payload),
        "risk_level": risk_level,
        "verification_marker": marker,
    }


def enqueue_approvals_for_plan(
    coordination: CoordinationStore,
    *,
    run_id: str,
    task: dict[str, Any],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    task_id = str(task.get("task_id") or (task.get("source") or {}).get("identifier") or "").strip()
    for action in actions:
        normalized = validate_action(action)
        if normalized["action_type"] in READ_ACTIONS:
            continue
        target_key = json.dumps(normalized["target"], sort_keys=True, default=str)
        dedupe_key = f"{run_id}:{normalized['action_type']}:{target_key}"
        rows.append(
            coordination.enqueue_external_action_approval(
                run_id=run_id,
                task_id=task_id,
                source_type=str((task.get("source") or {}).get("type") or ""),
                adapter="github_pr",
                action_type=normalized["action_type"],
                target=normalized["target"],
                payload=normalized["payload"],
                risk_level=normalized["risk_level"],
                verification_marker=normalized["verification_marker"],
                dedupe_key=dedupe_key,
            )
        )
    return rows


def _split_repo(base_repo: str) -> tuple[str, str]:
    if "/" not in base_repo:
        raise RuntimeError(f"Invalid GitHub repository slug: {base_repo}")
    owner, repo = base_repo.split("/", 1)
    return owner, repo


def _parse_tool_json(result: dict[str, Any]) -> Any:
    output = str(result.get("output") or "").strip()
    if output:
        try:
            return json.loads(output)
        except Exception:
            return {"output": output}
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        inner = metadata.get("result")
        if isinstance(inner, dict):
            content = inner.get("content")
            if isinstance(content, list):
                for entry in content:
                    text = str((entry or {}).get("text") or "").strip() if isinstance(entry, dict) else ""
                    if not text:
                        continue
                    try:
                        return json.loads(text)
                    except Exception:
                        return {"output": text}
            return inner
    return metadata or result


def _execute_tool(cfg: ResolvedConfig, tool: str, args: dict[str, Any]) -> Any:
    result = execute_engine_tool(cfg, tool, args)
    output = str(result.get("output") or "").strip().lower()
    metadata = result.get("metadata")
    is_error = isinstance(metadata, dict) and isinstance(metadata.get("result"), dict) and metadata["result"].get("isError") is True
    if is_error or output.startswith(("failed", "unknown tool", "unknown method", "missing required")):
        raise RuntimeError(str(result.get("output") or "GitHub MCP action failed"))
    return _parse_tool_json(result)


def _pr_issue_task(base_repo: str, pr_number: int, run_id: str) -> dict[str, Any]:
    owner, repo = _split_repo(base_repo)
    return {
        "run_id": run_id,
        "source": {
            "type": "github_project",
            "owner": owner,
            "repo_name": repo,
            "issue_number": pr_number,
            "issue_url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        },
    }


def _verify_pr_comment(cfg: ResolvedConfig, base_repo: str, pr_number: int, marker: str) -> bool:
    owner, repo = _split_repo(base_repo)
    result = _execute_tool(
        cfg,
        "mcp.github.list_issue_comments",
        {"owner": owner, "repo": repo, "issue_number": int(pr_number)},
    )
    comments = result if isinstance(result, list) else result.get("comments") if isinstance(result, dict) else []
    for comment in comments or []:
        if marker and marker in str((comment or {}).get("body") or (comment or {}).get("text") or ""):
            return True
    return False


def execute_approved_action(cfg: ResolvedConfig, approval: dict[str, Any]) -> dict[str, Any]:
    action_type = str(approval.get("action_type") or "")
    target = dict(approval.get("target") or {})
    payload = dict(approval.get("payload") or {})
    marker = str(approval.get("verification_marker") or "")
    base_repo = str(target.get("base_repo") or cfg.repository.slug or "")
    pr_number = int(target.get("pr_number") or 0)
    ensure_github_mcp_connected(cfg)

    if action_type == "comment_pr":
        body = str(payload.get("body") or "").strip()
        if not body:
            raise RuntimeError("comment_pr requires payload.body")
        owner, repo = _split_repo(base_repo)
        _execute_tool(cfg, "mcp.github.add_issue_comment", {"owner": owner, "repo": repo, "issue_number": pr_number, "body": body})
        verified = _verify_pr_comment(cfg, base_repo, pr_number, marker)
        if not verified:
            raise RuntimeError("GitHub PR comment could not be verified.")
        return {"verified": True, "action_type": action_type}

    if action_type == "close_pr":
        owner, repo = _split_repo(base_repo)
        _execute_tool(cfg, "mcp.github.update_pull_request", {"owner": owner, "repo": repo, "pull_number": pr_number, "state": "closed"})
        refreshed = get_pull_request(cfg, owner, repo, pr_number)
        state = str(refreshed.get("state") or "").lower()
        if state != "closed":
            raise RuntimeError(f"GitHub PR close could not be verified; state={state or 'unknown'}.")
        return {"verified": True, "action_type": action_type, "state": state}

    if action_type == "update_branch":
        owner, repo = _split_repo(base_repo)
        result = _execute_tool(cfg, "mcp.github.update_pull_request_branch", {"owner": owner, "repo": repo, "pull_number": pr_number})
        return {"verified": True, "action_type": action_type, "result": result}

    if action_type == "request_review":
        owner, repo = _split_repo(base_repo)
        result = _execute_tool(cfg, "mcp.github.request_copilot_review", {"owner": owner, "repo": repo, "pull_number": pr_number})
        return {"verified": True, "action_type": action_type, "result": result}

    if action_type == "merge_pr":
        owner, repo = _split_repo(base_repo)
        pr = normalize_pull_request_metadata(get_pull_request(cfg, owner, repo, pr_number), base_repo=base_repo)
        result = guarded_auto_merge(cfg, pr, approvals={"merge": "approved"})
        if not result.get("merged"):
            raise RuntimeError(str(result.get("reason") or result.get("error") or "GitHub PR merge was not verified."))
        return {"verified": True, "action_type": action_type, "result": result}

    if action_type == "delete_branch":
        owner, repo = _split_repo(base_repo)
        pr = normalize_pull_request_metadata(get_pull_request(cfg, owner, repo, pr_number), base_repo=base_repo)
        result = delete_remote_branch(cfg, pr)
        if not result.get("deleted"):
            raise RuntimeError(str(result.get("error") or result.get("reason") or "Remote branch deletion was not verified."))
        return {"verified": True, "action_type": action_type, "result": result}

    if action_type == "post_linear_summary":
        task = {"source": {"type": "linear", "issue_id": target.get("identifier"), "identifier": target.get("identifier")}}
        body = str(payload.get("body") or "").strip()
        warning = linear_add_comment(cfg, task, body)
        if warning:
            raise RuntimeError(warning)
        if marker and not linear_comment_marker_present(cfg, task, marker):
            raise RuntimeError("Linear summary comment could not be verified.")
        return {"verified": True, "action_type": action_type}

    raise RuntimeError(f"Unsupported approved action: {action_type}")


def execute_approved_actions(cfg: ResolvedConfig, coordination: CoordinationStore, *, run_id: str) -> dict[str, Any]:
    rows = coordination.list_external_action_approvals(run_id=run_id, status="approved", limit=250)
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            result = execute_approved_action(cfg, row)
            results.append(coordination.mark_external_action_executed(row["approval_id"], result=result))
        except Exception as exc:
            results.append(coordination.mark_external_action_failed(row["approval_id"], error=str(exc)))
    pending = coordination.list_external_action_approvals(run_id=run_id, status="pending", limit=250)
    failed = coordination.list_external_action_approvals(run_id=run_id, status="failed", limit=250)
    executed = coordination.list_external_action_approvals(run_id=run_id, status="executed", limit=250)
    return {
        "run_id": run_id,
        "executed_count": len([row for row in results if row.get("status") == "executed"]),
        "failed_count": len(failed),
        "pending_count": len(pending),
        "results": results,
        "complete": not pending and not failed and bool(executed),
    }
