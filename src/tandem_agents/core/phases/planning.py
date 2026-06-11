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

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.planning")


def _remote_code_task_requires_worker_execution(task: dict[str, Any]) -> bool:
    """Remote code tasks need an explicit worker verdict, not file-presence proof."""
    source = task.get("source") if isinstance(task, dict) else {}
    source_type = str(source.get("type") or "").strip() if isinstance(source, dict) else ""
    execution_kind = str(task.get("execution_kind") or "").strip()
    return execution_kind == "code_edit" and source_type in {"linear", "github_project"}


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from model output (imported from runner_core)."""
    # Import here to avoid circular imports; runner_core is the owner of this util
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415
    return _rc._extract_json(text)


def _prepare_subtasks(ctx: RunContext) -> tuple[list[str], list[dict[str, Any]]]:
    """Call the private runner_core subtask-preparation helper.

    Returns (discovered_files, subtasks).  Kept as a thin bridge so callers
    don't need to import the private helper directly.
    """
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415
    from pathlib import Path
    # Dispatch concurrency is limited later. When swarm is disabled, preserve
    # manager subtasks and run them serially instead of compacting a broad plan
    # into one prompt that can exhaust the engine iteration/timeout budget.
    planning_subtask_limit = max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1)
    return _rc._prepare_subtasks_with_discovery(
        ctx.task,
        ctx.manager_plan,
        Path(ctx.repo.get("path") or "."),
        planning_subtask_limit,
        merge_manager_subtasks=ctx.cfg.swarm.enabled,
    )


def _carry_forward_partial_diff_artifacts(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    artifacts = repair.get("partial_diff_artifacts") if isinstance(repair, dict) else []
    if not isinstance(artifacts, list) or not artifacts:
        return
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        patch_path = str(artifact.get("patch_path") or "").strip()
        if not patch_path:
            continue
        target_subtask_id = str(artifact.get("subtask_id") or "").strip()
        for subtask in subtasks:
            subtask_id = str(subtask.get("id") or "").strip()
            if target_subtask_id and subtask_id and target_subtask_id != subtask_id and len(subtasks) > 1:
                continue
            subtask["carry_forward_patch"] = patch_path
            existing_scope_note = str(subtask.get("scope_note") or "").strip()
            carry_note = (
                "ACA will apply the preserved partial worker diff before this retry so the worker can continue "
                "from the previous attempt instead of repeating it."
            )
            subtask["scope_note"] = f"{existing_scope_note}\n{carry_note}".strip()
            break


def _completed_repair_subtask_ids(ctx: RunContext) -> set[str]:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    raw_ids = repair.get("completed_subtask_ids") if isinstance(repair, dict) else []
    if not isinstance(raw_ids, list):
        return set()
    return {str(item).strip() for item in raw_ids if str(item).strip()}


def _completed_repair_worker_results(
    ctx: RunContext,
    recorded_subtask_ids: set[str],
) -> list[dict[str, Any]]:
    """Return successful prior-attempt worker results missing from this retry plan."""
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415

    completed_ids = _completed_repair_subtask_ids(ctx)
    if not completed_ids:
        return []
    missing_ids = {subtask_id for subtask_id in completed_ids if subtask_id not in recorded_subtask_ids}
    if not missing_ids:
        return []
    workers = ctx.blackboard.get("workers") if isinstance(ctx.blackboard, dict) else []
    if not isinstance(workers, list):
        return []
    carried: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in reversed(workers):
        if not isinstance(result, dict):
            continue
        subtask_id = str(result.get("subtask_id") or "").strip()
        if not subtask_id or subtask_id not in missing_ids or subtask_id in seen:
            continue
        status = _rc._normalized_text(result.get("status"))
        if status not in {"completed", "skipped_existing", "tolerated_failure"} and not result.get("verified_existing"):
            continue
        cloned = dict(result)
        cloned["worker_id"] = f"repo-check-{subtask_id}"
        cloned["subtask_index"] = 0
        cloned["status"] = "skipped_existing"
        cloned["returncode"] = 0
        cloned["worktree"] = str(ctx.repo_path)
        cloned["write_required"] = False
        cloned["verified_existing"] = True
        cloned["output_excerpt"] = (
            "Subtask carried forward from a completed repair-loop worker even though "
            "the retry manager narrowed the current plan away from this subtask."
        )
        carried.append(cloned)
        seen.add(subtask_id)
    carried.reverse()
    return carried


def run_manager_prompt(ctx: RunContext) -> None:
    """Execute the manager (planning) prompt and populate ctx.manager_plan.

    Mutates:
        ctx.manager_plan   -- parsed plan dict (summary, subtasks, risks, tests)
        ctx.blackboard     -- updated with manager_plan key
    """
    from src.tandem_agents.core.engine.engine import engine_env, engine_session_provider_model
    from src.tandem_agents.core.execution.worker import stream_tandem_prompt
    from src.tandem_agents.core.repository.repo_truth import repo_context_summary
    from src.tandem_agents.core.engine.prompts import build_manager_prompt
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    manager_model_selection = engine_session_provider_model(ctx.cfg, "manager")
    manager_provider = manager_model_selection["provider"]
    manager_model = manager_model_selection["model"]

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
    with _rc._coordination_heartbeat(ctx, phase="planning"):
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
    if manager_result.get("engine") or manager_result.get("blocker_kind"):
        ctx.blackboard["manager_engine"] = {
            "engine": manager_result.get("engine") or {},
            "failure_reason": manager_result.get("failure_reason") or "",
            "blocker_kind": manager_result.get("blocker_kind") or "",
            "recovery_action": manager_result.get("recovery_action") or "",
        }
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
    from src.tandem_agents.core.repository.repo_truth import (
        file_is_readable,
        subtask_satisfied,
    )
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot
    from pathlib import Path
    from src.tandem_agents.core.task_contract import task_plan_validation

    repo_path = ctx.repo_path
    discovered_files, subtasks = _prepare_subtasks(ctx)
    _carry_forward_partial_diff_artifacts(ctx, subtasks)

    ctx.planned_subtasks = subtasks
    ctx.pending_subtasks = []
    current_expected_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    sticky_expected_files = _rc._sticky_expected_repo_files(ctx.blackboard, current_expected_files)
    sticky_missing_from_plan = [path for path in sticky_expected_files if path not in current_expected_files]
    if sticky_missing_from_plan and ctx.planned_subtasks:
        first = ctx.planned_subtasks[0]
        for key in ("files", "target_files"):
            values = [str(entry).strip() for entry in (first.get(key) or []) if str(entry).strip()]
            for path in sticky_missing_from_plan:
                if path not in values:
                    values.append(path)
            first[key] = values
        existing_scope_note = str(first.get("scope_note") or "").strip()
        sticky_note = (
            "ACA kept these expected files from an earlier retry attempt because later manager plans "
            "must not narrow the run contract: "
            + ", ".join(sticky_missing_from_plan)
            + "."
        )
        first["scope_note"] = f"{existing_scope_note}\n{sticky_note}".strip()
    plan_validation = task_plan_validation(ctx.task, subtasks)
    ctx.blackboard["task_plan_validation"] = plan_validation
    force_worker_execution = (
        _remote_code_task_requires_worker_execution(ctx.task)
        or bool(getattr(ctx, "_manager_fallback_required", False))
        or _rc._task_mentions_external_pr_candidates(ctx.task)
    )
    completed_repair_subtask_ids = _completed_repair_subtask_ids(ctx)
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
        carried_forward_success = (
            str(subtask.get("id") or "").strip() in completed_repair_subtask_ids
            and subtask_satisfied(repo_path, subtask)
        )
        subtask["pre_satisfied"] = (
            True
            if carried_forward_success
            else (False if force_worker_execution else subtask_satisfied(repo_path, subtask))
        )
        subtask["write_required"] = not subtask["pre_satisfied"]

        if subtask["pre_satisfied"]:
            skip_reason = (
                "carried forward from a completed repair-loop worker"
                if carried_forward_success
                else "already satisfied"
            )
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
                    "Subtask skipped because it was "
                    f"{skip_reason} and its target files are readable in the base repository."
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
                {"subtask_id": subtask["id"], "reason": skip_reason},
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
        else:
            ctx.pending_subtasks.append(subtask)

    recorded_subtask_ids = {
        str(result.get("subtask_id") or "").strip()
        for result in ctx.worker_results
        if str(result.get("subtask_id") or "").strip()
    }
    for carried_result in _completed_repair_worker_results(ctx, recorded_subtask_ids):
        _rc._record_worker_result(ctx.blackboard, ctx.worker_results, carried_result)
        append_event(
            ctx.layout["events"],
            "worker.skipped",
            ctx.run_id,
            {
                "subtask_id": carried_result["subtask_id"],
                "reason": "carried forward from a previous repair-loop plan",
            },
            task_id=ctx.task.get("task_id"),
            role="worker",
            repo={"path": ctx.repo.get("path")},
        )

    ctx.expected_repo_files = _rc._sticky_expected_repo_files(
        ctx.blackboard,
        _rc._collect_expected_repo_files(ctx.planned_subtasks),
    )
    ctx.repo_validation = _rc._deterministic_repo_validation(repo_path, ctx.expected_repo_files)
    ctx.blackboard["repo_validation"] = ctx.repo_validation

    ctx.status["metrics"]["planned_workers"] = len(ctx.planned_subtasks)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    all_pre_satisfied = (
        not ctx.pending_subtasks
        and _rc._all_subtasks_verified_existing(ctx.planned_subtasks, ctx.worker_results, ctx.repo_validation, ctx.task)
    )
    if all_pre_satisfied:
        logger.info(
            "All %d subtask(s) pre-satisfied by existing repo files; skipping worker execution.",
            len(ctx.planned_subtasks),
        )
    return all_pre_satisfied
