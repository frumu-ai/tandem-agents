"""phases/planning.py -- Manager prompt, subtask decomposition, and pre-satisfaction.

This module owns the manager (planning) prompt phase:
1. Build and run the Tandem manager prompt
2. Parse the JSON plan (or fall back to a plain-text plan)
3. Decompose the plan into subtasks via ``derive_subtasks``
4. Pre-screen subtasks against the repository to discover already-satisfied ones
5. Detect the "no-targets" early-exit condition
6. Prepare the pending-subtask list for worker dispatch

The planning phase repeats inside the repair loop (max_loops iterations).
"""
from __future__ import annotations

import logging
from typing import Any

from src.aca.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.planning")


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from model output (imported from runner_core)."""
    # Import here to avoid circular imports; runner_core is the owner of this util
    from src.aca.core.execution import runner_core as _rc  # noqa: PLC0415
    return _rc._extract_json(text)


def _prepare_subtasks(ctx: RunContext) -> tuple[list[str], list[dict[str, Any]]]:
    """Call the private runner_core subtask-preparation helper.

    Returns (discovered_files, subtasks).  Kept as a thin bridge so callers
    don't need to import the private helper directly.
    """
    from src.aca.core.execution import runner_core as _rc  # noqa: PLC0415
    from pathlib import Path
    return _rc._prepare_subtasks_with_discovery(
        ctx.task,
        ctx.manager_plan,
        Path(ctx.repo.get("path") or "."),
        ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1,
    )


def run_manager_prompt(ctx: RunContext) -> None:
    """Execute the manager (planning) prompt and populate ctx.manager_plan.

    Mutates:
        ctx.manager_plan   -- parsed plan dict (summary, subtasks, risks, tests)
        ctx.blackboard     -- updated with manager_plan key
    """
    from src.aca.core.engine.engine import effective_tandem_provider, engine_env
    from src.aca.core.execution.worker import stream_tandem_prompt
    from src.aca.core.repository.repo_truth import repo_context_summary
    from src.aca.core.engine.prompts import build_manager_prompt
    from src.aca.runtime.runstate import append_event, save_blackboard
    from src.aca.runtime.run_output import write_blackboard_snapshot

    manager_provider, manager_model = ctx.cfg.provider_for_role("manager")
    manager_cli_provider = effective_tandem_provider(manager_provider, ctx.cfg)

    manager_prompt = build_manager_prompt(
        ctx.run_id,
        ctx.task,
        ctx.repo,
        ctx.cfg,
        repo_context=repo_context_summary(ctx.repo_path, ctx.task),
        previous_feedback=getattr(ctx, "_previous_feedback", None),
    )

    append_event(
        ctx.layout["events"],
        "manager.started",
        ctx.run_id,
        {"role": "manager"},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )

    logger.info("Running manager prompt (run_id=%s)", ctx.run_id)
    manager_result = stream_tandem_prompt(
        ctx.cfg,
        role="manager",
        prompt=manager_prompt,
        cwd=ctx.repo_path,
        provider=manager_provider,
        model=manager_model,
        env=engine_env(ctx.cfg),
        log_path=ctx.layout["logs"] / "manager.log",
        config_path=None,
    )

    ctx.manager_plan = _extract_json(manager_result["stdout"]) or {
        "summary": manager_result["stdout"][:1200],
        "subtasks": [],
        "risks": [],
        "tests": [],
    }
    ctx.blackboard["manager_plan"] = ctx.manager_plan
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    return manager_result


def pre_screen_subtasks(ctx: RunContext) -> bool:
    """Pre-screen planned subtasks against the repository.

    Separates each subtask into ``pending`` (needs work) or ``skipped_existing``
    (already satisfied by current repo state).

    Mutates:
        ctx.planned_subtasks   -- annotated with pre_satisfied and existing_files
        ctx.pending_subtasks   -- subtasks that need worker execution
        ctx.worker_results     -- extended with skipped-existing results
        ctx.expected_repo_files
        ctx.repo_validation
        ctx.blackboard

    Returns:
        True if all subtasks are pre-satisfied (no workers needed).
        False if there is work to do.
    """
    from src.aca.core.repository.repo_truth import (
        file_is_readable,
        subtask_satisfied,
    )
    from src.aca.core.execution import runner_core as _rc
    from src.aca.runtime.runstate import append_event, save_blackboard
    from src.aca.runtime.run_output import set_status, write_blackboard_snapshot
    from pathlib import Path
    from src.aca.core.task_contract import task_plan_validation

    repo_path = ctx.repo_path
    discovered_files, subtasks = _prepare_subtasks(ctx)

    ctx.planned_subtasks = subtasks
    ctx.pending_subtasks = []
    plan_validation = task_plan_validation(ctx.task, subtasks)
    ctx.blackboard["task_plan_validation"] = plan_validation
    if not plan_validation.get("ok", True):
        blocker_kind = str(plan_validation.get("blocker_kind") or "contract_incomplete")
        blocker_message = str(plan_validation.get("blocker_message") or "Subtask plan is incomplete or unsafe.")
        ctx.status = set_status(
            ctx.status,
            ctx.layout,
            phase="planning",
            phase_detail=blocker_message,
            run_status="blocked",
            blocker=(True, blocker_kind, blocker_message, "manager"),
        )
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        logger.warning(
            "Manager plan blocked during pre-screen (run_id=%s): %s",
            ctx.run_id,
            blocker_message,
        )
        return False

    for subtask in ctx.planned_subtasks:
        readable_existing = [
            rel_path
            for rel_path in (subtask.get("files") or [])
            if str(rel_path or "").strip()
            and (repo_path / str(rel_path).strip()).exists()
            and (repo_path / str(rel_path).strip()).is_file()
            and file_is_readable(repo_path / str(rel_path).strip())
        ]
        subtask["existing_files"] = readable_existing
        subtask["pre_satisfied"] = subtask_satisfied(repo_path, subtask)
        subtask["write_required"] = not subtask["pre_satisfied"]

        if subtask["pre_satisfied"]:
            skipped_result = {
                "worker_id": f"repo-check-{subtask['id']}",
                "subtask_index": 0,
                "subtask_id": subtask["id"],
                "title": subtask["title"],
                "status": "skipped_existing",
                "returncode": 0,
                "worktree": str(repo_path),
                "log_path": "",
                "output_excerpt": (
                    "Subtask skipped because all target files already exist "
                    "and are readable in the base repository."
                ),
                "write_required": False,
                "verified_existing": True,
            }
            _rc._record_worker_result(ctx.blackboard, ctx.worker_results, skipped_result)
            for item in ctx.blackboard["subtasks"]:
                if item.get("id") == subtask["id"]:
                    item["status"] = "skipped_existing"
                    item["write_required"] = False
                    break
            append_event(
                ctx.layout["events"],
                "worker.skipped",
                ctx.run_id,
                {"subtask_id": subtask["id"], "reason": "already satisfied"},
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
        else:
            ctx.pending_subtasks.append(subtask)

    ctx.expected_repo_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    ctx.repo_validation = _rc._deterministic_repo_validation(repo_path, ctx.expected_repo_files)
    ctx.blackboard["repo_validation"] = ctx.repo_validation

    ctx.status["metrics"]["planned_workers"] = len(ctx.planned_subtasks)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    all_pre_satisfied = (
        not ctx.pending_subtasks
        and _rc._all_subtasks_verified_existing(ctx.planned_subtasks, ctx.worker_results, ctx.repo_validation)
    )
    if all_pre_satisfied:
        logger.info(
            "All %d subtask(s) pre-satisfied by existing repo files; skipping worker execution.",
            len(ctx.planned_subtasks),
        )
    return all_pre_satisfied
