"""phases/context.py -- Per-run shared state container.

``RunContext`` is the single object that carries all mutable run state
through the coding-run lifecycle phases.  Instead of passing 15+ keyword
arguments to every phase function, callers pass one ``RunContext``.

Phase functions mutate the context in-place and return themselves or
raise ``PhaseBlocked`` / ``PhaseCompleted`` to signal lifecycle transitions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunContext:
    """All shared state for one ACA coding run.

    Fields are initialized by ``runner_core._run_once_internal`` after the
    task-intake phase and updated in-place by subsequent phases.

    Attributes
    ----------
    run_id:         Unique run identifier (e.g. ``run-20240101-abc123``).
    run_dir:        Path to the per-run output directory.
    layout:         Dict of well-known paths produced by ``ensure_layout``.
    cfg:            Resolved ACA configuration.
    coordination:   Open CoordinationStore instance for this run.
    engine:         Engine health info dict from ``engine_health()``.
    repo:           Resolved repo info dict from ``resolve_repository()``.
    task:           Normalized task dict.
    board:          Board state dict (cards + config).
    board_path:     Path to the board YAML on disk.
    branch_name:    The canonical run branch name.
    claim_identity: Dict with ``worker_id``, ``host_id``, ``role``, ``source_type``.
    status:         Current run status dict (written to ``status.json``).
    blackboard:     Current blackboard dict (written to ``blackboard.yaml``).
    source_type:    Task source type string (e.g. ``github_project``).
    source_scope:   GitHub MCP scope string (e.g. ``intake_finalize``).
    remote_sync:    GitHub remote sync mode string (e.g. ``status_comment``).
    execution_backend: Backend mode string (``legacy`` or ``coder``).
    worker_results: Accumulated worker result dicts for the current attempt.
    """

    # --- Identity ---
    run_id: str
    run_dir: Path
    layout: dict[str, Path]

    # --- Config & services ---
    cfg: Any  # ResolvedConfig -- avoid circular import at module level
    coordination: Any  # CoordinationStore

    # --- Resolved resources ---
    engine: dict[str, Any] = field(default_factory=dict)
    repo: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)
    board: dict[str, Any] = field(default_factory=dict)
    board_path: Path = field(default_factory=Path)
    branch_name: str = ""
    claim_identity: dict[str, str] = field(default_factory=dict)

    # --- Run state ---
    status: dict[str, Any] = field(default_factory=dict)
    blackboard: dict[str, Any] = field(default_factory=dict)

    # --- Source / sync metadata ---
    source_type: str = ""
    source_scope: str = "none"
    remote_sync: str = "off"
    execution_backend: str = "legacy"

    # --- Worker results ---
    worker_results: list[dict[str, Any]] = field(default_factory=list)

    # --- Subtask planning ---
    planned_subtasks: list[dict[str, Any]] = field(default_factory=list)
    pending_subtasks: list[dict[str, Any]] = field(default_factory=list)
    expected_repo_files: list[str] = field(default_factory=list)
    repo_validation: dict[str, Any] = field(default_factory=dict)

    # --- Review / test results ---
    review_result: dict[str, Any] = field(default_factory=dict)
    test_result: dict[str, Any] = field(default_factory=dict)
    manager_plan: dict[str, Any] = field(default_factory=dict)

    # --- Loop state ---
    pending_diff_snapshot: str = ""

    # --- Coordination heartbeat health ---
    # consecutive_heartbeat_misses tracks how many heartbeat attempts in a row
    # have returned None (lease not active or already reaped). After
    # COORDINATION_LOST_THRESHOLD misses the run blocks itself with a
    # `coordination_lost` blocker rather than continuing on a dead lease.
    consecutive_heartbeat_misses: int = 0
    coordination_lost: bool = False

    # ---------------------------------------------------------------------------
    # Convenience helpers
    # ---------------------------------------------------------------------------

    @property
    def lease_id(self) -> str | None:
        """Return the current coordination lease ID, or None."""
        return self.status.get("coordination", {}).get("lease_id")

    @property
    def repo_path(self) -> Path:
        """Return the repository path as a Path object."""
        return Path(self.repo.get("path") or ".")

    def coordination_task_context(
        self,
    ) -> tuple[str | None, str | None, str | None, str | None, int | None]:
        """Extract coordination fields from the current status dict.

        Returns (task_key, lease_id, worker_id, host_id, lease_expires_at_ms).
        """
        coord = dict(self.status.get("coordination") or {})
        return (
            coord.get("task_key"),
            coord.get("lease_id"),
            coord.get("worker_id"),
            coord.get("host_id"),
            coord.get("lease_expires_at_ms"),
        )

    def make_result(self, **extras: Any) -> dict[str, Any]:
        """Build a standard run-result dict for return from _run_once_internal."""
        result: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "layout": {k: str(v) for k, v in self.layout.items()},
        }
        result.update(extras)
        return result
