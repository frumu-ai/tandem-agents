"""phases/repair.py -- No-diff, no-proof, and retry decision helpers.

This module owns the loop-exit decision logic that runs after the
integration prompt completes but before review/test:

1. No-diff + repo-validation-failed  -> retry or block (``no_changes_repair``)
2. No-diff + repo-validation-ok but no verifiable proof -> retry or block (``no_proof_repair``)
3. Should-retry after verification failure -> build feedback (``build_retry_feedback``)

Each function returns a ``RepairDecision`` that tells the loop what to do next.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.repair")


@dataclass
class RepairDecision:
    """Outcome of a repair check.

    Attributes:
        action:    One of ``"continue"`` (proceed to review), ``"retry"`` (re-enter
                   the planning loop), or ``"block"`` (return a blocked-run result).
        feedback:  Feedback string to inject into the next manager prompt (retry only).
        message:   Human-readable blocker message (block only).
        kind:      Blocker kind tag for ``run.blocked`` event (block only).
        phase:     Phase label for blocked status (block only).
    """

    action: str  # "continue" | "retry" | "block"
    feedback: str | None = None
    message: str | None = None
    kind: str | None = None
    phase: str = "handoff"


def check_no_diff(ctx: RunContext, attempt: int, max_loops: int) -> RepairDecision:
    """Decide what to do when the run produced no diff and repo validation failed.

    This fires when ``pending_diff_snapshot`` is empty AND
    ``repo_validation.ok`` is False.

    Returns a RepairDecision with action ``"retry"`` or ``"block"``.
    """
    if attempt < max_loops - 1:
        logger.info(
            "Attempt %d produced no diff — scheduling retry (run_id=%s)", attempt + 1, ctx.run_id
        )
        from src.tandem_agents.core.execution import runner_core as _rc
        from src.tandem_agents.runtime.runstate import save_blackboard
        from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

        feedback = (
            "CRITICAL: The previous attempt produced no code changes. "
            "You must write code to satisfy the requirements!"
        )
        _rc._append_blackboard_note(
            ctx.blackboard, f"Attempt {attempt + 1} failed (no diff). Retrying."
        )
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        return RepairDecision(action="retry", feedback=feedback)

    return RepairDecision(
        action="block",
        message="Run produced no repository changes.",
        kind="no_diff",
        phase="handoff",
    )


def check_no_verifiable_proof(ctx: RunContext, attempt: int, max_loops: int) -> RepairDecision:
    """Decide what to do when there is no diff but repo validation passed.

    This fires when the diff is empty AND ``repo_validation.ok`` is True, but
    there are no expected target files and no verifiable worker success.

    Returns a RepairDecision with action ``"continue"``, ``"retry"``, or ``"block"``.
    """
    from src.tandem_agents.core.execution import runner_core as _rc

    has_expected_targets = bool(ctx.expected_repo_files)
    has_verifiable_success = _rc._has_verifiable_worker_success(ctx.worker_results)

    if has_expected_targets or has_verifiable_success:
        # Repo is happy even without a diff — proceed to review
        _rc._append_blackboard_note(
            ctx.blackboard,
            "Repository already satisfied the expected file set; continuing despite zero diff.",
        )
        from src.tandem_agents.runtime.runstate import save_blackboard
        from src.tandem_agents.runtime.run_output import write_blackboard_snapshot
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        return RepairDecision(action="continue")

    no_proof_message = (
        "Run produced no repository changes and no verifiable target-file proof. "
        "ACA will not mark this task complete."
    )

    if attempt < max_loops - 1:
        from src.tandem_agents.runtime.runstate import save_blackboard
        from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

        _rc._append_blackboard_note(
            ctx.blackboard,
            f"Attempt {attempt + 1} failed (no diff, no verifiable proof). Retrying.",
        )
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        logger.info(
            "Attempt %d had no verifiable proof — scheduling retry (run_id=%s)",
            attempt + 1,
            ctx.run_id,
        )
        return RepairDecision(action="retry", feedback=no_proof_message)

    return RepairDecision(
        action="block",
        message=no_proof_message,
        kind="no_verifiable_proof",
        phase="handoff",
    )


def build_retry_feedback(ctx: RunContext, attempt: int, verification: Any) -> str:
    """Build the previous_feedback string for a retry after verification failure.

    Args:
        ctx:          Current run context (has review_result, test_result).
        attempt:      Zero-based attempt index.
        verification: VerificationResult from evaluate_verification_policy.

    Returns:
        A non-empty feedback string to inject into the next manager prompt.
    """
    feedback_parts = []
    if ctx.review_result.get("stdout"):
        feedback_parts.append(f"Reviewer Feedback:\n{ctx.review_result['stdout']}")
    if ctx.test_result.get("stdout"):
        feedback_parts.append(f"Tester Feedback:\n{ctx.test_result['stdout']}")
    feedback = "\n\n".join(feedback_parts) or (
        verification.validation_blocker or "Validation failed without specific stdout."
    )
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    _rc._append_blackboard_note(
        ctx.blackboard,
        (
            f"Attempt {attempt + 1} failed with `repair-needed`. "
            f"Repairing with feedback and re-planning.\n"
            f"Validation blocker: {verification.validation_blocker}"
        ),
    )
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    logger.info(
        "Attempt %d needs repair — feedback length=%d (run_id=%s)",
        attempt + 1,
        len(feedback),
        ctx.run_id,
    )
    return feedback
