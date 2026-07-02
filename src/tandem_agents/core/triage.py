"""Issue triage gate and escalation contract (TAN2-4).

Before the fleet spends tokens on an issue it decides whether the issue is one
it should attempt autonomously. The launch policy is an allowlist of
low-risk, well-scoped issue classes — bug fixes, dependency bumps, test
backfill, and small *well-specified* features. Everything else is refused (or
sent back for clarification) with a stated reason, and escalated to a human
rather than silently attempted or silently dropped.

``triage_task`` is a pure function returning a verdict. ``escalate_triaged_task``
delivers the "needs a human" signal through the existing tracker-comment outbox
(dedupe-guarded so it fires once per task+verdict). ``summarize_triage`` rolls
verdicts up into the escalation-rate metric.
"""

from __future__ import annotations

from typing import Any

# Verdicts
ACCEPTED = "accepted"
REFUSED = "refused"
NEEDS_CLARIFICATION = "needs_clarification"

# Issue classes the allowlist can permit.
BUG_FIX = "bug_fix"
DEPENDENCY_BUMP = "dependency_bump"
TEST_BACKFILL = "test_backfill"
SMALL_FEATURE = "small_feature"

DEFAULT_ALLOWED_CLASSES = (BUG_FIX, DEPENDENCY_BUMP, TEST_BACKFILL, SMALL_FEATURE)

# Label name (lowercased) -> issue class.
_LABEL_CLASS = {
    "bug": BUG_FIX,
    "bugfix": BUG_FIX,
    "bug fix": BUG_FIX,
    "defect": BUG_FIX,
    "regression": BUG_FIX,
    "dependencies": DEPENDENCY_BUMP,
    "dependency": DEPENDENCY_BUMP,
    "deps": DEPENDENCY_BUMP,
    "dependabot": DEPENDENCY_BUMP,
    "test": TEST_BACKFILL,
    "tests": TEST_BACKFILL,
    "testing": TEST_BACKFILL,
    "test-coverage": TEST_BACKFILL,
    "feature": SMALL_FEATURE,
    "enhancement": SMALL_FEATURE,
    "feature-request": SMALL_FEATURE,
}

# Ordered (class, keyword) rules for title/body fallback classification. Order
# matters: more specific classes are checked first.
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (DEPENDENCY_BUMP, ("bump ", "upgrade dependency", "update dependency", "bump version", "dependabot", "npm audit", "pip upgrade")),
    (TEST_BACKFILL, ("add test", "add tests", "backfill test", "test coverage", "unit test", "missing test", "write tests")),
    (BUG_FIX, ("fix bug", "bugfix", "fix crash", "fix error", "regression", "broken", "does not work", "doesn't work", "incorrect", "fix the")),
    (SMALL_FEATURE, ("add ", "implement ", "support ", "introduce ", "create ")),
)


def allowed_classes(cfg: Any) -> set[str]:
    triage = getattr(cfg, "triage", None) if cfg is not None else None
    raw = getattr(triage, "allowed_classes", None)
    if not raw:
        return set(DEFAULT_ALLOWED_CLASSES)
    classes = {part.strip().lower() for part in str(raw).split(",") if part.strip()}
    return classes or set(DEFAULT_ALLOWED_CLASSES)


def _task_labels(task: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for source in (task.get("labels"), (task.get("source") or {}).get("labels")):
        if isinstance(source, (list, tuple)):
            labels.extend(str(label).strip().lower() for label in source if str(label).strip())
    return labels


def _task_text(task: dict[str, Any]) -> str:
    contract = task.get("task_contract") or {}
    parts = [
        str(task.get("title") or ""),
        str(task.get("description") or ""),
        str(contract.get("raw_issue_body") or ""),
    ]
    return "\n".join(parts).lower()


def _acceptance_criteria(task: dict[str, Any]) -> list[str]:
    contract = task.get("task_contract") or {}
    criteria = contract.get("acceptance_criteria")
    if isinstance(criteria, (list, tuple)):
        return [str(item).strip() for item in criteria if str(item).strip()]
    direct = task.get("acceptance_criteria")
    if isinstance(direct, (list, tuple)):
        return [str(item).strip() for item in direct if str(item).strip()]
    return []


def classify_issue_class(task: dict[str, Any]) -> str | None:
    """Best-effort issue-class detection from labels, then title/body keywords."""
    for label in _task_labels(task):
        if label in _LABEL_CLASS:
            return _LABEL_CLASS[label]
    text = _task_text(task)
    if text.strip():
        for issue_class, keywords in _KEYWORD_RULES:
            if any(keyword in text for keyword in keywords):
                return issue_class
    return None


def _is_well_specified(task: dict[str, Any]) -> bool:
    """A feature is safe to attempt only if it is concretely specified."""
    if _acceptance_criteria(task):
        return True
    # A reasonably detailed body is an acceptable proxy for specification.
    contract = task.get("task_contract") or {}
    body = str(contract.get("raw_issue_body") or task.get("description") or "")
    return len(body.strip()) >= 200


def triage_task(task: dict[str, Any], cfg: Any = None) -> dict[str, Any]:
    """Decide whether the fleet should attempt ``task`` autonomously.

    Returns ``{"verdict", "issue_class", "reason"}`` where verdict is one of
    accepted / refused / needs_clarification.
    """
    allow = allowed_classes(cfg)
    issue_class = classify_issue_class(task)
    if issue_class is None:
        return {
            "verdict": NEEDS_CLARIFICATION,
            "issue_class": None,
            "reason": "Could not determine a safe issue class from labels or description; a human should scope it.",
        }
    if issue_class not in allow:
        return {
            "verdict": REFUSED,
            "issue_class": issue_class,
            "reason": f"Issue class '{issue_class}' is not in the autonomy allowlist ({', '.join(sorted(allow))}).",
        }
    if issue_class == SMALL_FEATURE and not _is_well_specified(task):
        return {
            "verdict": NEEDS_CLARIFICATION,
            "issue_class": issue_class,
            "reason": "Feature is under-specified (no acceptance criteria and a thin description); a human should clarify scope before autonomy.",
        }
    return {
        "verdict": ACCEPTED,
        "issue_class": issue_class,
        "reason": f"Issue class '{issue_class}' is within the autonomy allowlist.",
    }


def summarize_triage(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll a list of verdicts into counts + escalation rate (the metric)."""
    total = len(verdicts)
    accepted = sum(1 for v in verdicts if v.get("verdict") == ACCEPTED)
    refused = sum(1 for v in verdicts if v.get("verdict") == REFUSED)
    needs_clarification = sum(1 for v in verdicts if v.get("verdict") == NEEDS_CLARIFICATION)
    escalated = refused + needs_clarification
    return {
        "total": total,
        "accepted": accepted,
        "refused": refused,
        "needs_clarification": needs_clarification,
        "escalated": escalated,
        "escalation_rate": (escalated / total) if total else 0.0,
    }


def _escalation_comment_kind(task: dict[str, Any]) -> str:
    source_type = str((task.get("source") or {}).get("type") or "").strip().lower()
    if source_type == "linear":
        return "linear_issue.comment"
    if source_type in {"github", "github_project"}:
        return "github_issue.comment"
    return ""


def build_triage_escalation_comment(task: dict[str, Any], verdict: dict[str, Any]) -> str:
    title = str(task.get("title") or task.get("task_id") or "this issue").strip()
    reason = str(verdict.get("reason") or "").strip()
    issue_class = verdict.get("issue_class") or "unknown"
    return (
        f"ACA triage did not accept **{title}** for autonomous execution "
        f"(verdict: `{verdict.get('verdict')}`, detected class: `{issue_class}`).\n\n"
        f"{reason}\n\n"
        "This issue needs a human to scope or reassign it — the fleet will not attempt it automatically."
    )


def escalate_triaged_task(cfg: Any, coordination: Any, task: dict[str, Any], verdict: dict[str, Any]) -> bool:
    """Deliver the human-escalation notification for a non-accepted verdict.

    Enqueues a tracker comment through the existing outbox, dedupe-guarded so it
    fires once per (task, verdict). Returns True if a notification was enqueued.
    """
    if verdict.get("verdict") == ACCEPTED:
        return False
    kind = _escalation_comment_kind(task)
    if not kind:
        return False
    task_key = str(task.get("task_key") or task.get("task_id") or "").strip()
    if not task_key:
        return False
    body = build_triage_escalation_comment(task, verdict)
    coordination.enqueue_outbox(
        kind=kind,
        aggregate_type="task",
        aggregate_id=task_key,
        payload={"task": task, "body": body, "outcome": "triage_escalated"},
        dedupe_key=f"{task_key}:triage-escalation:{verdict.get('verdict')}",
    )
    return True
