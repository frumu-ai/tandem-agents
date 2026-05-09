from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.aca.core.repository.repo_truth import repo_validation_blocker_message

REPAIR_ACTIONS = {
    "repair_needed",
    "repair",
    "retry",
    "needs_changes",
    "changes_requested",
    "fix_required",
    "revise",
    "fail",
    "failed",
}
HUMAN_REVIEW_ACTIONS = {
    "human_review_needed",
    "human_review",
    "human_review_required",
    "manual_review",
    "needs_human_review",
    "review_inconclusive_verification_required",
}
BLOCKED_ACTIONS = {
    "blocked",
    "pending",
    "in_progress",
    "error",
    "incomplete",
    "unknown",
}


def _extract_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1].strip())
    for candidate in candidates:
        try:
            import json

            loaded = json.loads(candidate)
        except Exception:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalized_action(value: Any) -> str:
    return _normalized_text(value).replace("-", "_")


@dataclass(frozen=True)
class VerificationPolicyDecision:
    outcome: str
    failure_category: str | None
    review_blocker: str | None
    test_blocker: str | None
    repo_blocker: str | None
    validation_blocker: str | None
    should_retry: bool
    review_returncode: int | None
    test_returncode: int | None
    review_outcome: str
    test_outcome: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "failure_category": self.failure_category,
            "review_blocker": self.review_blocker,
            "test_blocker": self.test_blocker,
            "repo_blocker": self.repo_blocker,
            "validation_blocker": self.validation_blocker,
            "should_retry": self.should_retry,
            "review_returncode": self.review_returncode,
            "test_returncode": self.test_returncode,
            "review_outcome": self.review_outcome,
            "test_outcome": self.test_outcome,
        }


def _severity_label(review_status: str, payload: dict[str, Any], *, source: str) -> tuple[str, str | None]:
    next_action = _normalized_action(payload.get("next_action") or payload.get("action") or payload.get("outcome"))
    status = _normalized_action(review_status)
    findings = list(payload.get("findings") or [])
    required_fixes = payload.get("required_fixes")
    severe_findings: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = _normalized_action(finding.get("severity"))
        if severity in {"critical", "high", "error", "blocking", "blocker", "fatal"}:
            severe_findings.append(finding)

    if source == "review":
        if next_action in HUMAN_REVIEW_ACTIONS or status in HUMAN_REVIEW_ACTIONS:
            return "human_review_needed", f"Reviewer status is `{review_status or next_action or 'unknown'}`."
        if next_action in REPAIR_ACTIONS or status in REPAIR_ACTIONS:
            return "repair_needed", "Reviewer reported required fixes."
        if severe_findings:
            return "repair_needed", "Reviewer reported high-severity findings."
        if required_fixes:
            return "repair_needed", "Reviewer reported required fixes."
        if next_action in BLOCKED_ACTIONS or status in BLOCKED_ACTIONS:
            return "blocked", f"Reviewer status is `{review_status or next_action or 'blocked'}`."
        return "pass", None

    # tester
    results_field = payload.get("results")
    results_status = ""
    if isinstance(results_field, str):
        results_status = _normalized_action(results_field)
    elif isinstance(results_field, list):
        if any(isinstance(r, dict) and _normalized_action(r.get("status")) in {"fail", "failed", "error"} for r in results_field):
            results_status = "failed"
        elif any(isinstance(r, dict) and _normalized_action(r.get("status")) in {"blocked", "pending", "in_progress"} for r in results_field):
            results_status = "blocked"
        else:
            results_status = "pass"
    else:
        results_status = _normalized_action(payload.get("overall_status") or "")

    if next_action in HUMAN_REVIEW_ACTIONS or status in HUMAN_REVIEW_ACTIONS:
        return "human_review_needed", f"Tester status is `{review_status or next_action or 'unknown'}`."
    if next_action in REPAIR_ACTIONS or status in REPAIR_ACTIONS or results_status == "failed":
        return "repair_needed", f"Tester results are `{results_status or review_status or next_action or 'failed'}`."
    if next_action in BLOCKED_ACTIONS or status in BLOCKED_ACTIONS or results_status in {"blocked", "pending", "in_progress"}:
        return "blocked", f"Tester results are `{results_status or review_status or next_action or 'blocked'}`."
    return "pass", None


def review_blocker_message(
    review_result: dict[str, Any],
    repo_validation: dict[str, Any] | None = None,
) -> str | None:
    payload = _extract_json(review_result.get("stdout") or "") or {}
    required_fixes = payload.get("required_fixes")
    if isinstance(required_fixes, dict):
        payload["required_fixes"] = [{"description": str(v)} for v in required_fixes.values() if str(v).strip()]
    elif isinstance(required_fixes, list):
        payload["required_fixes"] = list(required_fixes)
    else:
        payload["required_fixes"] = []

    has_known_keys = any(
        k in payload
        for k in (
            "review_status",
            "status",
            "overall_status",
            "verdict",
            "next_action",
            "action",
            "outcome",
            "findings",
            "required_fixes",
            "tests",
            "validation",
            "results",
        )
    )
    if not has_known_keys and payload and all(isinstance(v, str) for v in payload.values()):
        payload["required_fixes"].extend([{"description": str(v)} for v in payload.values() if str(v).strip()])

    review_status = _normalized_text(
        payload.get("review_status")
        or payload.get("status")
        or payload.get("overall_status")
        or payload.get("verdict")
    )
    findings = list(payload.get("findings") or [])
    severe_findings: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = _normalized_action(finding.get("severity"))
        if severity in {"critical", "high", "error", "blocking", "blocker", "fatal"}:
            severe_findings.append(finding)
    outcome, message = _severity_label(review_status, payload, source="review")
    if outcome == "pass":
        return None
    if outcome == "human_review_needed":
        return message
    if outcome == "repair_needed":
        if severe_findings:
            return "Reviewer reported high-severity findings."
        if payload.get("required_fixes"):
            return "Reviewer reported required fixes."
        return message or "Reviewer reported required fixes."
    if outcome == "blocked":
        if review_status:
            return f"Reviewer status is `{review_status}`."
        return message or "Reviewer blocked the run."
    return message or None


def test_blocker_message(test_result: dict[str, Any], repo_validation: dict[str, Any] | None = None) -> str | None:
    payload = _extract_json(test_result.get("stdout") or "") or {}
    status = _normalized_text(payload.get("status") or payload.get("overall_status") or payload.get("verdict"))
    outcome, message = _severity_label(status, payload, source="test")
    if outcome == "pass":
        return None
    if message:
        return message
    return None


test_blocker_message.__test__ = False


def evaluate_verification_policy(
    review_result: dict[str, Any],
    test_result: dict[str, Any],
    repo_validation: dict[str, Any] | None = None,
) -> VerificationPolicyDecision:
    review_blocker = review_blocker_message(review_result, repo_validation=repo_validation)
    test_blocker = test_blocker_message(test_result, repo_validation=repo_validation)
    repo_blocker = repo_validation_blocker_message(repo_validation or {})
    review_payload = _extract_json(review_result.get("stdout") or "") or {}
    test_payload = _extract_json(test_result.get("stdout") or "") or {}
    review_status = _normalized_text(
        review_payload.get("review_status")
        or review_payload.get("status")
        or review_payload.get("overall_status")
        or review_payload.get("verdict")
    )
    test_status = _normalized_text(test_payload.get("status") or test_payload.get("overall_status") or test_payload.get("verdict"))
    review_outcome, _ = _severity_label(review_status, review_payload, source="review")
    test_outcome, _ = _severity_label(test_status, test_payload, source="test")
    if review_outcome == "human_review_needed" or test_outcome == "human_review_needed":
        outcome = "human_review_needed"
    elif review_outcome == "blocked" or test_outcome == "blocked" or repo_blocker:
        outcome = "blocked"
    elif review_outcome == "repair_needed" or test_outcome == "repair_needed":
        outcome = "repair_needed"
    else:
        outcome = "pass"
    validation_blocker = repo_blocker or review_blocker or test_blocker
    failure_category = "verification_missing" if repo_blocker or (repo_validation or {}).get("verification_missing") else None
    should_retry = bool(
        outcome == "repair_needed"
        and (
            int(review_result.get("returncode") or 0) == 0
            or int(test_result.get("returncode") or 0) == 0
            or validation_blocker
        )
    )
    return VerificationPolicyDecision(
        outcome=outcome,
        failure_category=failure_category,
        review_blocker=review_blocker,
        test_blocker=test_blocker,
        repo_blocker=repo_blocker,
        validation_blocker=validation_blocker,
        should_retry=should_retry,
        review_returncode=review_result.get("returncode"),
        test_returncode=test_result.get("returncode"),
        review_outcome=review_outcome,
        test_outcome=test_outcome,
    )
