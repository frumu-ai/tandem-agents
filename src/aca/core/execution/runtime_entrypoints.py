"""Process-level ACA entrypoints.

The explicit worker entrypoint exists now so deployment can split coordinator
and worker containers, but it still reuses the current execution pipeline until
the coordinator/worker dispatcher is fully separated.
"""

from __future__ import annotations

from src.aca.core.coordination.coordination import CoordinationStore, default_host_id, default_worker_id
from src.aca.config.config_types import ResolvedConfig
from src.aca.core.execution.runner_core import run_once

DEFAULT_RUNTIME_ROLE = "coordinator"
WORKER_RUNTIME_ROLE = "worker"
VALID_RUNTIME_ROLES = {DEFAULT_RUNTIME_ROLE, WORKER_RUNTIME_ROLE}


def runtime_role(cfg: ResolvedConfig) -> str:
    raw = str(
        cfg.env.get("ACA_COORDINATION_ROLE")
        or cfg.env.get("ACA_RUNTIME_ROLE")
        or DEFAULT_RUNTIME_ROLE
    ).strip().lower()
    return raw if raw in VALID_RUNTIME_ROLES else DEFAULT_RUNTIME_ROLE


def _worker_capabilities(cfg: ResolvedConfig, role: str) -> dict[str, object]:
    return {
        "mode": role,
        "provider": cfg.provider.id,
        "model": cfg.provider.model,
        "repo_slug": cfg.repository.slug or "",
        "source_type": cfg.task_source.type,
    }


def _register_runtime_worker(cfg: ResolvedConfig, role: str) -> None:
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    store.register_worker(
        worker_id=default_worker_id(cfg),
        host_id=default_host_id(cfg),
        role=role,
        status="idle",
        capabilities=_worker_capabilities(cfg, role),
    )


def run_coordinator(cfg: ResolvedConfig) -> dict[str, object]:
    _register_runtime_worker(cfg, DEFAULT_RUNTIME_ROLE)
    return run_once(cfg)


def run_worker(cfg: ResolvedConfig) -> dict[str, object]:
    _register_runtime_worker(cfg, WORKER_RUNTIME_ROLE)
    return run_once(cfg)
