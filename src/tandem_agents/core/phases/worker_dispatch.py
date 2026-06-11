"""phases/worker_dispatch.py -- Local worker pool execution and result collection.

This module owns the worker dispatch phase:
1. Register workers with the coordination store
2. Spin up a heartbeat thread to keep leases alive during execution
3. Execute the local ThreadPoolExecutor worker pool via ``_execute_local_worker_pool``
4. Collect results and apply tolerated-failure logic
5. Clean up stale worker registrations on completion

All worker state is written into the RunContext. No return value — the
caller continues with ctx.worker_results after this returns.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.worker_dispatch")


_TERMINAL_WORKER_BLOCKER_KINDS = {
    "approval_failed",
    "github_context_unavailable",
    "unsupported_task",
    "worker_corrupt_diff",
    "worker_no_diff",
}


def dispatch_workers(ctx: RunContext) -> None:
    """Execute the pending subtask worker pool and collect results.

    If ``ctx.pending_subtasks`` is empty this is a no-op (results already
    accumulated by ``pre_screen_subtasks`` for the pre-satisfied path).

    Mutates:
        ctx.worker_results     -- extended with results from pending subtasks
        ctx.repo_validation    -- refreshed after worker sync
        ctx.blackboard, ctx.status
    """
    if not ctx.pending_subtasks:
        logger.debug(
            "No pending subtasks; skipping worker dispatch (run_id=%s)", ctx.run_id
        )
        _post_dispatch_validation(ctx)
        return

    from src.tandem_agents.core.engine.engine import effective_tandem_provider
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot, write_status

    worker_provider, worker_model = ctx.cfg.provider_for_role("worker")
    worker_capabilities = {
        "mode": "local-worker-pool",
        "provider": worker_provider,
        "model": worker_model,
        "repository": ctx.repo.get("slug") or ctx.cfg.repository.slug,
        "worktree_mode": "single-host",
    }
    worker_lease_id = str(ctx.status.get("coordination", {}).get("lease_id") or "")

    # Transition status
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="worker_execution",
        phase_role="worker",
        run_status="running",
    )
    _rc._touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="worker_execution",
        ctx=ctx,
    )
    append_event(
        ctx.layout["events"],
        "swarm.spawned",
        ctx.run_id,
        {
            "planned_workers": len(ctx.planned_subtasks),
            "max_parallel": max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1),
            "spawned_workers": len(ctx.pending_subtasks),
        },
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    write_status(ctx.layout["status"], ctx.status)

    # --- Heartbeat thread ---
    active_workers_lock = threading.Lock()
    active_workers: set[str] = set()
    worker_heartbeat_stop = threading.Event()

    def _heartbeat_local_workers() -> None:
        sleep_s = max(1.0, float(ctx.cfg.coordination.heartbeat_interval_seconds or 1) / 2.0)
        while not worker_heartbeat_stop.wait(sleep_s):
            _rc._touch_coordination(
                ctx.coordination,
                run_id=ctx.run_id,
                lease_id=ctx.lease_id,
                lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                status="running",
                phase="worker_execution",
                ctx=ctx,
            )
            with active_workers_lock:
                ids = list(active_workers)
            for wid in ids:
                try:
                    ctx.coordination.heartbeat_worker(
                        wid,
                        host_id=ctx.claim_identity["host_id"],
                        role="worker",
                        status="busy",
                        capabilities=worker_capabilities,
                        current_run_id=ctx.run_id,
                        current_lease_id=worker_lease_id,
                    )
                except Exception:
                    logger.debug("Heartbeat failed for worker %s", wid, exc_info=True)

    def _on_result(result: dict[str, Any]) -> None:
        wid = str(result.get("worker_id") or "").strip()
        subtask_id = str(result.get("subtask_id") or "").strip()
        _rc._record_worker_result(ctx.blackboard, ctx.worker_results, result)
        for item in ctx.blackboard["subtasks"]:
            if item.get("id") == subtask_id:
                item["status"] = result.get("status") or "failed"
                break
        ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        write_status(ctx.layout["status"], ctx.status)
        if wid:
            ctx.coordination.register_worker(
                worker_id=wid,
                host_id=ctx.claim_identity["host_id"],
                role="worker",
                status="idle",
                capabilities=worker_capabilities,
                current_run_id=None,
                current_lease_id=None,
            )
            with active_workers_lock:
                active_workers.discard(wid)

    # Register all workers upfront
    for index, subtask in enumerate(ctx.pending_subtasks, start=1):
        wid = f"worker-{index}"
        ctx.coordination.register_worker(
            worker_id=wid,
            host_id=ctx.claim_identity["host_id"],
            role="worker",
            status="busy",
            capabilities=worker_capabilities,
            current_run_id=ctx.run_id,
            current_lease_id=worker_lease_id,
        )
        with active_workers_lock:
            active_workers.add(wid)

    heartbeat_thread = threading.Thread(target=_heartbeat_local_workers, daemon=True)
    heartbeat_thread.start()

    try:
        logger.info(
            "Dispatching %d worker(s) (run_id=%s)", len(ctx.pending_subtasks), ctx.run_id
        )
        new_results = _rc._execute_local_worker_pool(
            ctx.cfg,
            ctx.run_id,
            ctx.repo_path,
            ctx.run_dir,
            ctx.task,
            ctx.pending_subtasks,
            max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1),
            on_result=_on_result,
        )
        # Merge any results that bypassed _on_result
        for r in new_results:
            if not any(
                existing.get("worker_id") == r.get("worker_id")
                and existing.get("subtask_id") == r.get("subtask_id")
                for existing in ctx.worker_results
            ):
                ctx.worker_results.append(r)
    finally:
        worker_heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
        with active_workers_lock:
            lingering = list(active_workers)
            active_workers.clear()
        for wid in lingering:
            try:
                ctx.coordination.register_worker(
                    worker_id=wid,
                    host_id=ctx.claim_identity["host_id"],
                    role="worker",
                    status="idle",
                    capabilities=worker_capabilities,
                    current_run_id=None,
                    current_lease_id=None,
                )
            except Exception:
                logger.debug("Failed to unregister lingering worker %s", wid, exc_info=True)

    _apply_tolerated_failures(ctx)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)

    _post_dispatch_validation(ctx)


def _apply_tolerated_failures(ctx: RunContext) -> None:
    """Upgrade 'failed' results to 'tolerated_failure' when target files are present post-sync."""
    from src.tandem_agents.core.repository.repo_truth import subtask_satisfied
    from src.tandem_agents.core.execution import runner_core as _rc

    task_source = ctx.task.get("source") if isinstance(ctx.task, dict) else {}
    if isinstance(task_source, dict) and str(task_source.get("type") or "").strip() == "github_project":
        return
    if _rc._task_mentions_external_pr_candidates(ctx.task):
        return

    for result in ctx.worker_results:
        if result.get("status") != "failed":
            continue
        matching = next(
            (s for s in ctx.planned_subtasks if s["id"] == result.get("subtask_id")),
            None,
        )
        failure_reason = str(result.get("failure_reason") or "").upper()
        blocker_kind = str(result.get("blocker_kind") or "").lower()
        if (
            failure_reason.startswith("ENGINE_")
            or failure_reason.startswith("ENGINE_ERROR:")
            or blocker_kind.startswith("engine_")
            or blocker_kind in _TERMINAL_WORKER_BLOCKER_KINDS
            or (matching and (matching.get("pr_candidate_context") or matching.get("pr_candidate_refs")))
        ):
            continue
        if matching and subtask_satisfied(ctx.repo_path, matching):
            result["status"] = "tolerated_failure"
            result["verified_existing"] = True
            for item in ctx.blackboard["subtasks"]:
                if item.get("id") == result["subtask_id"]:
                    item["status"] = "tolerated_failure"
                    break
            _rc._append_blackboard_note(
                ctx.blackboard,
                f"Tolerated noisy worker `{result['worker_id']}` because its target files were present after sync.",
            )


def _post_dispatch_validation(ctx: RunContext) -> None:
    """Refresh repo_validation and coding_run_contract after worker execution."""
    from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    ctx.expected_repo_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    changed_files: list[str] = _rc._collect_worker_changed_files(ctx.worker_results)
    if _rc._task_mentions_external_pr_candidates(ctx.task):
        if changed_files:
            ctx.expected_repo_files = changed_files
    else:
        ctx.expected_repo_files = _rc._sticky_expected_repo_files(
            ctx.blackboard,
            ctx.expected_repo_files,
        )
        ctx.expected_repo_files = _rc._validation_expected_repo_files(
            ctx.repo_path,
            ctx.expected_repo_files,
            changed_files,
        )
        ctx.blackboard["expected_repo_files"] = ctx.expected_repo_files
    ctx.repo_validation = _rc._deterministic_repo_validation(ctx.repo_path, ctx.expected_repo_files)
    if changed_files:
        unexpected_files = _rc._pr_candidate_unexpected_changed_files(ctx.planned_subtasks, changed_files)
        if unexpected_files:
            ctx.repo_validation = dict(ctx.repo_validation)
            ctx.repo_validation["unexpected_files"] = unexpected_files
            ctx.repo_validation["ok"] = False
    coding_run_contract = build_coding_run_contract(
        run_id=ctx.run_id,
        task=ctx.task,
        repo_path=ctx.repo_path,
        branch_name=ctx.branch_name,
        expected_repo_files=ctx.expected_repo_files,
    )
    ctx.blackboard["repo_validation"] = ctx.repo_validation
    _rc._record_coding_run_contract(ctx.blackboard, coding_run_contract)

    if ctx.repo_validation.get("ok"):
        _rc._append_blackboard_note(
            ctx.blackboard, "Deterministic repo validation passed for expected files."
        )
    else:
        blocker = _rc._repo_validation_blocker_message(ctx.repo_validation)
        _rc._append_blackboard_note(
            ctx.blackboard,
            f"Deterministic repo validation found issues: {blocker or 'unknown issue'}",
        )
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
