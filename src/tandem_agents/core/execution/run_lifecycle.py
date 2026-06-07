"""run_lifecycle.py -- Shared helpers for the ACA coding-run lifecycle.

Provides:
- build_provider_config_dict  -- assembles the provider metadata dict for initial_status
- build_swarm_config_dict     -- assembles the swarm metadata dict for initial_status
- block_run                   -- full blocked-run lifecycle in one call
- finalize_run_result         -- standard return-value helper for completed runs
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status
from src.tandem_agents.runtime.runstate import append_event, initial_status, write_status

logger = logging.getLogger("aca.run_lifecycle")


# ---------------------------------------------------------------------------
# Config dict helpers
# ---------------------------------------------------------------------------


def build_provider_config_dict(cfg: ResolvedConfig) -> dict[str, Any]:
    """Assemble the provider metadata dict expected by initial_status.

    Eliminates the repeated 4-line literal that appeared 6+ times in runner_core.
    """
    return {
        "id": cfg.provider.id,
        "model": cfg.provider.model,
        "fallback_provider": cfg.provider.fallback_provider or None,
        "fallback_model": cfg.provider.fallback_model or None,
    }


def build_swarm_config_dict(cfg: ResolvedConfig) -> dict[str, Any]:
    """Assemble the swarm metadata dict expected by initial_status.

    Records the resolved provider/model for each role together with the source
    of each value (role override / global provider / fallback / built-in
    default). Roles whose model resolved to the built-in ``default`` had no
    operator selection (e.g. from the control panel); we surface those so a run
    silently degrading to the generic fallback model is visible rather than
    hidden.
    """
    roles = ("manager", "worker", "reviewer", "tester")
    role_entries: dict[str, dict[str, Any]] = {}
    defaulted: list[str] = []
    for role in roles:
        resolved = cfg.provider_for_role_with_source(role)
        role_entries[role] = {
            "provider": resolved["provider"],
            "model": resolved["model"],
            "provider_source": resolved["provider_source"],
            "model_source": resolved["model_source"],
        }
        if resolved["model_source"] == "default":
            defaulted.append(role)

    if defaulted:
        logger.warning(
            "No model configured for role(s) %s; falling back to the built-in "
            "default %s/%s. Select a model in the control panel (or set "
            "ACA_PROVIDER/ACA_MODEL) for better results.",
            ", ".join(defaulted),
            cfg.provider_for_role(defaulted[0])[0],
            cfg.provider_for_role(defaulted[0])[1],
        )

    return {
        "enabled": cfg.swarm.enabled,
        "shared_model": cfg.swarm.shared_model,
        "max_workers": cfg.swarm.max_workers,
        "using_default_model_fallback": bool(defaulted),
        "default_model_fallback_roles": defaulted,
        **role_entries,
    }


# ---------------------------------------------------------------------------
# Blocked-run helper
# ---------------------------------------------------------------------------


def block_run(
    *,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    cfg: ResolvedConfig,
    task: dict[str, Any] | None,
    repo: dict[str, Any] | None,
    engine: dict[str, Any],
    phase: str,
    kind: str,
    message: str,
    role: str = "manager",
    phase_detail: str | None = None,
    coordination: CoordinationStore | None = None,
    worker_results: list[dict[str, Any]] | None = None,
    existing_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handle the entire blocked-run lifecycle in one call.

    Replaces the ~25-line copy-pasted block that appeared 14 times in
    _run_once_internal. Performs:
      1. Build (or reuse) a run status dict
      2. Call set_status with the blocker
      3. Save a blocked summary to summary.md
      4. Append a run.blocked event
      5. Optionally touch coordination
      6. Return the standard run-result dict

    Args:
        run_id:          Current run identifier.
        run_dir:         Path to the per-run output directory.
        layout:          Dict of well-known paths (status, events, summary, ...).
        cfg:             Resolved ACA configuration.
        task:            Normalized task dict (may be None if we blocked before intake).
        repo:            Resolved repo info dict (may be None if blocked before resolution).
        engine:          Engine health/info dict.
        phase:           The phase name where the block occurred (e.g. "engine_check").
        kind:            The kind label for the run.blocked event (e.g. "engine").
        message:         Human-readable blocker message.
        role:            The role responsible for the block (default "manager").
        phase_detail:    Optional detailed phase description (defaults to message).
        coordination:    Optional CoordinationStore to update run state.
        worker_results:  Optional list of worker results for summary rendering.
        existing_status: If a status dict was already partially built, pass it here
                         instead of creating a fresh one.

    Returns:
        Standard run-result dict: {"run_id", "status", "layout"}.
    """
    effective_task = task or {
        "source": {"type": cfg.task_source.type},
        "title": f"Blocked at phase: {phase}",
    }
    effective_repo = repo or {"path": ""}
    effective_detail = phase_detail or message

    if existing_status is None:
        status = initial_status(
            run_id,
            effective_task,
            effective_repo,
            engine,
            build_provider_config_dict(cfg),
            build_swarm_config_dict(cfg),
            run_dir,
        )
    else:
        status = existing_status

    status = set_status(
        status,
        layout,
        phase=phase,
        phase_detail=effective_detail,
        run_status="blocked",
        blocker=(True, kind, message, role),
        run_completed=True,
    )

    task_title = effective_task.get("title") if isinstance(effective_task, dict) else None
    save_run_text(
        layout["summary"],
        build_blocked_summary(
            task_title=task_title,
            message=message,
            worker_results=worker_results,
        ),
    )
    append_event(
        layout["events"],
        "run.blocked",
        run_id,
        {"kind": kind, "phase": phase, "detail": effective_detail},
    )

    if coordination is not None:
        try:
            coordination.update_run(
                run_id,
                status="blocked",
                phase=phase,
                error=message,
                completed=True,
            )
        except Exception:
            logger.warning(
                "Failed to update coordination run state for blocked run %s",
                run_id,
                exc_info=True,
            )

    logger.warning(
        "Run %s blocked at phase=%s kind=%s: %s",
        run_id,
        phase,
        kind,
        message,
    )

    return {
        "run_id": run_id,
        "status": status,
        "layout": {k: str(v) for k, v in layout.items()},
    }


# ---------------------------------------------------------------------------
# Completed-run result helper
# ---------------------------------------------------------------------------


def make_run_result(
    run_id: str,
    status: dict[str, Any],
    layout: dict[str, Path],
    **extras: Any,
) -> dict[str, Any]:
    """Build the standard run-result dict returned from run_once / _run_once_internal.

    Accepts optional keyword arguments (e.g. worker_results, board) that are
    merged into the result for callers that need them.
    """
    result: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "layout": {k: str(v) for k, v in layout.items()},
    }
    result.update(extras)
    return result
