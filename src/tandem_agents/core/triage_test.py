from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.core import triage
from src.tandem_agents.core.coordination.coordination import CoordinationStore


def _cfg(allowed="bug_fix,dependency_bump,test_backfill,small_feature"):
    return SimpleNamespace(triage=SimpleNamespace(allowed_classes=allowed, enabled=True, enforce=True))


class TriageClassificationTest(unittest.TestCase):
    def test_bug_label_accepted(self) -> None:
        v = triage.triage_task({"title": "Login fails", "labels": ["Bug"]}, _cfg())
        self.assertEqual(v["verdict"], triage.ACCEPTED)
        self.assertEqual(v["issue_class"], triage.BUG_FIX)

    def test_dependency_bump_by_keyword(self) -> None:
        v = triage.triage_task({"title": "Bump lodash to 4.17.21"}, _cfg())
        self.assertEqual(v["issue_class"], triage.DEPENDENCY_BUMP)
        self.assertEqual(v["verdict"], triage.ACCEPTED)

    def test_test_backfill_by_keyword(self) -> None:
        v = triage.triage_task({"title": "Add tests for the parser"}, _cfg())
        self.assertEqual(v["issue_class"], triage.TEST_BACKFILL)
        self.assertEqual(v["verdict"], triage.ACCEPTED)

    def test_unclassifiable_needs_clarification(self) -> None:
        v = triage.triage_task({"title": "Thoughts on the roadmap?"}, _cfg())
        self.assertEqual(v["verdict"], triage.NEEDS_CLARIFICATION)
        self.assertIsNone(v["issue_class"])

    def test_class_outside_allowlist_is_refused(self) -> None:
        # small_feature is a real class but excluded from this allowlist.
        v = triage.triage_task(
            {"title": "Add dark mode", "task_contract": {"acceptance_criteria": ["persists"]}},
            _cfg(allowed="bug_fix"),
        )
        self.assertEqual(v["verdict"], triage.REFUSED)
        self.assertEqual(v["issue_class"], triage.SMALL_FEATURE)
        self.assertIn("not in the autonomy allowlist", v["reason"])

    def test_small_feature_underspecified_needs_clarification(self) -> None:
        v = triage.triage_task({"title": "Add dark mode toggle"}, _cfg())
        self.assertEqual(v["verdict"], triage.NEEDS_CLARIFICATION)
        self.assertEqual(v["issue_class"], triage.SMALL_FEATURE)

    def test_small_feature_with_acceptance_criteria_accepted(self) -> None:
        v = triage.triage_task(
            {"title": "Add dark mode toggle", "task_contract": {"acceptance_criteria": ["persists", "respects OS"]}},
            _cfg(),
        )
        self.assertEqual(v["verdict"], triage.ACCEPTED)

    def test_labels_take_precedence_over_keywords(self) -> None:
        # Body says "refactor" (unclassified) but the dependencies label wins.
        v = triage.triage_task({"title": "big refactor", "labels": ["dependencies"]}, _cfg())
        self.assertEqual(v["issue_class"], triage.DEPENDENCY_BUMP)


class TriageSummaryTest(unittest.TestCase):
    def test_escalation_rate(self) -> None:
        verdicts = [
            {"verdict": triage.ACCEPTED},
            {"verdict": triage.ACCEPTED},
            {"verdict": triage.REFUSED},
            {"verdict": triage.NEEDS_CLARIFICATION},
        ]
        summary = triage.summarize_triage(verdicts)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["accepted"], 2)
        self.assertEqual(summary["escalated"], 2)
        self.assertEqual(summary["escalation_rate"], 0.5)

    def test_empty_is_zero_rate(self) -> None:
        self.assertEqual(triage.summarize_triage([])["escalation_rate"], 0.0)


class TriageEscalationTest(unittest.TestCase):
    def test_escalation_enqueues_dedupe_guarded_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoordinationStore(backend="sqlite", db_path=Path(tmp) / "c.sqlite3")
            store.ensure_schema()
            task = {"task_key": "linear:TAN-9", "task_id": "TAN-9", "title": "Rewrite everything",
                    "source": {"type": "linear", "identifier": "TAN-9"}}
            verdict = {"verdict": triage.REFUSED, "issue_class": "small_feature", "reason": "not allowed"}

            self.assertTrue(triage.escalate_triaged_task(None, store, task, verdict))
            # Dedupe: a second identical escalation does not create a duplicate.
            triage.escalate_triaged_task(None, store, task, verdict)
            pending = store.list_pending_outbox()
            triage_rows = [r for r in pending if r.get("kind") == "linear_issue.comment"]
            self.assertEqual(len(triage_rows), 1)

    def test_accepted_verdict_is_not_escalated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoordinationStore(backend="sqlite", db_path=Path(tmp) / "c.sqlite3")
            store.ensure_schema()
            task = {"task_key": "linear:TAN-1", "source": {"type": "linear"}}
            self.assertFalse(
                triage.escalate_triaged_task(None, store, task, {"verdict": triage.ACCEPTED})
            )


if __name__ == "__main__":
    unittest.main()
