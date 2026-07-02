from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tandem_agents.core import budget
from src.tandem_agents.core.coordination.coordination import CoordinationStore


def _cfg(*, max_tokens=0, max_cost_usd=0.0, max_coder_executions=0):
    return SimpleNamespace(
        budget=SimpleNamespace(
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
            max_coder_executions=max_coder_executions,
        )
    )


class ExtractUsageTest(unittest.TestCase):
    def test_reads_total_tokens_from_run(self) -> None:
        usage = budget.extract_usage({"run": {"usage": {"total_tokens": 1234, "cost_usd": 0.5}}})
        self.assertEqual(usage["total_tokens"], 1234)
        self.assertEqual(usage["cost_usd"], 0.5)

    def test_sums_input_and_output_tokens(self) -> None:
        usage = budget.extract_usage({"usage": {"input_tokens": 100, "output_tokens": 50}})
        self.assertEqual(usage["total_tokens"], 150)

    def test_does_not_double_count_echoed_totals(self) -> None:
        # Same totals echoed in run and execute_response -> take max, not sum.
        result = {
            "run": {"total_tokens": 900, "cost_usd": 1.0},
            "execute_response": {"run": {"total_tokens": 900, "cost_usd": 1.0}},
        }
        usage = budget.extract_usage(result)
        self.assertEqual(usage["total_tokens"], 900)
        self.assertEqual(usage["cost_usd"], 1.0)

    def test_missing_usage_degrades_to_zero(self) -> None:
        usage = budget.extract_usage({"run": {"status": "completed"}})
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(usage["cost_usd"], 0.0)

    def test_ignores_bool_values(self) -> None:
        usage = budget.extract_usage({"usage": {"total_tokens": True}})
        self.assertEqual(usage["total_tokens"], 0)


class BudgetStatusTest(unittest.TestCase):
    def test_within_budget(self) -> None:
        exhausted, reason = budget.budget_status(
            {"total_tokens": 10, "cost_usd": 0.1, "coder_executions": 1},
            _cfg(max_tokens=100, max_cost_usd=1.0, max_coder_executions=8),
        )
        self.assertFalse(exhausted)
        self.assertEqual(reason, "")

    def test_token_axis_exhausted(self) -> None:
        exhausted, reason = budget.budget_status(
            {"total_tokens": 100, "coder_executions": 1},
            _cfg(max_tokens=100),
        )
        self.assertTrue(exhausted)
        self.assertIn("tokens", reason)

    def test_execution_axis_is_hard_backstop_without_tokens(self) -> None:
        # Even with no token data, the execution count caps runaway spend.
        exhausted, reason = budget.budget_status(
            {"total_tokens": 0, "coder_executions": 8},
            _cfg(max_coder_executions=8),
        )
        self.assertTrue(exhausted)
        self.assertIn("coder_executions", reason)

    def test_zero_axis_is_disabled(self) -> None:
        exhausted, _ = budget.budget_status(
            {"total_tokens": 10_000_000, "cost_usd": 999.0, "coder_executions": 999},
            _cfg(max_tokens=0, max_cost_usd=0.0, max_coder_executions=0),
        )
        self.assertFalse(exhausted)

    def test_no_budget_attr_is_safe(self) -> None:
        exhausted, reason = budget.budget_status({"total_tokens": 5}, SimpleNamespace())
        self.assertFalse(exhausted)
        self.assertEqual(reason, "")


class SpendLedgerTest(unittest.TestCase):
    def test_record_accumulates_across_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoordinationStore(backend="sqlite", db_path=Path(tmp) / "coordination.sqlite3")
            store.ensure_schema()
            store.record_run_start(run_id="run-1", task={"task_id": "t"}, repo={"slug": "acme/demo"}, worker_id="w", host_id="h", lease_id="l", branch_name="b")

            spend = budget.record_coder_spend(store, "run-1", {"run": {"usage": {"total_tokens": 100, "cost_usd": 0.2}}})
            self.assertEqual(spend, {"total_tokens": 100, "cost_usd": 0.2, "coder_executions": 1})

            spend = budget.record_coder_spend(store, "run-1", {"run": {"usage": {"total_tokens": 50, "cost_usd": 0.1}}})
            self.assertEqual(spend["total_tokens"], 150)
            self.assertAlmostEqual(spend["cost_usd"], 0.3)
            self.assertEqual(spend["coder_executions"], 2)

            # Persisted and reloadable.
            self.assertEqual(budget.load_issue_spend(store, "run-1")["coder_executions"], 2)

    def test_load_defaults_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CoordinationStore(backend="sqlite", db_path=Path(tmp) / "coordination.sqlite3")
            store.ensure_schema()
            store.record_run_start(run_id="run-x", task={"task_id": "t"}, repo={"slug": "acme/demo"}, worker_id="w", host_id="h", lease_id="l", branch_name="b")
            self.assertEqual(budget.load_issue_spend(store, "run-x"), budget.empty_spend())


if __name__ == "__main__":
    unittest.main()
