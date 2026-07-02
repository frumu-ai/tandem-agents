from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.budget import budget_status, load_issue_spend, record_coder_spend
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.engine.coder_backend import build_coder_summary
from src.tandem_agents.core.engine.tandem_client_sdk import (
    sdk_coder_cancel_run,
    sdk_coder_create_run,
    sdk_coder_execute_all,
    sdk_coder_get_run,
)
from src.tandem_agents.core.integrations.github_mcp import (
    build_issue_comment_body,
    build_pull_request_repair_prompt,
    collect_pull_request_repair_context,
    github_mcp_scope,
    github_project_status_name_for_outcome,
    github_remote_sync_mode,
    guarded_auto_merge,
    refresh_pull_request_lifecycle,
    update_pull_request_branch,
)
from src.tandem_agents.core.integrations.linear_mcp import (
    build_linear_comment_body,
    linear_add_comment,
    linear_remote_sync_mode,
    linear_status_name_for_outcome,
)
from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status
from src.tandem_agents.runtime.runstate import (
    append_event,
    ensure_layout,
    load_blackboard,
    load_status,
    save_blackboard,
    write_status,
)
from src.tandem_agents.runtime.workspace_registry import load_workspace, record_run_reference, save_workspace
from src.tandem_agents.utils.utils import now_ms

logger = logging.getLogger("aca.coder_supervisor")

# Circuit-breaker defaults for the PR repair loop (TAN2-2). The lifecycle
# refresh runs on a ~30s cadence; without these bounds a PR stuck in
# "needs-repair" (red CI, un-dismissed "changes requested") would dispatch a
# fresh coder run every tick forever, burning tokens and spamming comments.
_REPAIR_MAX_ATTEMPTS_DEFAULT = 5
_REPAIR_COOLDOWN_BASE_MS_DEFAULT = 60_000  # 1 min, doubled per attempt (capped)
_REPAIR_COOLDOWN_MAX_SHIFT = 6  # cap exponential backoff at base * 2**6 (~64 min)


def _repair_max_attempts(cfg: ResolvedConfig) -> int:
    value = getattr(getattr(cfg, "review", None), "max_repair_attempts", None)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _REPAIR_MAX_ATTEMPTS_DEFAULT
    return parsed if parsed > 0 else _REPAIR_MAX_ATTEMPTS_DEFAULT


def _repair_cooldown_base_ms(cfg: ResolvedConfig) -> int:
    value = getattr(getattr(cfg, "review", None), "repair_cooldown_base_ms", None)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _REPAIR_COOLDOWN_BASE_MS_DEFAULT
    return parsed if parsed > 0 else _REPAIR_COOLDOWN_BASE_MS_DEFAULT


def _repair_signature(context: dict[str, Any]) -> str:
    """Stable fingerprint of the actionable PR feedback.

    Two refresh ticks that surface the *same* review comments and failing
    checks produce the same signature. An unchanged signature after a repair
    pass means the pass moved nothing — a strong "escalate, don't re-grind"
    signal. Includes the head branch so a force-push that changes nothing else
    still counts as unchanged.
    """
    items = context.get("feedback_items") or []
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        body_hash = hashlib.sha1(str(item.get("body") or "").encode("utf-8")).hexdigest()
        parts.append(
            "|".join(
                [
                    str(item.get("kind") or ""),
                    str(item.get("url") or ""),
                    str(item.get("path") or ""),
                    str(item.get("line") or ""),
                    body_hash,
                ]
            )
        )
    pr = context.get("pull_request") if isinstance(context.get("pull_request"), dict) else {}
    head = str(pr.get("head_branch") or "")
    joined = head + "\n" + "\n".join(sorted(parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _repair_gate_decision(
    state: dict[str, Any],
    signature: str,
    now_ms_value: int,
    *,
    max_attempts: int,
    cooldown_base_ms: int,
) -> tuple[str, dict[str, Any], str]:
    """Decide whether to dispatch another repair pass.

    Returns ``(decision, new_state, reason)`` where decision is one of:

    - ``"proceed"``  — dispatch a pass; ``new_state`` has attempts incremented.
    - ``"defer"``    — within the exponential cooldown window; try again later.
    - ``"escalate"`` — cap reached or feedback unchanged since the last pass;
                        hand off to a human, do not spend more tokens.
    - ``"skip"``     — already escalated; do nothing.

    Pure function (no I/O) so it is unit-testable in isolation.
    """
    new_state: dict[str, Any] = {
        "attempts": int(state.get("attempts") or 0),
        "last_attempt_ms": int(state.get("last_attempt_ms") or 0),
        "last_signature": str(state.get("last_signature") or ""),
        "escalated": bool(state.get("escalated") or False),
        "reason": str(state.get("reason") or ""),
    }
    if new_state["escalated"]:
        return "skip", new_state, new_state["reason"] or "already_escalated"

    attempts = new_state["attempts"]
    if attempts >= max_attempts:
        new_state["escalated"] = True
        new_state["reason"] = "max_attempts"
        return "escalate", new_state, "max_attempts"

    if attempts > 0:
        shift = min(attempts - 1, _REPAIR_COOLDOWN_MAX_SHIFT)
        cooldown = cooldown_base_ms * (2 ** shift)
        if now_ms_value - new_state["last_attempt_ms"] < cooldown:
            return "defer", new_state, "cooldown"
        if signature and signature == new_state["last_signature"]:
            new_state["escalated"] = True
            new_state["reason"] = "no_new_feedback"
            return "escalate", new_state, "no_new_feedback"

    new_state["attempts"] = attempts + 1
    new_state["last_attempt_ms"] = now_ms_value
    new_state["last_signature"] = signature
    return "proceed", new_state, ""

TERMINAL_CODER_STATUSES = {"completed", "failed", "blocked", "cancelled", "canceled"}
NON_TERMINAL_RUN_STATUSES = {"created", "running"}
BLACKBOARD_SUPERVISION_MARKERS = (
    "coder_run:",
    "execution_backend: coder",
    "execution_backend: 'coder'",
    'execution_backend: "coder"',
    "pull_request_lifecycle:",
)


def _normalize_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "cancelled" if normalized == "canceled" else normalized


def _run_dir(cfg: ResolvedConfig, run_id: str) -> Path:
    return cfg.output_root() / run_id


def _is_run_directory(run_dir: Path) -> bool:
    if not run_dir.is_dir():
        return False
    name = run_dir.name
    if name in {"state", "browser-tests"} or name.startswith(("_", ".")):
        return False
    if not (name.startswith("run-") or name.startswith("sched-") or name.startswith("qa-") or name.startswith("bak-run-")):
        return False
    return (run_dir / "status.json").exists() or (run_dir / "blackboard.yaml").exists()


def _load_status_safe(path: Path) -> dict[str, Any]:
    try:
        loaded = load_status(path)
    except Exception:
        logger.debug("Skipping run with unreadable status file: %s", path, exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _blackboard_has_supervision_marker(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.debug("Skipping run with unreadable blackboard marker scan: %s", path, exc_info=True)
        return False
    return any(marker in text for marker in BLACKBOARD_SUPERVISION_MARKERS)


def _load_blackboard_for_supervision(run_dir: Path, status: dict[str, Any]) -> dict[str, Any]:
    blackboard_path = run_dir / "blackboard.yaml"
    if not blackboard_path.exists():
        return {}
    run = status.get("run") if isinstance(status, dict) else {}
    phase = status.get("phase") if isinstance(status, dict) else {}
    run_status = _normalize_status(run.get("status")) if isinstance(run, dict) else ""
    phase_name = str(phase.get("name") or "").strip() if isinstance(phase, dict) else ""
    if not (
        (run_status in NON_TERMINAL_RUN_STATUSES and phase_name == "coder_execution")
        or (run_status in {"completed", "running"} and _blackboard_has_supervision_marker(blackboard_path))
    ):
        return {}
    try:
        loaded = load_blackboard(blackboard_path)
    except Exception:
        logger.debug("Skipping run with unreadable blackboard file: %s", blackboard_path, exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _coder_run_id(run_id: str, blackboard: dict[str, Any]) -> str:
    coder_run = blackboard.get("coder_run")
    if isinstance(coder_run, dict):
        for key in ("coder_run_id", "coderRunId", "id"):
            value = str(coder_run.get(key) or "").strip()
            if value:
                return value
    return run_id


def _is_coder_execution(status: dict[str, Any], blackboard: dict[str, Any]) -> bool:
    run = status.get("run") if isinstance(status, dict) else {}
    phase = status.get("phase") if isinstance(status, dict) else {}
    return (
        isinstance(run, dict)
        and _normalize_status(run.get("status")) in NON_TERMINAL_RUN_STATUSES
        and isinstance(phase, dict)
        and str(phase.get("name") or "").strip() == "coder_execution"
        and (
            str(blackboard.get("execution_backend") or "").strip() == "coder"
            or isinstance(blackboard.get("coder_run"), dict)
        )
    )


def _is_pr_lifecycle_supervisable(status: dict[str, Any], blackboard: dict[str, Any]) -> bool:
    run = status.get("run") if isinstance(status, dict) else {}
    lifecycle = blackboard.get("pull_request_lifecycle") if isinstance(blackboard, dict) else {}
    if not isinstance(run, dict) or not isinstance(lifecycle, dict):
        return False
    if _normalize_status(run.get("status")) not in {"completed", "running"}:
        return False
    state = str(lifecycle.get("lifecycle_state") or "").strip()
    return bool(lifecycle.get("number")) and state in {
        "running",
        "waiting-for-review",
        "needs-repair",
        "ready-to-merge",
    } or (state == "blocked" and _is_retryable_pr_lifecycle_error(lifecycle))


def _is_retryable_pr_lifecycle_error(lifecycle: dict[str, Any]) -> bool:
    error = str(lifecycle.get("error") or "").strip()
    return "Could not read GitHub pull request" in error or "GitHub MCP" in error


def _source_task_terminal(task: dict[str, Any]) -> bool:
    values = [
        str(task.get("state") or "").strip().lower(),
        str(task.get("status") or "").strip().lower(),
    ]
    source = task.get("source")
    if isinstance(source, dict):
        values.append(str(source.get("state") or "").strip().lower())
        values.append(str(source.get("status") or "").strip().lower())
    return any(value in {"done", "completed", "closed", "cancelled", "canceled"} for value in values)


def _coordination_context(status: dict[str, Any], run_coord: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(status.get("coordination") or {})
    run_coord = run_coord or {}
    return {
        "task_key": raw.get("task_key") or run_coord.get("task_key"),
        "lease_id": raw.get("lease_id") or run_coord.get("lease_id"),
        "worker_id": raw.get("worker_id") or run_coord.get("worker_id"),
        "host_id": raw.get("host_id") or run_coord.get("host_id"),
        "lease_expires_at_ms": raw.get("lease_expires_at_ms"),
    }


def _touch_coordination(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    run_id: str,
    *,
    status: str,
    phase: str,
    error: str | None = None,
    completed: bool = False,
    supervision: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    existing = coordination.get_run(run_id) or {}
    metadata = dict(existing.get("metadata") or {})
    if supervision is not None:
        metadata["coder_supervision"] = supervision
    updated = coordination.update_run(
        run_id,
        status=status,
        phase=phase,
        error=error,
        metadata=metadata if metadata else None,
        completed=completed,
    )
    lease_id = str((updated or existing).get("lease_id") or "").strip()
    if status == "running" and lease_id:
        coordination.heartbeat_lease(lease_id, lease_ttl_seconds=cfg.coordination.lease_ttl_seconds)
    return updated


def _update_workspace_reference(cfg: ResolvedConfig, run_id: str, status: str) -> None:
    try:
        workspace = load_workspace(cfg.root_dir)
        runs = list((workspace.get("workspace") or {}).get("runs") or [])
        existing = next((item for item in runs if str(item.get("run_id") or "") == run_id), None)
        if not isinstance(existing, dict):
            return
        save_workspace(
            cfg.root_dir,
            record_run_reference(
                workspace,
                run_id=run_id,
                project_id=str(existing.get("project_id") or ""),
                project_key=str(existing.get("project_key") or ""),
                status=status,
                phase=str(existing.get("phase") or "coder_execution"),
                execution_backend=str(existing.get("execution_backend") or "coder"),
                admission_role=str(existing.get("admission_role") or "aca_scheduler"),
                execution_path=str(existing.get("execution_path") or "tandem_coder"),
                task_key=str(existing.get("task_key") or ""),
                task_title=str(existing.get("task_title") or ""),
                created_at_ms=existing.get("created_at_ms"),
            ),
        )
    except Exception:
        logger.debug("Failed to update workspace run reference for %s", run_id, exc_info=True)


def _task_source_type(task: dict[str, Any], cfg: ResolvedConfig) -> str:
    source = task.get("source") if isinstance(task, dict) else {}
    if isinstance(source, dict):
        return str(source.get("type") or cfg.task_source.type or "").strip().lower()
    return str(cfg.task_source.type or "").strip().lower()


def _maybe_add_linear_repair_comment(cfg: ResolvedConfig, task: dict[str, Any], body: str) -> None:
    if _task_source_type(task, cfg) != "linear":
        return
    try:
        linear_add_comment(cfg, task, body)
    except Exception:
        logger.debug("Failed to add Linear repair comment", exc_info=True)


def _linear_finalize_enabled(cfg: ResolvedConfig, task: dict[str, Any]) -> bool:
    source_type = _task_source_type(task, cfg)
    return (
        source_type == "linear"
        and linear_remote_sync_mode(cfg, source_type) != "off"
        and str(cfg.linear_mcp.scope or "").strip().lower() in {"intake_finalize", "always"}
    )


def _linear_task_with_run_id(task: dict[str, Any], run_id: str) -> dict[str, Any]:
    updated = dict(task)
    if updated.get("run_id") in (None, ""):
        updated["run_id"] = run_id
    return updated


def _enqueue_linear_finalize(
    *,
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    task: dict[str, Any],
    run_id: str,
    outcome: str,
    summary: str,
) -> None:
    if not _linear_finalize_enabled(cfg, task):
        return
    target_status = linear_status_name_for_outcome(cfg, outcome)
    labels: list[str] = []
    if outcome != "completed" and str(cfg.linear_mcp.blocked_label or "").strip():
        labels.append(cfg.linear_mcp.blocked_label)
    task_payload = _linear_task_with_run_id(task, run_id)
    coordination.enqueue_outbox(
        kind="linear_issue.status_update",
        aggregate_type="task",
        aggregate_id=str(task.get("task_id") or run_id),
        payload={
            "run_id": run_id,
            "outcome": outcome,
            "summary": summary,
            "target_status": target_status,
            "labels": labels,
            "task": task_payload,
        },
        dedupe_key=f"{run_id}:linear:finalize-status",
    )
    remote_sync = linear_remote_sync_mode(cfg, "linear")
    if outcome != "completed" and remote_sync in {"status_comment", "rich"}:
        body = build_linear_comment_body(
            run_id=run_id,
            task_title=str(task.get("title") or "Linear task"),
            outcome=outcome,
            summary=(
                f"{summary}\n\n"
                "Next expected action: inspect the blocked ACA run, resolve the error, "
                "and restart or repair the task before marking it complete."
            ).strip(),
        )
        coordination.enqueue_outbox(
            kind="linear_issue.comment",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "outcome": outcome,
                "summary": summary,
                "body": body,
                "task": task_payload,
            },
            dedupe_key=f"{run_id}:linear:finalize-comment",
        )


def _enqueue_linear_merge_finalize(
    *,
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    task: dict[str, Any],
    run_id: str,
    pull_request: dict[str, Any],
    merge: dict[str, Any],
) -> None:
    if not _linear_finalize_enabled(cfg, task) or not merge.get("merged"):
        return
    target_status = cfg.linear_mcp.done_status or "Done"
    labels = [cfg.linear_mcp.done_label] if str(cfg.linear_mcp.done_label or "").strip() else []
    task_payload = _linear_task_with_run_id(task, run_id)
    coordination.enqueue_outbox(
        kind="linear_issue.status_update",
        aggregate_type="task",
        aggregate_id=str(task.get("task_id") or run_id),
        payload={
            "run_id": run_id,
            "outcome": "merged",
            "summary": "Pull request merged by ACA guarded auto-merge.",
            "target_status": target_status,
            "labels": labels,
            "task": task_payload,
            "pull_request": pull_request,
            "merge": merge,
        },
        dedupe_key=f"{run_id}:linear:merge-status",
    )
    if linear_remote_sync_mode(cfg, "linear") in {"status_comment", "rich"}:
        pr_url = str(pull_request.get("url") or "").strip()
        strategy = str(merge.get("strategy") or "").strip()
        branch_deleted = "yes" if merge.get("branch_deleted") else "no"
        body = "\n".join(
            [
                f"ACA merged the pull request for run `{run_id}` and moved this issue to `{target_status}`.",
                "",
                f"Pull request: {pr_url or 'unknown'}",
                f"Merge strategy: `{strategy or 'unknown'}`",
                f"Remote branch deleted: `{branch_deleted}`",
                "",
                f"<!-- aca:linear-merge:{run_id} -->",
            ]
        ).strip()
        coordination.enqueue_outbox(
            kind="linear_issue.comment",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "outcome": "merged",
                "summary": "Pull request merged by ACA guarded auto-merge.",
                "body": body,
                "task": task_payload,
                "pull_request": pull_request,
                "merge": merge,
            },
            dedupe_key=f"{run_id}:linear:merge-comment",
        )


def _start_pr_repair_pass(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    pull_request: dict[str, Any],
) -> dict[str, Any]:
    task = dict(status_payload.get("task") or blackboard.get("task") or {})
    context = collect_pull_request_repair_context(cfg, pull_request)
    prompt = build_pull_request_repair_prompt(context)
    repair = {
        "run_id": run_id,
        "repair_run_id": f"{run_id}-repair-{now_ms()}",
        "status": "no_action",
        "pull_request": context.get("pull_request") or pull_request,
        "context": context,
        "prompt": prompt,
        "started_at_ms": now_ms(),
        "completed_at_ms": None,
        "summary": "",
    }

    def _persist_repair(repair_state: dict[str, Any], breaker: dict[str, Any] | None = None) -> None:
        blackboard["pull_request_repair"] = repair_state
        status_payload["pull_request_repair"] = repair_state
        save_blackboard(layout["blackboard"], blackboard)
        write_status(layout["status"], status_payload)
        run = coordination.get_run(run_id) or {}
        metadata = dict(run.get("metadata") or {})
        metadata["pull_request_repair"] = repair_state
        if breaker is not None:
            metadata["pull_request_repair_state"] = breaker
        coordination.update_run(run_id, metadata=metadata)

    if not context.get("actionable"):
        repair["summary"] = str(context.get("reason") or "No actionable PR feedback.")
        _persist_repair(repair)
        return repair

    # Circuit breaker: bound attempts, back off exponentially between passes,
    # and escalate rather than re-dispatch when the feedback is unchanged (the
    # last pass moved nothing). Without this the ~30s lifecycle refresh would
    # dispatch a fresh coder run for a stuck PR every tick, forever (TAN2-2).
    run_meta = dict((coordination.get_run(run_id) or {}).get("metadata") or {})
    breaker_state = dict(run_meta.get("pull_request_repair_state") or {})
    signature = _repair_signature(context)
    max_attempts = _repair_max_attempts(cfg)

    # Per-issue budget beats the attempt gate (TAN2-1): if the issue has already
    # spent its token/cost/execution budget, escalate rather than dispatch — no
    # matter how many repair attempts remain.
    spend = load_issue_spend(coordination, run_id)
    over_budget, budget_reason = budget_status(spend, cfg)
    if over_budget:
        already_escalated = bool(breaker_state.get("escalated"))
        breaker_state = {
            "attempts": int(breaker_state.get("attempts") or 0),
            "last_attempt_ms": int(breaker_state.get("last_attempt_ms") or 0),
            "last_signature": str(breaker_state.get("last_signature") or ""),
            "escalated": True,
            "reason": f"budget_exhausted ({budget_reason})",
        }
        decision = "skip" if already_escalated else "escalate"
        reason = breaker_state["reason"]
    else:
        decision, breaker_state, reason = _repair_gate_decision(
            breaker_state,
            signature,
            now_ms(),
            max_attempts=max_attempts,
            cooldown_base_ms=_repair_cooldown_base_ms(cfg),
        )

    if decision == "defer":
        repair.update(
            {
                "status": "deferred",
                "completed_at_ms": now_ms(),
                "summary": f"Repair pass deferred (cooldown) after attempt {breaker_state['attempts']}.",
                "breaker": breaker_state,
            }
        )
        _persist_repair(repair, breaker_state)
        append_event(layout["events"], "github_pull_request.repair_pass_deferred", run_id, repair)
        return repair

    if decision in {"escalate", "skip"}:
        escalated_pr = {
            **(context.get("pull_request") or pull_request),
            "lifecycle_state": "needs-human",
            "terminal": False,
            "repair_escalated": True,
            "repair_escalation_reason": reason,
            "last_checked_at_ms": now_ms(),
        }
        _persist_pull_request_lifecycle(
            cfg,
            coordination,
            run_id=run_id,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            pull_request=escalated_pr,
        )
        repair.update(
            {
                "status": "escalated",
                "completed_at_ms": now_ms(),
                "summary": f"Escalated to human after {breaker_state['attempts']} repair attempt(s): {reason}.",
                "breaker": breaker_state,
            }
        )
        _persist_repair(repair, breaker_state)
        # Post the escalation comment only on the tick we first flip to
        # escalated (decision == "escalate"); subsequent ticks return "skip"
        # and stay silent so we don't re-spam the issue.
        if decision == "escalate":
            _maybe_add_linear_repair_comment(
                cfg,
                task,
                f"ACA attempted {breaker_state['attempts']} repair pass(es) on "
                f"{pull_request.get('url') or 'the linked PR'} without resolving the feedback "
                f"(reason: {reason}). Pausing automated repairs and escalating for human review.",
            )
            append_event(layout["events"], "github_pull_request.repair_escalated", run_id, repair)
        return repair

    # decision == "proceed": dispatch a repair pass.
    first_attempt = breaker_state["attempts"] <= 1
    if first_attempt:
        _maybe_add_linear_repair_comment(
            cfg,
            task,
            f"ACA is starting repair passes for PR feedback on "
            f"{pull_request.get('url') or 'the linked PR'} (up to {max_attempts} attempts).",
        )
    # The Tandem coder endpoint accepts a generic payload; use a distinct
    # workflow_mode so the engine can route this as a same-branch PR repair.
    payload = {
        "coder_run_id": repair["repair_run_id"],
        "workflow_mode": "pr_repair",
        "repo_binding": {
            "workspace_id": "aca",
            "workspace_root": str((status_payload.get("repo") or {}).get("path") or ""),
            "repo_slug": str((status_payload.get("repo") or {}).get("slug") or cfg.repository.slug or ""),
            "default_branch": str(pull_request.get("base_branch") or cfg.repository.default_branch or "main"),
        },
        "github_ref": {
            "kind": "pull_request",
            "number": pull_request.get("number"),
            "url": pull_request.get("url"),
            "head_branch": pull_request.get("head_branch"),
        },
        "objective": prompt,
        "source_client": "aca",
        "repair_context": context,
    }
    create_response: Any = {}
    execute_response: Any = {}
    try:
        create_response = sdk_coder_create_run(cfg, payload)
        execute_response = sdk_coder_execute_all(cfg, repair["repair_run_id"], {"max_steps": 16})
        repair.update(
            {
                # "dispatched", not "completed": the pass was sent, but success
                # (a new commit + CI transition) is only confirmable when the
                # next lifecycle refresh moves the PR off "needs-repair".
                "status": "dispatched",
                "completed_at_ms": now_ms(),
                "summary": (
                    f"Repair pass {breaker_state['attempts']}/{max_attempts} dispatched to Tandem coder; "
                    "outcome verified on next lifecycle refresh."
                ),
                "create_response": create_response if isinstance(create_response, dict) else {},
                "execute_response": execute_response if isinstance(execute_response, dict) else {},
                "breaker": breaker_state,
            }
        )
    except Exception as exc:
        repair.update(
            {
                "status": "blocked",
                "completed_at_ms": now_ms(),
                "summary": str(exc).strip() or repr(exc),
                "breaker": breaker_state,
            }
        )
    # Fold this pass's usage into the per-issue spend ledger (counts one coder
    # execution even on failure) so budget enforcement sees repair spend.
    spend = record_coder_spend(
        coordination,
        run_id,
        {
            "create_response": create_response if isinstance(create_response, dict) else {},
            "execute_response": execute_response if isinstance(execute_response, dict) else {},
        },
    )
    repair["issue_spend"] = spend
    _persist_repair(repair, breaker_state)
    append_event(layout["events"], "github_pull_request.repair_pass_completed", run_id, repair)
    return repair


def _persist_pull_request_lifecycle(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    pull_request: dict[str, Any],
) -> dict[str, Any]:
    pr_url = str(pull_request.get("url") or blackboard.get("pull_request") or status_payload.get("pull_request") or "").strip()
    blackboard["pull_request"] = pr_url
    blackboard["pull_request_lifecycle"] = pull_request
    save_blackboard(layout["blackboard"], blackboard)
    status_payload["pull_request"] = pr_url
    status_payload["pull_request_lifecycle"] = pull_request
    if not isinstance(status_payload.get("task"), dict):
        status_payload["task"] = {}
    status_payload["task"]["pull_request"] = pr_url
    status_payload["task"]["pull_request_lifecycle"] = pull_request
    write_status(layout["status"], status_payload)
    run = coordination.get_run(run_id) or {}
    metadata = dict(run.get("metadata") or {})
    metadata["pull_request"] = pr_url
    metadata["pull_request_lifecycle"] = pull_request
    coordination.update_run(run_id, metadata=metadata)
    return pull_request


def _persist_pull_request_merge(
    coordination: CoordinationStore,
    *,
    run_id: str,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    merge: dict[str, Any],
) -> dict[str, Any]:
    blackboard["pull_request_merge"] = merge
    status_payload["pull_request_merge"] = merge
    save_blackboard(layout["blackboard"], blackboard)
    write_status(layout["status"], status_payload)
    run = coordination.get_run(run_id) or {}
    metadata = dict(run.get("metadata") or {})
    metadata["pull_request_merge"] = merge
    coordination.update_run(run_id, metadata=metadata)
    return merge


def _update_pr_branch_for_run(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    pull_request: dict[str, Any],
) -> dict[str, Any]:
    """Update a behind-base PR branch and record the outcome (TAN2-3)."""
    try:
        result = update_pull_request_branch(cfg, pull_request)
    except Exception as exc:
        result = {"updated": False, "error": str(exc).strip() or repr(exc)}
    result["completed_at_ms"] = now_ms()
    blackboard["pull_request_rebase"] = result
    status_payload["pull_request_rebase"] = result
    save_blackboard(layout["blackboard"], blackboard)
    write_status(layout["status"], status_payload)
    run = coordination.get_run(run_id) or {}
    metadata = dict(run.get("metadata") or {})
    metadata["pull_request_rebase"] = result
    coordination.update_run(run_id, metadata=metadata)
    append_event(layout["events"], "github_pull_request.branch_update", run_id, result)
    return result


def _maybe_auto_merge_pr(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    pull_request: dict[str, Any],
) -> dict[str, Any] | None:
    if str(cfg.review.policy or "").strip().lower() != "auto_merge":
        return None
    approvals = blackboard.get("finalization_approvals")
    if not isinstance(approvals, dict):
        approvals = status_payload.get("finalization_approvals")
    if not isinstance(approvals, dict):
        approvals = {}
    try:
        merge = guarded_auto_merge(cfg, pull_request, approvals=approvals)
    except Exception as exc:
        merge = {
            "status": "blocked",
            "merged": False,
            "branch_deleted": False,
            "strategy": str(cfg.review.auto_merge_strategy or "").strip().lower(),
            "pull_request": pull_request,
            "error": str(exc).strip() or repr(exc),
            "completed_at_ms": now_ms(),
        }
    else:
        merge["completed_at_ms"] = now_ms()
    _persist_pull_request_merge(
        coordination,
        run_id=run_id,
        layout=layout,
        status_payload=status_payload,
        blackboard=blackboard,
        merge=merge,
    )
    append_event(layout["events"], "github_pull_request.auto_merge_evaluated", run_id, merge)
    if merge.get("merged"):
        task = dict(status_payload.get("task") or blackboard.get("task") or {})
        merged_pr = {
            **pull_request,
            "lifecycle_state": "merged",
            "terminal": True,
            "merged": True,
            "merge_strategy": merge.get("strategy"),
            "branch_deleted": merge.get("branch_deleted"),
            "last_checked_at_ms": now_ms(),
        }
        _persist_pull_request_lifecycle(
            cfg,
            coordination,
            run_id=run_id,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            pull_request=merged_pr,
        )
        _enqueue_linear_merge_finalize(
            cfg=cfg,
            coordination=coordination,
            task=task,
            run_id=run_id,
            pull_request=merged_pr,
            merge=merge,
        )
    return merge


def _refresh_pr_lifecycle_for_run(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
) -> dict[str, Any]:
    existing = dict(
        blackboard.get("pull_request_lifecycle")
        or status_payload.get("pull_request_lifecycle")
        or {}
    )
    if not existing:
        return {"run_id": run_id, "status": "skipped", "reason": "missing_pull_request_lifecycle"}
    try:
        refreshed = refresh_pull_request_lifecycle(cfg, existing)
    except Exception as exc:
        message = str(exc).strip() or repr(exc)
        existing_state = str(existing.get("lifecycle_state") or "").strip()
        retryable_state = existing_state if existing_state in {
            "running",
            "waiting-for-review",
            "needs-repair",
            "needs-rebase",
            "conflicted",
            "ready-to-merge",
        } else "waiting-for-review"
        failed = {
            **existing,
            "lifecycle_state": retryable_state,
            "terminal": False,
            "error": message,
            "last_checked_at_ms": now_ms(),
        }
        _persist_pull_request_lifecycle(
            cfg,
            coordination,
            run_id=run_id,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            pull_request=failed,
        )
        append_event(layout["events"], "github_pull_request.lifecycle_refresh_failed", run_id, failed)
        return {"run_id": run_id, "status": retryable_state, "terminal": False, "pull_request": failed, "error": message}
    refreshed["last_checked_at_ms"] = now_ms()
    _persist_pull_request_lifecycle(
        cfg,
        coordination,
        run_id=run_id,
        layout=layout,
        status_payload=status_payload,
        blackboard=blackboard,
        pull_request=refreshed,
    )
    lifecycle_state = str(refreshed.get("lifecycle_state") or "").strip()
    rebase = None
    if lifecycle_state == "needs-rebase":
        # Behind base but otherwise clean: update the branch (cheap, no coder
        # tokens). The next refresh re-evaluates once CI re-runs (TAN2-3).
        rebase = _update_pr_branch_for_run(
            cfg,
            coordination,
            run_id=run_id,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            pull_request=refreshed,
        )
    repair = None
    # A conflicted PR is routed through the same repair machinery so the agent
    # attempts conflict resolution under the circuit breaker; the breaker
    # escalates to a human if it can't be resolved (TAN2-2/TAN2-3).
    if lifecycle_state in {"needs-repair", "conflicted"}:
        repair = _start_pr_repair_pass(
            cfg,
            coordination,
            run_id=run_id,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            pull_request=refreshed,
        )
    merge = None
    if str(refreshed.get("lifecycle_state") or "").strip() == "ready-to-merge":
        merge = _maybe_auto_merge_pr(
            cfg,
            coordination,
            run_id=run_id,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            pull_request=refreshed,
        )
    append_event(layout["events"], "github_pull_request.lifecycle_refreshed", run_id, refreshed)
    current_lifecycle = dict(
        blackboard.get("pull_request_lifecycle")
        or status_payload.get("pull_request_lifecycle")
        or refreshed
    )
    result = {
        "run_id": run_id,
        "status": current_lifecycle.get("lifecycle_state") or refreshed.get("lifecycle_state") or "unknown",
        "terminal": bool(current_lifecycle.get("terminal")),
        "pull_request": current_lifecycle,
    }
    if repair is not None:
        result["repair"] = repair
    if rebase is not None:
        result["rebase"] = rebase
    if merge is not None:
        result["merge"] = merge
    return result


def _enqueue_github_finalize(
    *,
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    task: dict[str, Any],
    run_id: str,
    outcome: str,
    summary: str,
) -> None:
    source_type = str((task.get("source") or {}).get("type") or cfg.task_source.type)
    remote_sync = github_remote_sync_mode(cfg, source_type)
    scope = github_mcp_scope(cfg, source_type)
    if remote_sync == "off" or scope not in {"intake_finalize", "always"}:
        return
    target_status = github_project_status_name_for_outcome(outcome)
    coordination.enqueue_outbox(
        kind="github_project.status_update",
        aggregate_type="task",
        aggregate_id=str(task.get("task_id") or run_id),
        payload={
            "run_id": run_id,
            "outcome": outcome,
            "summary": summary,
            "target_status": target_status,
            "task": task,
        },
        dedupe_key=f"{run_id}:github:finalize-status",
    )
    if remote_sync == "status_comment":
        coordination.enqueue_outbox(
            kind="github_issue.comment",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "outcome": outcome,
                "summary": summary,
                "body": build_issue_comment_body(
                    run_id=run_id,
                    task_title=task.get("title") or "GitHub task",
                    outcome=outcome,
                    summary=summary,
                ),
                "task": task,
            },
            dedupe_key=f"{run_id}:github:finalize-comment",
        )


def _supervision_payload(
    *,
    coder_run_id: str,
    tandem_status: str,
    tandem_phase: str,
    monitor_state: str,
    last_error: str = "",
    terminal: bool = False,
    cancel_requested_at_ms: int | None = None,
) -> dict[str, Any]:
    now = now_ms()
    payload: dict[str, Any] = {
        "coder_run_id": coder_run_id,
        "tandem_status": tandem_status,
        "tandem_phase": tandem_phase,
        "monitor_state": monitor_state,
        "last_checked_at_ms": now,
        "next_check_at_ms": None if terminal else now,
        "last_error": last_error,
        "terminal": terminal,
    }
    if cancel_requested_at_ms is not None:
        payload["cancel_requested_at_ms"] = cancel_requested_at_ms
    return payload


def _apply_non_terminal(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    coder_run_id: str,
    tandem_status: str,
    tandem_phase: str,
) -> dict[str, Any]:
    message = f"Tandem coder run `{coder_run_id}` is still `{tandem_status or 'running'}`."
    supervision = _supervision_payload(
        coder_run_id=coder_run_id,
        tandem_status=tandem_status or "running",
        tandem_phase=tandem_phase,
        monitor_state="running",
    )
    blackboard["coder_supervision"] = supervision
    save_blackboard(layout["blackboard"], blackboard)
    updated_status = set_status(
        status_payload,
        layout,
        phase="coder_execution",
        phase_detail=message,
        run_status="running",
        blocker=(False, None, None, None),
    )
    _touch_coordination(cfg, coordination, run_id, status="running", phase="coder_execution", supervision=supervision)
    save_run_text(
        layout["summary"],
        "\n".join(
            [
                "# Coder run still running",
                "",
                f"Task: {(updated_status.get('task') or {}).get('title') or 'Untitled task'}",
                "",
                message,
                "",
                "Tandem remains the execution authority. ACA will reconcile this run before posting a final GitHub update.",
            ]
        ),
    )
    append_event(layout["events"], "coder.supervisor.running", run_id, supervision)
    _update_workspace_reference(cfg, run_id, "running")
    return {"run_id": run_id, "status": "running", "terminal": False, "supervision": supervision}


def _apply_terminal(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    status_payload: dict[str, Any],
    blackboard: dict[str, Any],
    coder_result: dict[str, Any],
) -> dict[str, Any]:
    tandem_status = _normalize_status(coder_result.get("status"))
    tandem_phase = str(coder_result.get("phase") or "").strip().lower()
    coder_run = dict(coder_result.get("coder_run") or {})
    coder_run_id = _coder_run_id(run_id, {"coder_run": coder_run})
    terminal_status = "completed" if tandem_status == "completed" else "cancelled" if tandem_status == "cancelled" else "blocked"
    outcome = "completed" if tandem_status == "completed" else "blocked"
    terminal_phase = "handoff" if tandem_status == "completed" else "coder_execution"
    task = dict(status_payload.get("task") or blackboard.get("task") or {})
    repo = dict(status_payload.get("repo") or blackboard.get("repo") or {})
    supervision = _supervision_payload(
        coder_run_id=coder_run_id,
        tandem_status=tandem_status,
        tandem_phase=tandem_phase,
        monitor_state="terminal",
        last_error=str(coder_result.get("last_error") or "").strip(),
        terminal=True,
    )
    blackboard["coder_run"] = coder_run
    blackboard["artifacts"] = coder_result.get("artifacts") or blackboard.get("artifacts") or []
    blackboard["coder_supervision"] = supervision
    save_blackboard(layout["blackboard"], blackboard)

    if tandem_status == "completed":
        summary = build_coder_summary(
            run_id=run_id,
            task=task,
            repo=repo,
            engine_label=str((status_payload.get("engine") or {}).get("version") or "unknown"),
            provider_id=str((status_payload.get("provider") or {}).get("id") or cfg.provider.id),
            provider_model=str((status_payload.get("provider") or {}).get("model") or cfg.provider.model),
            coder_result=coder_result,
        )
        phase_detail = "coder workflow completed"
        blocker = (False, None, None, None)
    else:
        summary = str(coder_result.get("last_error") or f"Coder workflow stopped with status `{tandem_status or 'unknown'}`")
        phase_detail = summary
        blocker = (True, "coder", summary, "manager")
        summary = build_blocked_summary(task_title=task.get("title"), message=summary)

    updated_status = set_status(
        status_payload,
        layout,
        phase=terminal_phase,
        phase_detail=phase_detail,
        run_status=terminal_status,
        blocker=blocker,
        run_completed=True,
    )
    save_run_text(layout["summary"], summary)
    run_coord = _touch_coordination(
        cfg,
        coordination,
        run_id,
        status=terminal_status,
        phase=terminal_phase,
        error=None if terminal_status == "completed" else phase_detail,
        completed=True,
        supervision=supervision,
    )
    context = _coordination_context(updated_status, run_coord)
    task_key = str(context.get("task_key") or "").strip()
    lease_id = str(context.get("lease_id") or "").strip()
    worker_id = str(context.get("worker_id") or "").strip()
    host_id = str(context.get("host_id") or "").strip()
    if task_key:
        if terminal_status == "completed":
            coordination.mark_task_done(task_key, run_id=run_id, lease_id=lease_id or None, worker_id=worker_id or None, host_id=host_id or None, reason="coder workflow completed")
        else:
            coordination.mark_task_blocked(task_key, run_id=run_id, lease_id=lease_id or None, worker_id=worker_id or None, host_id=host_id or None, reason=phase_detail)
    if lease_id:
        coordination.release_lease(lease_id, status="completed" if terminal_status == "completed" else terminal_status, reason=phase_detail)
    if _task_source_type(task, cfg) == "linear":
        _enqueue_linear_finalize(
            cfg=cfg,
            coordination=coordination,
            task=task,
            run_id=run_id,
            outcome=outcome,
            summary=summary,
        )
    else:
        _enqueue_github_finalize(cfg=cfg, coordination=coordination, task=task, run_id=run_id, outcome=outcome, summary=summary)
    append_event(layout["events"], "run.completed" if terminal_status == "completed" else "run.blocked", run_id, {"kind": "coder", "supervised": True, "tandem_status": tandem_status})
    _update_workspace_reference(cfg, run_id, terminal_status)
    return {"run_id": run_id, "status": terminal_status, "terminal": True, "supervision": supervision}


def build_coder_result_from_response(run_id: str, response: dict[str, Any]) -> dict[str, Any]:
    coder_run = dict(response.get("coder_run") or {})
    final_run = dict(response.get("run") or {})
    status = _normalize_status(final_run.get("status") or coder_run.get("status"))
    phase = str(final_run.get("phase") or coder_run.get("phase") or "").strip().lower()
    artifacts: list[dict[str, Any]] = []
    for key in ("coder_artifacts", "artifacts"):
        raw = response.get(key) or []
        if isinstance(raw, list):
            artifacts.extend(dict(item) for item in raw if isinstance(item, dict))
    last_error = str(final_run.get("last_error") or response.get("error") or "").strip()
    if not coder_run:
        coder_run = {"coder_run_id": run_id, "status": status, "phase": phase}
    return {
        "run_response": response,
        "coder_run": coder_run,
        "run": final_run,
        "artifacts": artifacts,
        "status": status,
        "phase": phase,
        "last_error": last_error,
    }


def apply_coder_result(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    run_id: str,
    coder_result: dict[str, Any],
    status_payload: dict[str, Any] | None = None,
    blackboard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = _run_dir(cfg, run_id)
    layout = ensure_layout(run_dir)
    status_payload = status_payload if isinstance(status_payload, dict) else load_status(layout["status"])
    blackboard = blackboard if isinstance(blackboard, dict) else load_blackboard(layout["blackboard"])
    status = _normalize_status(coder_result.get("status"))
    if status not in TERMINAL_CODER_STATUSES:
        coder_run = dict(coder_result.get("coder_run") or {})
        return _apply_non_terminal(
            cfg,
            coordination,
            run_id=run_id,
            run_dir=run_dir,
            layout=layout,
            status_payload=status_payload,
            blackboard=blackboard,
            coder_run_id=_coder_run_id(run_id, {"coder_run": coder_run}),
            tandem_status=status,
            tandem_phase=str(coder_result.get("phase") or "").strip().lower(),
        )
    return _apply_terminal(
        cfg,
        coordination,
        run_id=run_id,
        run_dir=run_dir,
        layout=layout,
        status_payload=status_payload,
        blackboard=blackboard,
        coder_result=coder_result,
    )


def reconcile_coder_run(
    cfg: ResolvedConfig,
    run_id: str,
    *,
    coordination: CoordinationStore | None = None,
    cancel_reason: str | None = None,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    run_dir = _run_dir(cfg, run_id)
    layout = ensure_layout(run_dir)
    status_payload = load_status(layout["status"])
    blackboard = load_blackboard(layout["blackboard"])
    if not _is_coder_execution(status_payload, blackboard) and not cancel_reason:
        if _is_pr_lifecycle_supervisable(status_payload, blackboard):
            return _refresh_pr_lifecycle_for_run(
                cfg,
                store,
                run_id=run_id,
                layout=layout,
                status_payload=status_payload,
                blackboard=blackboard,
            )
        return {"run_id": run_id, "status": "skipped", "reason": "not_active_coder_execution"}
    coder_run_id = _coder_run_id(run_id, blackboard)
    task = status_payload.get("task") if isinstance(status_payload, dict) else {}
    if (
        not cancel_reason
        and bool(getattr(cfg.execution, "coder_cancel_on_source_terminal", True))
        and isinstance(task, dict)
        and _source_task_terminal(task)
    ):
        cancel_reason = "source task reached a terminal state before Tandem coder completed"
    cancel_requested_at_ms = None
    if cancel_reason:
        sdk_coder_cancel_run(cfg, coder_run_id, cancel_reason)
        cancel_requested_at_ms = now_ms()
    try:
        response = sdk_coder_get_run(cfg, coder_run_id)
    except Exception as exc:
        message = str(exc).strip() or repr(exc)
        supervision = _supervision_payload(
            coder_run_id=coder_run_id,
            tandem_status=str(((blackboard.get("coder_supervision") or {}).get("tandem_status") or "unknown")),
            tandem_phase=str(((blackboard.get("coder_supervision") or {}).get("tandem_phase") or "")),
            monitor_state="poll_error",
            last_error=message,
            cancel_requested_at_ms=cancel_requested_at_ms,
        )
        blackboard["coder_supervision"] = supervision
        save_blackboard(layout["blackboard"], blackboard)
        set_status(
            status_payload,
            layout,
            phase="coder_execution",
            phase_detail=message,
            run_status="running",
            blocker=(False, None, None, None),
        )
        _touch_coordination(cfg, store, run_id, status="running", phase="coder_execution", error=message, supervision=supervision)
        append_event(layout["events"], "coder.supervisor.poll_error", run_id, supervision)
        return {"run_id": run_id, "status": "running", "terminal": False, "error": message, "supervision": supervision}
    response_payload = response if isinstance(response, dict) else {}
    coder_result = build_coder_result_from_response(coder_run_id, response_payload)
    if cancel_requested_at_ms is not None:
        coder_result.setdefault("cancel_requested_at_ms", cancel_requested_at_ms)
    return apply_coder_result(
        cfg,
        store,
        run_id=run_id,
        coder_result=coder_result,
        status_payload=status_payload,
        blackboard=blackboard,
    )


def list_active_coder_runs(cfg: ResolvedConfig, *, limit: int | None = None) -> list[dict[str, Any]]:
    output_root = cfg.output_root()
    batch_size = max(1, int(limit or getattr(cfg.execution, "coder_supervisor_batch_size", 100) or 100))
    active: list[dict[str, Any]] = []
    if not output_root.exists():
        return active
    for run_dir in sorted(output_root.iterdir(), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True):
        if len(active) >= batch_size:
            break
        if not _is_run_directory(run_dir):
            continue
        status_payload = _load_status_safe(run_dir / "status.json")
        blackboard = _load_blackboard_for_supervision(run_dir, status_payload)
        if not _is_coder_execution(status_payload, blackboard) and not _is_pr_lifecycle_supervisable(status_payload, blackboard):
            continue
        run_meta = status_payload.get("run") if isinstance(status_payload, dict) else {}
        task = status_payload.get("task") if isinstance(status_payload, dict) else {}
        repo = status_payload.get("repo") if isinstance(status_payload, dict) else {}
        supervision = blackboard.get("coder_supervision") if isinstance(blackboard, dict) else {}
        active.append(
            {
                "run_id": run_dir.name,
                "coder_run_id": _coder_run_id(run_dir.name, blackboard),
                "task_title": task.get("title") if isinstance(task, dict) else None,
                "task_id": task.get("task_id") if isinstance(task, dict) else None,
                "task_key": (status_payload.get("coordination") or {}).get("task_key") if isinstance(status_payload, dict) else None,
                "repo_slug": repo.get("slug") if isinstance(repo, dict) else None,
                "repo_path": repo.get("path") if isinstance(repo, dict) else None,
                "status": run_meta.get("status") if isinstance(run_meta, dict) else None,
                "phase": (status_payload.get("phase") or {}).get("name") if isinstance(status_payload, dict) else None,
                "updated_at_ms": run_meta.get("updated_at_ms") if isinstance(run_meta, dict) else None,
                "coder_supervision": supervision if isinstance(supervision, dict) else {},
            }
        )
    return active


def list_active_coder_task_refs(cfg: ResolvedConfig, *, limit: int | None = None) -> list[dict[str, Any]]:
    output_root = cfg.output_root()
    batch_size = max(1, int(limit or getattr(cfg.execution, "coder_supervisor_batch_size", 100) or 100))
    refs: list[dict[str, Any]] = []
    if not output_root.exists():
        return refs
    for run_dir in sorted(output_root.iterdir(), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True):
        if len(refs) >= batch_size:
            break
        if not _is_run_directory(run_dir):
            continue
        status_payload = _load_status_safe(run_dir / "status.json")
        run = status_payload.get("run") if isinstance(status_payload, dict) else {}
        phase = status_payload.get("phase") if isinstance(status_payload, dict) else {}
        if not (
            isinstance(run, dict)
            and _normalize_status(run.get("status")) in NON_TERMINAL_RUN_STATUSES
            and isinstance(phase, dict)
            and str(phase.get("name") or "").strip() == "coder_execution"
        ):
            continue
        task = status_payload.get("task") if isinstance(status_payload, dict) else {}
        refs.append(
            {
                "run_id": run_dir.name,
                "task_id": task.get("task_id") if isinstance(task, dict) else None,
                "task_key": (status_payload.get("coordination") or {}).get("task_key") if isinstance(status_payload, dict) else None,
            }
        )
    return refs


def reconcile_active_coder_runs(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    active = list_active_coder_runs(cfg, limit=limit)
    results = [reconcile_coder_run(cfg, str(item["run_id"]), coordination=store) for item in active]
    return {"count": len(results), "results": results}


def task_has_active_coder_run(cfg: ResolvedConfig, task: dict[str, Any]) -> bool:
    task_key = str(task.get("task_key") or "").strip()
    task_id = str(task.get("task_id") or "").strip()
    for item in list_active_coder_runs(cfg):
        if task_key and str(item.get("task_key") or "") == task_key:
            return True
        if task_id and str(item.get("task_id") or "") == task_id:
            return True
    return False
