from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig


@dataclass(frozen=True)
class ReviewPolicyDecision:
    policy: str
    human_review_required: bool
    auto_merge_requested: bool
    supported: bool
    blocker: str | None
    handoff_rules: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_review_policy(cfg: ResolvedConfig) -> ReviewPolicyDecision:
    policy = str(cfg.review.policy or "human_review").strip().lower() or "human_review"
    auto_merge_requested = policy == "auto_merge"
    supported = not auto_merge_requested
    blocker = None
    handoff_rules = [
        "Pull requests must be reviewed by a human before merge.",
        "ACA does not auto-merge pull requests in the current implementation.",
    ]
    if auto_merge_requested:
        blocker = "review.policy=auto_merge is not implemented yet; use human_review."
        handoff_rules.append("Requested auto-merge is blocked until merge support is implemented.")
    return ReviewPolicyDecision(
        policy=policy,
        human_review_required=True,
        auto_merge_requested=auto_merge_requested,
        supported=supported,
        blocker=blocker,
        handoff_rules=handoff_rules,
    )
