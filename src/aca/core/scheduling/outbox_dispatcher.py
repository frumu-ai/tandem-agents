from __future__ import annotations

import time
from typing import Any

from src.aca.config.config_types import ResolvedConfig
from src.aca.core.coordination.coordination import CoordinationStore
from src.aca.core.integrations.github_mcp import (
    add_issue_comment,
    build_issue_comment_body,
    ensure_github_mcp_connected,
    ensure_github_mcp_disconnected,
    create_pull_request,
    update_project_item_status,
)

DEFAULT_OUTBOX_BATCH_LIMIT = 25
DEFAULT_OUTBOX_MAX_ATTEMPTS = 5


def outbox_dispatcher_interval(cfg: ResolvedConfig) -> float:
    heartbeat = max(1, int(cfg.coordination.heartbeat_interval_seconds or 1))
    return max(1.0, min(10.0, heartbeat / 2.0))


def _coordination_store(cfg: ResolvedConfig, coordination: CoordinationStore | None = None) -> CoordinationStore:
    store = coordination or CoordinationStore.from_config(cfg)
    store.ensure_schema()
    return store


def _dispatch_status_update(cfg: ResolvedConfig, outbox: dict[str, Any]) -> dict[str, Any]:
    payload = dict(outbox.get("payload") or {})
    task = dict(payload.get("task") or {})
    target_status = str(payload.get("target_status") or payload.get("status_name") or "").strip()
    if not target_status:
        return {
            "outbox_id": outbox.get("id"),
            "kind": outbox.get("kind"),
            "payload": payload,
            "status": "failed",
            "terminal": True,
            "error": "Outbox payload missing target_status for GitHub Project status update.",
        }
    warning = update_project_item_status(cfg, task, target_status)
    if warning:
        return {
            "outbox_id": outbox.get("id"),
            "kind": outbox.get("kind"),
            "payload": payload,
            "status": "failed",
            "terminal": True,
            "error": warning,
        }
    return {
        "outbox_id": outbox.get("id"),
        "kind": outbox.get("kind"),
        "payload": payload,
        "status": "dispatched",
        "terminal": False,
        "error": "",
    }


def _dispatch_issue_comment(cfg: ResolvedConfig, outbox: dict[str, Any]) -> dict[str, Any]:
    payload = dict(outbox.get("payload") or {})
    task = dict(payload.get("task") or {})
    if payload.get("run_id") not in (None, "") and task.get("run_id") in (None, ""):
        task["run_id"] = payload.get("run_id")
    body = str(payload.get("body") or "").strip()
    if not body:
        body = build_issue_comment_body(
            run_id=str(payload.get("run_id") or ""),
            task_title=str((task or {}).get("title") or "GitHub task"),
            outcome=str(payload.get("outcome") or "completed"),
            summary=str(payload.get("summary") or ""),
            diff_snapshot=payload.get("diff_snapshot"),
            review_returncode=payload.get("review_returncode"),
            test_returncode=payload.get("test_returncode"),
        )
    if not body.strip():
        return {
            "outbox_id": outbox.get("id"),
            "kind": outbox.get("kind"),
            "payload": payload,
            "status": "failed",
            "terminal": True,
            "error": "Outbox payload missing comment body.",
        }
    warning = add_issue_comment(cfg, task, body)
    if warning:
        return {
            "outbox_id": outbox.get("id"),
            "kind": outbox.get("kind"),
            "payload": payload,
            "status": "failed",
            "terminal": True,
            "error": warning,
        }
    return {
        "outbox_id": outbox.get("id"),
        "kind": outbox.get("kind"),
        "payload": payload,
        "status": "dispatched",
        "terminal": False,
        "error": "",
    }


def _dispatch_pull_request_create(cfg: ResolvedConfig, outbox: dict[str, Any]) -> dict[str, Any]:
    payload = dict(outbox.get("payload") or {})
    task = dict(payload.get("task") or {})
    if payload.get("run_id") not in (None, "") and task.get("run_id") in (None, ""):
        task["run_id"] = payload.get("run_id")
    head_branch = str(payload.get("head_branch") or "").strip()
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not head_branch or not title:
        return {
            "outbox_id": outbox.get("id"),
            "kind": outbox.get("kind"),
            "payload": payload,
            "status": "failed",
            "terminal": True,
            "error": "Outbox payload missing PR head branch or title.",
        }
    pr_url = create_pull_request(cfg, task, head_branch=head_branch, title=title, body=body)
    if pr_url is None or str(pr_url).strip() == "":
        return {
            "outbox_id": outbox.get("id"),
            "kind": outbox.get("kind"),
            "payload": payload,
            "status": "failed",
            "terminal": True,
            "error": "Pull request creation returned no URL.",
        }
    return {
        "outbox_id": outbox.get("id"),
        "kind": outbox.get("kind"),
        "payload": payload,
        "status": "dispatched",
        "terminal": False,
        "error": "",
        "pr_url": pr_url,
    }


def dispatch_outbox_item(cfg: ResolvedConfig, outbox: dict[str, Any]) -> dict[str, Any]:
    kind = str(outbox.get("kind") or "").strip()
    outbox_id = int(outbox.get("id") or 0)
    payload = dict(outbox.get("payload") or {})

    if kind == "github_project.status_update":
        result = _dispatch_status_update(cfg, outbox)
        result.setdefault("payload", payload)
        return result
    if kind == "github_issue.comment":
        result = _dispatch_issue_comment(cfg, outbox)
        result.setdefault("payload", payload)
        return result
    if kind == "github_pull_request.create":
        result = _dispatch_pull_request_create(cfg, outbox)
        result.setdefault("payload", payload)
        return result
    return {
        "outbox_id": outbox_id,
        "kind": kind,
        "payload": payload,
        "status": "failed",
        "terminal": True,
        "error": f"Unsupported outbox kind: {kind or 'unknown'}",
    }


def dispatch_outbox_tick(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int = DEFAULT_OUTBOX_BATCH_LIMIT,
) -> dict[str, Any]:
    store = _coordination_store(cfg, coordination)
    reaped = store.reap_stale_outbox_claims()
    claimed = store.claim_pending_outbox(limit=limit)
    items: list[dict[str, Any]] = []
    dispatched = 0
    retried = 0
    failed = 0

    if not claimed:
        return {
            "reaped": len(reaped),
            "claimed": 0,
            "dispatched": 0,
            "retried": 0,
            "failed": 0,
            "items": [],
        }

    github_connected = False
    try:
        ensure_github_mcp_connected(cfg)
        github_connected = True
    except Exception as exc:
        error = str(exc).strip() or "Failed to connect GitHub MCP"
        for outbox in claimed:
            outbox_id = int(outbox.get("id") or 0)
            attempts = int(outbox.get("attempts") or 0)
            terminal = attempts >= DEFAULT_OUTBOX_MAX_ATTEMPTS
            store.retry_outbox(outbox_id, error=error, terminal=terminal)
            items.append(
                {
                    "outbox_id": outbox_id,
                    "kind": outbox.get("kind"),
                    "payload": dict(outbox.get("payload") or {}),
                    "status": "failed" if terminal else "retry",
                    "terminal": terminal,
                    "error": error,
                }
            )
            if terminal:
                failed += 1
            else:
                retried += 1
        return {
            "reaped": len(reaped),
            "claimed": len(claimed),
            "dispatched": dispatched,
            "retried": retried,
            "failed": failed,
            "items": items,
        }

    try:
        for outbox in claimed:
            result = dispatch_outbox_item(cfg, outbox)
            outbox_id = int(result.get("outbox_id") or 0)
            status = str(result.get("status") or "").strip().lower()
            terminal = bool(result.get("terminal"))
            error = str(result.get("error") or "").strip()
            if status == "dispatched":
                store.complete_outbox(outbox_id)
                dispatched += 1
            else:
                if terminal:
                    store.retry_outbox(outbox_id, error=error or "outbox dispatch failed", terminal=True)
                    failed += 1
                else:
                    store.retry_outbox(outbox_id, error=error or "outbox dispatch failed", terminal=False)
                    retried += 1
            items.append(result)
    finally:
        if github_connected:
            try:
                ensure_github_mcp_disconnected(cfg)
            except Exception:
                pass

    return {
        "reaped": len(reaped),
        "claimed": len(claimed),
        "dispatched": dispatched,
        "retried": retried,
        "failed": failed,
        "items": items,
    }


def run_outbox_dispatcher(
    cfg: ResolvedConfig,
    *,
    coordination: CoordinationStore | None = None,
    limit: int = DEFAULT_OUTBOX_BATCH_LIMIT,
    sleep_seconds: float | None = None,
    stop_event: Any | None = None,
    once: bool = False,
) -> dict[str, Any]:
    interval = float(sleep_seconds or outbox_dispatcher_interval(cfg))
    last_summary: dict[str, Any] = {
        "reaped": 0,
        "claimed": 0,
        "dispatched": 0,
        "retried": 0,
        "failed": 0,
        "items": [],
    }
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        last_summary = dispatch_outbox_tick(cfg, coordination=coordination, limit=limit)
        if once:
            break
        if stop_event is not None and stop_event.is_set():
            break
        if any(int(last_summary.get(key) or 0) for key in ("claimed", "dispatched", "retried", "failed", "reaped")):
            time.sleep(min(1.0, interval))
        else:
            time.sleep(interval)
    return last_summary
