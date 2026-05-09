"""phases/github_sync.py -- GitHub MCP connect/disconnect/claim/finalize.

This module owns the GitHub MCP lifecycle within a coding run:
- ``connect_for_intake``   -- Connect before task normalization
- ``disconnect_for_coding`` -- Disconnect before pure-coding phases
- ``sync_claim_status``   -- Enqueue/dispatch the GitHub Project "In Progress" update
- ``finalize_sync``       -- Enqueue/dispatch final status + comment after completion

All functions operate on ``RunContext`` where the run is already under way.
Stateless helpers (``_record_github_warning``, ``_update_github_mcp_status``)
remain in runner_core and are accessed via the _rc bridge pattern.
"""
from __future__ import annotations

import logging
from typing import Any

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.github_sync")


def connect_for_intake(ctx: RunContext, *, required: bool = True) -> bool:
    """Connect GitHub MCP before task intake (intake_only / intake_finalize scope).

    Mutates ctx.status, ctx.blackboard with connection state.

    Returns:
        True if connected successfully, False if not required and connection failed.
    Raises:
        Exception if ``required=True`` and connection failed.
    """
    from src.tandem_agents.core.execution import runner_core as _rc

    connected = _rc._connect_github_for_phase(
        cfg=ctx.cfg,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status if ctx.status else None,
        blackboard=ctx.blackboard if ctx.blackboard else None,
        event_type="github_mcp.connected_for_intake",
        required=required,
    )
    if connected:
        logger.info("GitHub MCP connected for intake (run_id=%s)", ctx.run_id)
    else:
        logger.warning("GitHub MCP not connected for intake (run_id=%s)", ctx.run_id)
    return connected


def disconnect_for_coding(ctx: RunContext) -> None:
    """Disconnect GitHub MCP before the coding phase if scope allows it.

    This is skipped when ``source_scope == "always"`` (GitHub tools are needed
    in the coding phase itself).

    Mutates ctx.status, ctx.blackboard.
    """
    if ctx.source_scope == "always":
        return
    from src.tandem_agents.core.execution import runner_core as _rc

    _rc._disconnect_github_for_coding(
        cfg=ctx.cfg,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status,
        blackboard=ctx.blackboard,
        event_type="github_mcp.disconnected_for_coding",
    )
    logger.debug("GitHub MCP disconnected for coding (run_id=%s)", ctx.run_id)


def sync_claim_status(ctx: RunContext) -> None:
    """Enqueue and dispatch the GitHub Project "In Progress" status update after claim.

    Only runs when ``source_type == "github_project"`` and ``remote_sync != "off"``.

    Mutates ctx.blackboard.
    """
    if ctx.source_type != "github_project":
        return
    from src.tandem_agents.core.execution import runner_core as _rc

    _rc._sync_github_claim_status(
        cfg=ctx.cfg,
        task=ctx.task,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status,
        blackboard=ctx.blackboard,
        remote_sync=ctx.remote_sync,
        coordination=ctx.coordination,
    )
    logger.debug("GitHub claim status synced (run_id=%s)", ctx.run_id)


def finalize_sync(
    ctx: RunContext,
    *,
    outcome: str,
    summary: str,
    diff_snapshot: str | None = None,
    review_returncode: int | None = None,
    test_returncode: int | None = None,
) -> None:
    """Enqueue and dispatch the final GitHub status + comment after run completion.

    Works for both ``completed`` and ``blocked`` outcomes.

    Mutates ctx.blackboard.
    """
    from src.tandem_agents.core.execution import runner_core as _rc

    _rc._finalize_github_sync(
        cfg=ctx.cfg,
        task=ctx.task,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status,
        blackboard=ctx.blackboard,
        outcome=outcome,
        summary=summary,
        diff_snapshot=diff_snapshot,
        review_returncode=review_returncode,
        test_returncode=test_returncode,
        coordination=ctx.coordination,
    )
    logger.debug(
        "GitHub finalize sync dispatched: outcome=%s (run_id=%s)", outcome, ctx.run_id
    )
