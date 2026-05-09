"""domain.py -- Core types for the ACA autonomous coding agent.

Defines the fundamental domain types passed throughout the system.
"""
from __future__ import annotations

from typing import Any, TypedDict


class Task(TypedDict, total=False):
    """An intake task to be processed by an ACA swarm.
    
    This matches the layout expected by the system for any work item.
    """
    task_id: str
    title: str
    description: str
    source: dict[str, Any]
    repo: dict[str, Any]
    metadata: dict[str, Any]


class WorkerResult(TypedDict, total=False):
    """The result of a single worker subtask execution."""
    worker_id: str
    subtask_index: int
    subtask_id: str
    title: str
    status: str
    returncode: int
    worktree: str
    log_path: str
    output_excerpt: str
    write_required: bool
    verified_existing: bool


class RunStatus(TypedDict, total=False):
    """The overall status object for a given ACA run."""
    run: dict[str, Any]
    task: dict[str, Any]
    coordination: dict[str, Any]
    repo: dict[str, Any]
    engine: dict[str, Any]
    provider: dict[str, Any]
    swarm: dict[str, Any]
    github_mcp: dict[str, Any]
    metrics: dict[str, Any]


class ACAError(Exception):
    """Base exception for all ACA-specific domain errors."""
    pass
