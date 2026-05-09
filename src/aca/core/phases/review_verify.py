"""phases/review_verify.py -- Review agent, test agent, and verification policy.

This module owns the review + test phase that follows worker execution:
1. Build and run the Tandem reviewer prompt
2. Build and run the Tandem tester prompt
3. Run deterministic repo validation (file presence + command checks)
4. Apply the verification policy to produce a structured outcome
5. Determine whether to retry, block, or proceed

No state is written to disk here — callers drive that.
"""
from __future__ import annotations

import logging
from typing import Any

from src.aca.core.phases.context import RunContext
from src.aca.core.verification.verification_policy import evaluate_verification_policy

logger = logging.getLogger("aca.phases.review_verify")


def run_review_and_test(ctx: RunContext) -> dict[str, Any]:
    """Execute the reviewer and tester prompts and verify the result.

    Mutates:
        ctx.review_result      -- raw result dict from reviewer stream_tandem_prompt
        ctx.test_result        -- raw result dict from tester stream_tandem_prompt
        ctx.repo_validation    -- updated with command-check results
        ctx.blackboard["verification"]

    Returns:
        A ``VerificationResult`` (from verification_policy) as a plain dict
        with keys: outcome, should_retry, review_blocker, test_blocker,
        repo_blocker, validation_blocker.
    """
    from src.aca.core.engine.engine import (
        effective_tandem_provider,
        engine_env,
        git_diff_stat,
    )
    from src.aca.core.execution.worker import stream_tandem_prompt
    from src.aca.core.engine.prompts import build_review_prompt, build_test_prompt
    from src.aca.core.repository.repo_truth import deterministic_repo_validation, extract_command_checks
    from src.aca.core.verification.coding_run_contract import build_coding_run_contract
    from src.aca.core.execution import runner_core as _rc
    from src.aca.runtime.runstate import append_event, save_blackboard
    from src.aca.runtime.run_output import set_status, write_blackboard_snapshot, write_status

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

    task_verification_commands = [
        str(entry).strip()
        for entry in _as_list(ctx.task.get("verification_commands"))
        if str(entry).strip()
    ]
    manager_command_checks = extract_command_checks(ctx.manager_plan)
    combined_command_checks: list[str] = []
    for command in task_verification_commands + manager_command_checks:
        if command not in combined_command_checks:
            combined_command_checks.append(command)

    # --- Transition to review phase ---
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="review", phase_role="reviewer", run_status="running"
    )
    _rc._touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="review",
        ctx=ctx,
    )
    task_key, lease_id, worker_id, host_id, lease_expires_at_ms = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id and lease_expires_at_ms is not None:
        ctx.coordination.mark_task_review(
            task_key,
            run_id=ctx.run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            lease_expires_at_ms=int(lease_expires_at_ms),
            reason="review phase started",
        )

    # --- Reviewer ---
    review_prompt = build_review_prompt(ctx.run_id, ctx.task, ctx.worker_results)
    review_provider, review_model = ctx.cfg.provider_for_role("reviewer")
    review_cli_provider = effective_tandem_provider(review_provider, ctx.cfg)
    _rc._role_provider_override_config(
        cfg=ctx.cfg,
        layout=ctx.layout,
        role="reviewer",
        provider=review_cli_provider,
        model=review_model,
    )
    logger.info("Running reviewer prompt (run_id=%s)", ctx.run_id)
    ctx.review_result = stream_tandem_prompt(
        ctx.cfg,
        role="reviewer",
        prompt=review_prompt,
        cwd=ctx.repo_path,
        provider=review_cli_provider,
        model=review_model,
        env=engine_env(ctx.cfg),
        log_path=ctx.layout["logs"] / "reviewer.log",
        config_path=None,
    )
    append_event(
        ctx.layout["events"],
        "review.completed" if ctx.review_result["returncode"] == 0 else "review.failed",
        ctx.run_id,
        {"returncode": ctx.review_result["returncode"]},
        task_id=ctx.task.get("task_id"),
        role="reviewer",
        repo={"path": ctx.repo.get("path")},
    )

    # --- Transition to test phase ---
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="test", phase_role="tester", run_status="running"
    )
    _rc._touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="test",
        ctx=ctx,
    )
    _rc._wait_for_engine(ctx.cfg)

    # --- Tester ---
    test_prompt = build_test_prompt(ctx.run_id, ctx.task, ctx.repo, ctx.worker_results)
    test_provider, test_model = ctx.cfg.provider_for_role("tester")
    test_cli_provider = effective_tandem_provider(test_provider, ctx.cfg)
    _rc._role_provider_override_config(
        cfg=ctx.cfg,
        layout=ctx.layout,
        role="tester",
        provider=test_cli_provider,
        model=test_model,
    )
    logger.info("Running tester prompt (run_id=%s)", ctx.run_id)
    ctx.test_result = stream_tandem_prompt(
        ctx.cfg,
        role="tester",
        prompt=test_prompt,
        cwd=ctx.repo_path,
        provider=test_cli_provider,
        model=test_model,
        env=engine_env(ctx.cfg),
        log_path=ctx.layout["logs"] / "tester.log",
        config_path=None,
    )
    append_event(
        ctx.layout["events"],
        "test.completed" if ctx.test_result["returncode"] == 0 else "test.failed",
        ctx.run_id,
        {"returncode": ctx.test_result["returncode"]},
        task_id=ctx.task.get("task_id"),
        role="tester",
        repo={"path": ctx.repo.get("path")},
    )

    # --- Deterministic repo validation ---
    ctx.repo_validation = _rc._deterministic_repo_validation(ctx.repo_path, ctx.expected_repo_files)
    if combined_command_checks:
        ctx.repo_validation = deterministic_repo_validation(
            ctx.repo_path,
            ctx.expected_repo_files,
            command_checks=combined_command_checks,
        )
    coding_run_contract = build_coding_run_contract(
        run_id=ctx.run_id,
        task=ctx.task,
        repo_path=ctx.repo_path,
        branch_name=ctx.branch_name,
        expected_repo_files=ctx.expected_repo_files,
    )
    if coding_run_contract.requires_minimal_verification_before_handoff and not combined_command_checks:
        ctx.repo_validation = dict(ctx.repo_validation)
        ctx.repo_validation["verification_missing"] = True
        ctx.repo_validation["ok"] = False
    ctx.blackboard["task_contract"] = ctx.task.get("task_contract") or {}
    ctx.blackboard["program_goal"] = ctx.task.get("program_goal") or ctx.blackboard.get("program_goal")
    ctx.blackboard["local_goal"] = ctx.task.get("local_goal") or ctx.blackboard.get("local_goal")
    ctx.blackboard["dependency_status"] = ctx.task.get("dependency_status") or ctx.blackboard.get("dependency_status") or {}
    ctx.blackboard["contract_completeness"] = ctx.task.get("contract_completeness") or ctx.blackboard.get("contract_completeness") or {}
    ctx.blackboard["verification_plan"] = {
        "commands": combined_command_checks,
        "task_commands": task_verification_commands,
        "manager_commands": manager_command_checks,
        "expected_files": list(ctx.expected_repo_files or []),
    }
    ctx.blackboard["expected_deliverables"] = {
        "deliverables": [str(entry).strip() for entry in _as_list(ctx.task.get("deliverables")) if str(entry).strip()],
        "target_files": [str(entry).strip() for entry in _as_list(ctx.task.get("target_files")) if str(entry).strip()],
        "acceptance_criteria": [str(entry).strip() for entry in _as_list(ctx.task.get("acceptance_criteria")) if str(entry).strip()],
    }
    ctx.blackboard["repo_validation"] = ctx.repo_validation
    _rc._record_coding_run_contract(ctx.blackboard, coding_run_contract)
    ctx.status["repo_validation"] = ctx.repo_validation
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)

    # --- Verification policy ---
    verification = evaluate_verification_policy(
        ctx.review_result, ctx.test_result, repo_validation=ctx.repo_validation
    )
    ctx.blackboard["verification"] = verification.as_dict()
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    logger.info(
        "Verification outcome=%s should_retry=%s (run_id=%s)",
        verification.outcome,
        verification.should_retry,
        ctx.run_id,
    )
    return verification
