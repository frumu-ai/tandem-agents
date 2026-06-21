"""phases/task_intake.py -- Task normalization, branch setup, and coordination claim.

This module owns the task-intake phase that runs after repository resolution:
1. Connect GitHub MCP for intake if required
2. Normalize the task from the configured task source
3. Derive the canonical run branch name and check it out
4. Register task and worker with the coordination store
5. Attempt to claim the task lease; block if already claimed
6. Initialize the run status dict and blackboard
7. Start the coordination run in the store

Returns the fully initialized RunContext ready for the planning loop, or
raises/blocks early if the claim fails.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.task_intake")


def run_task_intake(
    ctx: RunContext,
) -> dict[str, Any] | None:
    """Execute the task intake sequence and fully populate ctx.

    Populates:
        ctx.task, ctx.board, ctx.board_path, ctx.branch_name,
        ctx.claim_identity, ctx.status, ctx.blackboard,
        ctx.source_type, ctx.source_scope, ctx.remote_sync,
        ctx.execution_backend

    Returns:
        None on success (caller continues).
        A blocked-run result dict if the task claim fails (caller should
        ``return`` this immediately).
    """
    from src.tandem_agents.core.engine.engine import (
        checkout_run_worktree,
        task_run_branch_name,
        task_run_worktree_name,
    )
    from src.tandem_agents.core.integrations.github_mcp import github_mcp_scope, github_remote_sync_mode
    from src.tandem_agents.core.integrations.linear_mcp import linear_mcp_scope, linear_remote_sync_mode
    from src.tandem_agents.core.engine.coder_backend import coder_backend_mode
    from src.tandem_agents.core.repository.board import card_to_task, claim_card, save_board, select_card
    from src.tandem_agents.core.execution.run_lifecycle import block_run
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, initial_blackboard, initial_status, save_blackboard
    from src.tandem_agents.runtime.run_output import (
        set_status,
        write_blackboard_snapshot,
        write_board_snapshot,
    )
    from src.tandem_agents.runtime.task_sources import normalize_task
    from src.tandem_agents.core.task_contract import classify_task_execution_kind, task_contract_completeness
    from src.tandem_agents.core.execution.run_lifecycle import build_provider_config_dict, build_swarm_config_dict
    from src.tandem_agents.core.repository.repository import repository_status

    # 1. Source MCP for intake
    if ctx.cfg.task_source.type == "linear":
        source_scope_pre = linear_mcp_scope(ctx.cfg, ctx.cfg.task_source.type)
    else:
        source_scope_pre = github_mcp_scope(ctx.cfg, ctx.cfg.task_source.type)
    if source_scope_pre in {"intake_only", "intake_finalize", "always"} and ctx.cfg.task_source.type == "github_project":
        _rc._connect_github_for_phase(
            cfg=ctx.cfg,
            run_id=ctx.run_id,
            layout=ctx.layout,
            status=None,
            blackboard=None,
            event_type="github_mcp.connected_for_intake",
            required=True,
        )
    if source_scope_pre in {"intake_only", "intake_finalize", "always"} and ctx.cfg.task_source.type == "linear":
        _rc._connect_linear_for_phase(
            cfg=ctx.cfg,
            run_id=ctx.run_id,
            layout=ctx.layout,
            status=None,
            blackboard=None,
            event_type="linear_mcp.connected_for_intake",
            required=True,
        )

    # 2. Normalize task
    intake_started_at = time.monotonic()
    intake_payload = _task_source_intake_payload(ctx)
    append_event(
        ctx.layout["events"],
        "task_source.intake_started",
        ctx.run_id,
        intake_payload,
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    try:
        task, board, board_path = normalize_task(ctx.cfg, coordination=ctx.coordination)
    except Exception as exc:
        append_event(
            ctx.layout["events"],
            "task_source.intake_failed",
            ctx.run_id,
            {
                **intake_payload,
                "duration_ms": int((time.monotonic() - intake_started_at) * 1000),
                "error": str(exc),
            },
            role="manager",
            repo={"path": ctx.repo.get("path")},
        )
        raise
    if board_path is None:
        board_path = ctx.layout["board"]
    ctx.task = task
    ctx.board = board
    ctx.board_path = board_path
    write_board_snapshot(ctx.run_dir, board)
    task_contract = task.get("task_contract") or {}
    contract_completeness = task.get("contract_completeness") or task_contract_completeness(task)
    dependency_status = task.get("dependency_status") or {}

    # 3. Initial blackboard
    ctx.blackboard = initial_blackboard(
        ctx.run_id,
        task,
        ctx.repo,
        build_provider_config_dict(ctx.cfg),
        ctx.engine,
        {
            "enabled": ctx.cfg.swarm.enabled,
            "shared_model": ctx.cfg.swarm.shared_model,
            "max_workers": ctx.cfg.swarm.max_workers,
        },
    )
    ctx.blackboard["task_contract"] = task_contract
    ctx.blackboard["program_goal"] = task.get("program_goal") or task_contract.get("program_goal")
    ctx.blackboard["local_goal"] = task.get("local_goal") or task_contract.get("local_goal")
    ctx.blackboard["dependency_status"] = dependency_status
    ctx.blackboard["task_source_intake"] = {
        **intake_payload,
        "duration_ms": int((time.monotonic() - intake_started_at) * 1000),
        "identifier": (task.get("source") or {}).get("identifier") if isinstance(task.get("source"), dict) else None,
        "issue_id": (task.get("source") or {}).get("issue_id") if isinstance(task.get("source"), dict) else None,
        "selected_status": (task.get("source") or {}).get("initial_status_name") if isinstance(task.get("source"), dict) else None,
    }
    ctx.blackboard["contract_completeness"] = contract_completeness
    ctx.blackboard["verification_plan"] = {
        "commands": list(task.get("verification_commands") or task_contract.get("verification_commands") or []),
    }
    ctx.blackboard["expected_deliverables"] = {
        "deliverables": list(task.get("deliverables") or task_contract.get("deliverables") or []),
        "target_files": list(task.get("target_files") or task_contract.get("target_files") or []),
        "acceptance_criteria": list(task.get("acceptance_criteria") or task_contract.get("acceptance_criteria") or []),
    }
    ctx.blackboard["execution_kind"] = task.get("execution_kind") or classify_task_execution_kind(task)
    _rc._record_review_policy(ctx.blackboard, ctx.cfg)
    _rc._append_blackboard_note(
        ctx.blackboard,
        "Task contract loaded during intake; verification and dependency status are recorded before claim.",
    )
    append_event(
        ctx.layout["events"],
        "task_source.intake_completed",
        ctx.run_id,
        dict(ctx.blackboard["task_source_intake"]),
        task_id=task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    # 3b. Early block for contract or dependency issues
    blocked_kind = None
    blocked_message = None
    if not contract_completeness.get("ok", True):
        blocked_kind = str(contract_completeness.get("blocker_kind") or "contract_incomplete")
        blocked_message = str(contract_completeness.get("blocker_message") or "Task contract is incomplete.")
    elif dependency_status.get("blocked"):
        blocked_kind = "dependency_blocked"
        blocked_message = str(dependency_status.get("blocked_reason") or "Task dependencies are unresolved.")
    if blocked_kind and blocked_message:
        registered_task = ctx.coordination.register_task(task, repo=ctx.repo, status="blocked")
        task_key = str(registered_task.get("task_key") or task.get("task_key") or "").strip()
        if task_key:
            try:
                ctx.coordination.mark_task_blocked(
                    task_key,
                    run_id=ctx.run_id,
                    lease_id=None,
                    worker_id=None,
                    host_id=None,
                    reason=blocked_message,
                )
            except Exception:
                logger.warning("Failed to mark blocked task in coordination (run_id=%s)", ctx.run_id, exc_info=True)
        _rc._append_blackboard_note(ctx.blackboard, f"Blocked during intake: {blocked_message}")
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        return block_run(
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            layout=ctx.layout,
            cfg=ctx.cfg,
            task=task,
            repo=ctx.repo,
            engine=ctx.engine,
            phase="task_resolution",
            kind=blocked_kind,
            message=blocked_message,
            phase_detail=blocked_message,
            coordination=ctx.coordination,
        )

    # 4. Claim identity
    ctx.claim_identity = _rc._task_claim_identity(ctx.cfg, task)

    # 5. Branch/worktree setup
    source_repo = dict(ctx.repo or {})
    repo_slug = source_repo.get("slug") or ctx.cfg.repository.slug
    ctx.branch_name = task_run_branch_name(task, ctx.run_id, repo_slug)
    source_repo_path = ctx.repo_path
    run_worktree_path = ctx.run_dir / "repo" / task_run_worktree_name(task, ctx.run_id, repo_slug)
    run_repo_path = checkout_run_worktree(ctx.cfg, source_repo_path, run_worktree_path, ctx.branch_name)
    active_repo = repository_status(
        run_repo_path,
        ctx.cfg.repository.remote_name,
        ctx.cfg.repository.default_branch,
    )
    for key in ("slug", "clone_url", "hint", "credential_file"):
        if source_repo.get(key):
            active_repo[key] = source_repo[key]
    active_repo["source_path"] = str(source_repo_path)
    ctx.repo = active_repo
    def attach_active_repo_metadata(target_task: dict) -> dict:
        task_repo = dict(target_task.get("repo") or {})
        for key in ("slug", "clone_url", "hint", "credential_file"):
            if active_repo.get(key):
                task_repo[key] = active_repo[key]
        task_repo.update(
            {
                "path": str(run_repo_path),
                "source_path": str(source_repo_path),
                "branch": ctx.branch_name,
                "default_branch": ctx.cfg.repository.default_branch,
                "remote_name": ctx.cfg.repository.remote_name,
            }
        )
        target_task["repo"] = task_repo
        return target_task

    task = attach_active_repo_metadata(task)
    ctx.blackboard["task"] = task
    ctx.blackboard["repo"] = dict(ctx.repo)
    append_event(
        ctx.layout["events"],
        "repo.run_worktree_checked_out",
        ctx.run_id,
        {
            "source_repo_path": str(source_repo_path),
            "run_repo_path": str(run_repo_path),
            "branch_name": ctx.branch_name,
        },
        task_id=task.get("task_id"),
        role="manager",
        repo={"path": str(run_repo_path)},
    )

    # 6. Register task + worker
    registered_task = ctx.coordination.register_task(task, repo=ctx.repo, status="queued")
    ctx.coordination.register_worker(
        worker_id=ctx.claim_identity["worker_id"],
        host_id=ctx.claim_identity["host_id"],
        role=ctx.claim_identity["role"],
        status="idle",
        capabilities={
            "mode": "coordinator",
            "source_type": ctx.claim_identity["source_type"],
            "repository": ctx.repo.get("slug") or ctx.cfg.repository.slug,
        },
    )

    # 7. Claim task
    claim_result = ctx.coordination.claim_task(
        task,
        run_id=ctx.run_id,
        worker_id=ctx.claim_identity["worker_id"],
        host_id=ctx.claim_identity["host_id"],
        role=ctx.claim_identity["role"],
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        branch_name=ctx.branch_name,
        repo=ctx.repo,
    )
    if not claim_result.get("claimed"):
        if str(claim_result.get("reason") or "").strip() == "dependency_blocked":
            blocked_message = str(
                claim_result.get("blocked_reason")
                or (claim_result.get("dependency_status") or {}).get("blocked_reason")
                or "Task dependencies are unresolved."
            ).strip()
            task_key = str((claim_result.get("task") or {}).get("task_key") or registered_task.get("task_key") or "").strip()
            if task_key:
                try:
                    ctx.coordination.mark_task_blocked(
                        task_key,
                        run_id=ctx.run_id,
                        lease_id=None,
                        worker_id=None,
                        host_id=None,
                        reason=blocked_message,
                    )
                except Exception:
                    logger.warning("Failed to mark dependency-blocked task in coordination (run_id=%s)", ctx.run_id, exc_info=True)
            return block_run(
                run_id=ctx.run_id,
                run_dir=ctx.run_dir,
                layout=ctx.layout,
                cfg=ctx.cfg,
                task=task,
                repo=ctx.repo,
                engine=ctx.engine,
                phase="task_resolution",
                kind="dependency_blocked",
                message=blocked_message,
                phase_detail=blocked_message,
                coordination=ctx.coordination,
            )
        # A failed claimant must not refresh a lease owned by another run.
        # Otherwise repeated retries can keep an interrupted worker alive.
        logger.warning("Task already leased; blocking run %s", ctx.run_id)
        return block_run(
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            layout=ctx.layout,
            cfg=ctx.cfg,
            task=task,
            repo=ctx.repo,
            engine=ctx.engine,
            phase="task_resolution",
            kind="coordination",
            message="Task is already leased by another worker.",
            phase_detail="task already has an active lease",
            coordination=ctx.coordination,
        )

    # 8. Claim the board card if present
    if board.get("cards") and task.get("task_id"):
        card = select_card(board, task["task_id"])
        if card:
            claim_card(board, card["id"], ctx.run_id, actor="manager")
            ctx.task = card_to_task(card, board_path=board_path)
            ctx.task["source"]["type"] = ctx.task["source"].get("type") or ctx.cfg.task_source.type
            ctx.task["source"].setdefault("board_path", str(board_path))
            ctx.task["execution_kind"] = task.get("execution_kind") or classify_task_execution_kind(task)
            ctx.task = attach_active_repo_metadata(ctx.task)
            append_event(
                ctx.layout["events"], "task.claimed", ctx.run_id,
                {"card_id": card["id"], "lane": card.get("lane")},
                task_id=ctx.task.get("task_id"),
            )
            save_board(board_path, board)
    else:
        ctx.task.setdefault("source", {})
        ctx.task["source"].setdefault("board_path", str(board_path))

    ctx.blackboard["task"] = ctx.task
    ctx.blackboard["repo"] = dict(ctx.repo)
    write_board_snapshot(ctx.run_dir, board)

    # 9. Source / sync metadata
    task = ctx.task
    source_type = str((task.get("source") or {}).get("type") or ctx.cfg.task_source.type)
    ctx.source_type = source_type
    if source_type == "linear":
        ctx.source_scope = linear_mcp_scope(ctx.cfg, source_type)
        ctx.remote_sync = linear_remote_sync_mode(ctx.cfg, source_type)
    else:
        ctx.source_scope = github_mcp_scope(ctx.cfg, source_type)
        ctx.remote_sync = github_remote_sync_mode(ctx.cfg, source_type)
    configured_remote_sync = str(ctx.cfg.github_mcp.remote_sync or "off").strip().lower()
    configured_scope = str(ctx.cfg.github_mcp.scope or "none").strip().lower()
    if (
        source_type == "github_project"
        and configured_remote_sync != "off"
        and configured_scope in {"intake_finalize", "always"}
        and (ctx.source_scope == "none" or ctx.remote_sync == "off")
    ):
        return block_run(
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            layout=ctx.layout,
            cfg=ctx.cfg,
            task=task,
            repo=ctx.repo,
            engine=ctx.engine,
            phase="github_sync",
            kind="github_mcp_disabled",
            message=(
                "GitHub Project remote sync is configured for this ACA task source, "
                "but GitHub MCP is not enabled. Refusing to mark the task complete "
                "without updating GitHub."
            ),
            coordination=ctx.coordination,
        )
    configured_linear_remote_sync = str(ctx.cfg.linear_mcp.remote_sync or "off").strip().lower()
    configured_linear_scope = str(ctx.cfg.linear_mcp.scope or "none").strip().lower()
    if (
        source_type == "linear"
        and configured_linear_remote_sync != "off"
        and configured_linear_scope in {"intake_finalize", "always"}
        and (ctx.source_scope == "none" or ctx.remote_sync == "off")
    ):
        return block_run(
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            layout=ctx.layout,
            cfg=ctx.cfg,
            task=task,
            repo=ctx.repo,
            engine=ctx.engine,
            phase="linear_sync",
            kind="linear_mcp_disabled",
            message=(
                "Linear remote sync is configured for this ACA task source, "
                "but Linear MCP is not enabled. Refusing to mark the task complete "
                "without updating Linear."
            ),
            coordination=ctx.coordination,
        )

    # 10. Initial status dict
    ctx.status = initial_status(
        ctx.run_id,
        task,
        ctx.repo,
        ctx.engine,
        build_provider_config_dict(ctx.cfg),
        build_swarm_config_dict(ctx.cfg),
        ctx.run_dir,
    )
    ctx.status["coordination"] = {
        "worker_id": ctx.claim_identity["worker_id"],
        "host_id": ctx.claim_identity["host_id"],
        "lease_id": claim_result["lease"]["lease_id"] if claim_result.get("lease") else None,
        "task_key": (registered_task.get("task_key") if registered_task else None),
        "lease_expires_at_ms": (
            claim_result["lease"].get("expires_at_ms") if claim_result.get("lease") else None
        ),
    }
    if source_type == "linear":
        ctx.status = _rc._init_linear_mcp_status(
            ctx.status, ctx.layout, scope=ctx.source_scope, remote_sync=ctx.remote_sync
        )
    else:
        ctx.status = _rc._init_github_mcp_status(
            ctx.status, ctx.layout, scope=ctx.source_scope, remote_sync=ctx.remote_sync
        )
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="task_resolution", run_status="running", run_started=True
    )

    ctx.coordination.update_run(
        ctx.run_id,
        status="running",
        phase="task_resolution",
        lease_id=ctx.status["coordination"]["lease_id"],
        branch_name=ctx.branch_name,
        started=True,
    )
    _rc._touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.status["coordination"]["lease_id"],
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="task_resolution",
    )
    task_key, lease_id, worker_id, host_id, lease_expires_at_ms = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id and lease_expires_at_ms is not None:
        ctx.coordination.mark_task_active(
            task_key,
            run_id=ctx.run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            lease_expires_at_ms=int(lease_expires_at_ms),
            reason="execution started",
        )

    # 11. Source and runtime notes
    _rc._append_blackboard_note(
        ctx.blackboard,
        f"{'Linear' if source_type == 'linear' else 'GitHub'} MCP scope: `{ctx.source_scope}`; remote sync: `{ctx.remote_sync}`.",
    )

    # 12. Execution backend
    execution_kind = str(ctx.task.get("execution_kind") or classify_task_execution_kind(ctx.task)).strip()
    if execution_kind == "linear_comment":
        ctx.execution_backend = "linear_comment"
    elif execution_kind == "github_pr_action":
        ctx.execution_backend = "github_pr_action"
    else:
        ctx.execution_backend = coder_backend_mode(ctx.cfg, ctx.task, ctx.repo)
    ctx.blackboard["execution_kind"] = execution_kind
    ctx.blackboard["execution_backend"] = ctx.execution_backend
    _rc._append_blackboard_note(ctx.blackboard, f"Execution backend: `{ctx.execution_backend}`.")
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    logger.info(
        "Task intake complete: task=%s backend=%s branch=%s (run_id=%s)",
        task.get("title"),
        ctx.execution_backend,
        ctx.branch_name,
        ctx.run_id,
    )
    return None  # success


def _task_source_intake_payload(ctx: RunContext) -> dict[str, Any]:
    cfg = ctx.cfg
    payload: dict[str, Any] = {
        "source_type": cfg.task_source.type,
        "team": cfg.task_source.team,
        "project": cfg.task_source.project,
        "filters": {
            "statuses": cfg.task_source.statuses,
            "labels": cfg.task_source.labels,
            "query": cfg.task_source.query,
            "item": cfg.task_source.item or cfg.task_source.url,
        },
    }
    if cfg.task_source.type == "linear":
        from src.tandem_agents.core.integrations.linear_mcp import linear_mcp_server_name

        payload["mcp_server"] = linear_mcp_server_name(cfg)
    return payload
