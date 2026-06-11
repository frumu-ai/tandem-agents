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
from pathlib import Path
import shlex
from typing import Any

from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.verification.verification_policy import evaluate_verification_policy

logger = logging.getLogger("aca.phases.review_verify")

PROSE_VERIFIED_SUFFIXES = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"}


def _changes_require_command_verification(changed_files: list[str], expected_files: list[str]) -> bool:
    paths = [str(path or "").strip() for path in changed_files or expected_files if str(path or "").strip()]
    if not paths:
        return False
    for raw_path in paths:
        suffix = Path(raw_path).suffix.lower()
        if suffix not in PROSE_VERIFIED_SUFFIXES:
            return True
    return False


def _run_engine_command_checks(
    cfg: Any,
    repo_path: Path,
    commands: list[str],
    *,
    timeout_seconds: int = 120,
) -> list[dict[str, Any]]:
    from src.tandem_agents.core.engine.engine import execute_engine_tool, engine_visible_path

    host_repo_path = engine_visible_path(repo_path)
    results: list[dict[str, Any]] = []
    for command in commands:
        shell_command = f"cd {shlex.quote(str(host_repo_path))} && {command}"
        try:
            payload = execute_engine_tool(
                cfg,
                "bash",
                {
                    "command": shell_command,
                    "timeout_ms": max(1000, int(timeout_seconds * 1000)),
                },
            )
            metadata = payload.get("metadata") if isinstance(payload, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            exit_code = metadata.get("exit_code")
            returncode = int(exit_code) if isinstance(exit_code, int) else 1
            output_value = payload.get("output") if isinstance(payload, dict) else ""
            stdout = str(output_value or "").strip()
            stderr = str(metadata.get("stderr") or "").strip()
        except Exception as exc:
            returncode = 1
            stdout = ""
            stderr = str(exc)
        results.append(
            {
                "command": command,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "status": "pass" if returncode == 0 else "fail",
                "executor": "tandem_engine",
                "engine_repo_path": str(host_repo_path),
            }
        )
    return results


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
    from src.tandem_agents.core.engine.engine import (
        engine_env,
        engine_session_provider_model,
        git_diff_stat,
        git_working_diff,
    )
    from src.tandem_agents.core.execution.worker import stream_tandem_prompt
    from src.tandem_agents.core.engine.prompts import build_review_prompt, build_test_prompt
    from src.tandem_agents.core.repository.repo_truth import (
        deterministic_repo_validation,
        extract_command_checks,
        filter_executable_command_checks,
        infer_command_checks,
    )
    from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot, write_status

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
    worker_changed_files: list[str] = []
    for result in ctx.worker_results:
        for raw_path in _as_list(result.get("changed_files")):
            rel_path = str(raw_path or "").strip()
            if rel_path and rel_path not in worker_changed_files:
                worker_changed_files.append(rel_path)
    inferred_command_checks = infer_command_checks(ctx.repo_path, worker_changed_files, task=ctx.task)
    combined_command_checks: list[str] = []
    executable_task_commands = filter_executable_command_checks(task_verification_commands)
    for command in executable_task_commands + manager_command_checks + inferred_command_checks:
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

    # Capture the actual uncommitted diff once so the reviewer and tester judge
    # the real worker changes instead of relying only on self-reported notes.
    try:
        repo_diff = git_working_diff(ctx.repo_path)
    except Exception:  # diff is advisory context; never fail the phase over it
        logger.warning("Failed to capture working diff for review (run_id=%s)", ctx.run_id, exc_info=True)
        repo_diff = ""

    # --- Reviewer ---
    review_prompt = build_review_prompt(ctx.run_id, ctx.task, ctx.worker_results, repo_diff=repo_diff)
    review_model_selection = engine_session_provider_model(ctx.cfg, "reviewer")
    review_cli_provider = review_model_selection["provider"]
    review_model = review_model_selection["model"]
    _rc._role_provider_override_config(
        cfg=ctx.cfg,
        layout=ctx.layout,
        role="reviewer",
        provider=review_cli_provider,
        model=review_model,
    )
    logger.info("Running reviewer prompt (run_id=%s)", ctx.run_id)
    with _rc._coordination_heartbeat(ctx, phase="review"):
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
    test_prompt = build_test_prompt(
        ctx.run_id,
        ctx.task,
        ctx.repo,
        ctx.worker_results,
        repo_diff=repo_diff,
        verification_commands=combined_command_checks,
    )
    test_model_selection = engine_session_provider_model(ctx.cfg, "tester")
    test_cli_provider = test_model_selection["provider"]
    test_model = test_model_selection["model"]
    _rc._role_provider_override_config(
        cfg=ctx.cfg,
        layout=ctx.layout,
        role="tester",
        provider=test_cli_provider,
        model=test_model,
    )
    logger.info("Running tester prompt (run_id=%s)", ctx.run_id)
    with _rc._coordination_heartbeat(ctx, phase="test"):
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
    ctx.expected_repo_files = _rc._validation_expected_repo_files(
        ctx.repo_path,
        list(ctx.expected_repo_files or []),
        worker_changed_files,
    )
    ctx.blackboard["expected_repo_files"] = ctx.expected_repo_files
    ctx.repo_validation = _rc._deterministic_repo_validation(ctx.repo_path, ctx.expected_repo_files)
    if combined_command_checks:
        ctx.repo_validation = deterministic_repo_validation(
            ctx.repo_path,
            ctx.expected_repo_files,
            command_checks=[],
        )
        command_results = _run_engine_command_checks(ctx.cfg, ctx.repo_path, combined_command_checks)
        command_failures = [result for result in command_results if result.get("status") != "pass"]
        ctx.repo_validation = dict(ctx.repo_validation)
        ctx.repo_validation["command_checks"] = command_results
        ctx.repo_validation["command_failures"] = command_failures
        ctx.repo_validation["ok"] = bool(ctx.repo_validation.get("ok")) and not command_failures
    if worker_changed_files and _rc._task_mentions_external_pr_candidates(ctx.task):
        unexpected_files = _rc._pr_candidate_unexpected_changed_files(ctx.planned_subtasks, worker_changed_files)
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
    needs_command_verification = _changes_require_command_verification(
        worker_changed_files,
        list(ctx.expected_repo_files or []),
    )
    if (
        coding_run_contract.requires_minimal_verification_before_handoff
        and needs_command_verification
        and not combined_command_checks
    ):
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
        "executable_task_commands": executable_task_commands,
        "manager_commands": manager_command_checks,
        "inferred_commands": inferred_command_checks,
        "changed_files": worker_changed_files,
        "expected_files": list(ctx.expected_repo_files or []),
        "requires_command_verification": needs_command_verification,
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
    ctx.status["verification"] = verification.as_dict()
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)
    append_event(
        ctx.layout["events"],
        "verification.completed",
        ctx.run_id,
        verification.as_dict(),
        task_id=ctx.task.get("task_id"),
        role="tester",
        repo={"path": ctx.repo.get("path")},
    )

    logger.info(
        "Verification outcome=%s should_retry=%s (run_id=%s)",
        verification.outcome,
        verification.should_retry,
        ctx.run_id,
    )
    return verification
