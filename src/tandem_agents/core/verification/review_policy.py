from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig


@dataclass(frozen=True)
class ReviewPolicyDecision:
    policy: str
    human_review_required: bool
    auto_merge_requested: bool
    merge_requires_approval: bool
    branch_delete_requires_approval: bool
    delete_branch_after_merge: bool
    supported: bool
    blocker: str | None
    handoff_rules: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_review_policy(cfg: ResolvedConfig) -> ReviewPolicyDecision:
    policy = str(cfg.review.policy or "human_review").strip().lower() or "human_review"
    auto_merge_requested = policy == "auto_merge"
    supported = True
    blocker = None
    handoff_rules = []
    if auto_merge_requested:
        strategy = str(cfg.review.auto_merge_strategy or "squash").strip().lower()
        handoff_rules.extend(
            [
                "Auto-merge is opt-in and must pass guarded PR lifecycle gates.",
                f"Configured merge strategy: `{strategy}`.",
                "ACA must prove checks are clean and review state is approved before merge.",
            ]
        )
        if cfg.review.merge_requires_approval:
            handoff_rules.append("A human approval is required before ACA performs the merge.")
        else:
            handoff_rules.append("Merge approval gate is disabled by policy.")
        if cfg.review.delete_branch_after_merge:
            if cfg.review.branch_delete_requires_approval:
                handoff_rules.append("A separate human approval is required before ACA deletes the remote branch.")
            else:
                handoff_rules.append("Remote branch deletion approval gate is disabled by policy.")
        else:
            handoff_rules.append("Remote branch deletion after merge is disabled by policy.")
    else:
        handoff_rules.extend(
            [
                "Pull requests must be reviewed by a human before merge.",
                "ACA will not auto-merge pull requests while review.policy=human_review.",
            ]
        )
    return ReviewPolicyDecision(
        policy=policy,
        human_review_required=not auto_merge_requested,
        auto_merge_requested=auto_merge_requested,
        merge_requires_approval=bool(cfg.review.merge_requires_approval),
        branch_delete_requires_approval=bool(cfg.review.branch_delete_requires_approval),
        delete_branch_after_merge=bool(cfg.review.delete_branch_after_merge),
        supported=supported,
        blocker=blocker,
        handoff_rules=handoff_rules,
    )
