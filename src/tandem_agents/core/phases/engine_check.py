"""phases/engine_check.py -- Engine health and repository binding validation.

This module owns the first two logical barriers before a run begins:
1. Engine availability (Tandem engine is reachable and healthy)
2. Repository binding validity (slug, path, remote URL all resolve)

Both checks are fast, stateless IO calls that happen before any task
intake or coordination work. If either fails, the run is blocked
immediately with a clear diagnostic.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import engine_health, ensure_engine, resolve_repository
from src.tandem_agents.core.repository.repository import repository_binding_issues
from src.tandem_agents.core.execution.run_lifecycle import block_run

logger = logging.getLogger("aca.phases.engine_check")


def check_engine_at_startup(
    cfg: ResolvedConfig,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the engine pre-flight in ``run_once`` (before auto-approve starts).

    Uses ``ensure_engine`` rather than ``engine_health`` because this is the
    first contact -- it may start the engine if configured to do so.

    Returns:
        (engine_info, blocked_result)

        ``blocked_result`` is non-None if the engine is blocked -- the caller
        should ``return blocked_result`` immediately.
    """
    engine = ensure_engine(cfg, layout["logs"])
    if engine.get("action") == "blocked":
        detail = engine.get("detail") or "Engine unavailable"
        logger.warning("Engine blocked at startup: %s", detail)
        return engine, block_run(
            run_id=run_id,
            run_dir=run_dir,
            layout=layout,
            cfg=cfg,
            task=None,
            repo=None,
            engine=engine,
            phase="engine_check",
            kind="engine",
            message=f"Engine issue: {detail}",
            phase_detail=detail,
        )
    logger.info("Engine healthy: %s", engine.get("version") or engine.get("status") or "ok")
    return engine, None


def check_engine_health(
    cfg: ResolvedConfig,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the engine and repository binding checks inside ``_run_once_internal``.

    Uses ``engine_health`` (lightweight ping) rather than ``ensure_engine``
    because the engine was already started in ``run_once``.

    Returns:
        (engine_info, blocked_result)

        ``blocked_result`` is non-None if either the engine or repository
        binding is invalid — the caller should ``return blocked_result``
        immediately.
    """
    engine = engine_health(cfg)

    # 1. Repository binding static checks (no network)
    repo_issues = repository_binding_issues(cfg)
    if repo_issues:
        repo_error = "; ".join(repo_issues)
        logger.warning("Repository binding invalid: %s", repo_error)
        return engine, block_run(
            run_id=run_id,
            run_dir=run_dir,
            layout=layout,
            cfg=cfg,
            task=None,
            repo=None,
            engine=engine,
            phase="repo_resolution",
            kind="repository",
            message=repo_error,
        )

    # 2. Repository resolution (network/disk)
    try:
        repo = resolve_repository(cfg)
    except RuntimeError as exc:
        repo_error = str(exc).strip() or "Repository resolution failed"
        logger.warning("Repository resolution failed: %s", repo_error)
        return engine, block_run(
            run_id=run_id,
            run_dir=run_dir,
            layout=layout,
            cfg=cfg,
            task=None,
            repo=None,
            engine=engine,
            phase="repo_resolution",
            kind="repository",
            message=repo_error,
        )

    logger.info(
        "Repository resolved: path=%s branch=%s",
        repo.get("path"),
        repo.get("branch"),
    )
    return engine, None  # blocked_result = None means "proceed"


def resolve_repo_after_checkout(cfg: ResolvedConfig) -> dict[str, Any]:
    """Re-resolve the repository after branch checkout to refresh status fields.

    The branch name changes after ``checkout_run_branch`` so the repo dict
    must be refreshed to reflect the new HEAD.
    """
    return resolve_repository(cfg)
