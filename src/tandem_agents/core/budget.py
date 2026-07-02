"""Per-issue spend accounting and budget enforcement (TAN2-1).

The ACA orchestrates Tandem coder runs; without a per-issue ceiling a single
stuck issue can grind indefinitely (repeated repair passes, oversized runs)
and wipe out the margin on an outcome-priced engagement. This module provides:

- ``extract_usage`` — best-effort parse of token/cost usage from a coder
  result (the engine's usage schema is not guaranteed, so we probe the common
  shapes and degrade to zeros).
- a per-run spend ledger persisted in coordination run metadata
  (``issue_spend``: total_tokens, cost_usd, coder_executions).
- ``budget_status`` — pure predicate deciding whether any enabled budget axis
  is exhausted, so callers can escalate to a human instead of spending more.

``coder_executions`` is always enforceable ACA-side (it counts dispatched
coder passes, main + repairs) and acts as a hard backstop even when the engine
reports no token usage; token and cost caps add finer control when available.
"""

from __future__ import annotations

from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig

ISSUE_SPEND_KEY = "issue_spend"


def empty_spend() -> dict[str, Any]:
    return {"total_tokens": 0, "cost_usd": 0.0, "coder_executions": 0}


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _usage_from_container(container: Any) -> tuple[int, float]:
    """Pull (tokens, cost) from a single dict, trying several key conventions."""
    if not isinstance(container, dict):
        return 0, 0.0
    tokens = 0.0
    for key in ("total_tokens", "tokens", "token_count"):
        tokens = max(tokens, _num(container.get(key)))
    combined = _num(container.get("input_tokens")) + _num(container.get("output_tokens"))
    tokens = max(tokens, combined)
    combined = _num(container.get("prompt_tokens")) + _num(container.get("completion_tokens"))
    tokens = max(tokens, combined)
    cost = 0.0
    for key in ("cost_usd", "total_cost_usd", "cost", "total_cost"):
        cost = max(cost, _num(container.get(key)))
    return int(tokens), float(cost)


def extract_usage(coder_result: dict[str, Any]) -> dict[str, Any]:
    """Best-effort token/cost extraction from a coder result.

    Probes the containers the engine may echo usage into and takes the maximum
    per axis rather than the sum, since the same totals are frequently repeated
    across ``run`` / ``execute_response`` / ``run_response`` — summing would
    double-count and escalate prematurely. Always returns numeric fields.
    """
    tokens = 0
    cost = 0.0
    if isinstance(coder_result, dict):
        containers: list[Any] = [coder_result]
        for key in ("usage", "run", "coder_run", "execute_response", "run_response"):
            value = coder_result.get(key)
            if isinstance(value, dict):
                containers.append(value)
                nested = value.get("usage")
                if isinstance(nested, dict):
                    containers.append(nested)
        for container in containers:
            t, c = _usage_from_container(container)
            tokens = max(tokens, t)
            cost = max(cost, c)
    return {"total_tokens": int(tokens), "cost_usd": float(cost)}


def accumulate(spend: dict[str, Any], usage: dict[str, Any], *, executions_delta: int = 1) -> dict[str, Any]:
    return {
        "total_tokens": int(spend.get("total_tokens") or 0) + int(usage.get("total_tokens") or 0),
        "cost_usd": float(spend.get("cost_usd") or 0.0) + float(usage.get("cost_usd") or 0.0),
        "coder_executions": int(spend.get("coder_executions") or 0) + int(executions_delta),
    }


def budget_status(spend: dict[str, Any], cfg: ResolvedConfig) -> tuple[bool, str]:
    """Return ``(exhausted, reason)`` for the current spend against ``cfg.budget``.

    An axis with a value <= 0 is disabled. ``reason`` names every exhausted
    axis (empty string when within budget).
    """
    budget = getattr(cfg, "budget", None)
    if budget is None:
        return False, ""
    reasons: list[str] = []
    max_tokens = int(getattr(budget, "max_tokens", 0) or 0)
    if max_tokens > 0 and int(spend.get("total_tokens") or 0) >= max_tokens:
        reasons.append(f"tokens {int(spend.get('total_tokens') or 0)}>={max_tokens}")
    max_cost = float(getattr(budget, "max_cost_usd", 0.0) or 0.0)
    if max_cost > 0 and float(spend.get("cost_usd") or 0.0) >= max_cost:
        reasons.append(f"cost ${float(spend.get('cost_usd') or 0.0):.2f}>=${max_cost:.2f}")
    max_exec = int(getattr(budget, "max_coder_executions", 0) or 0)
    if max_exec > 0 and int(spend.get("coder_executions") or 0) >= max_exec:
        reasons.append(f"coder_executions {int(spend.get('coder_executions') or 0)}>={max_exec}")
    return bool(reasons), "; ".join(reasons)


def load_issue_spend(coordination: Any, run_id: str) -> dict[str, Any]:
    run = coordination.get_run(run_id) or {}
    metadata = run.get("metadata") or {}
    spend = metadata.get(ISSUE_SPEND_KEY)
    if isinstance(spend, dict):
        merged = empty_spend()
        merged.update(spend)
        return merged
    return empty_spend()


def record_coder_spend(
    coordination: Any,
    run_id: str,
    coder_result: dict[str, Any],
    *,
    executions_delta: int = 1,
) -> dict[str, Any]:
    """Fold a coder result's usage into the persisted per-issue spend ledger."""
    usage = extract_usage(coder_result)
    spend = accumulate(load_issue_spend(coordination, run_id), usage, executions_delta=executions_delta)
    run = coordination.get_run(run_id) or {}
    metadata = dict(run.get("metadata") or {})
    metadata[ISSUE_SPEND_KEY] = spend
    coordination.update_run(run_id, metadata=metadata)
    return spend
