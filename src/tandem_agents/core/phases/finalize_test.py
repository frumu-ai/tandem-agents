from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.phases.finalize import _enqueue_and_dispatch_pr


class _Coordination:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue_outbox(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


class FinalizePhaseTest(unittest.TestCase):
    def _ctx(self, root: Path) -> SimpleNamespace:
        status_path = root / "status.json"
        return SimpleNamespace(
            run_id="run-1",
            task={"task_id": "TAN-1", "title": "Create PR"},
            branch_name="aca/test",
            coordination=_Coordination(),
            cfg=SimpleNamespace(repository=SimpleNamespace(default_branch="main", slug="acme/demo")),
            layout={"summary": root / "summary.md", "status": status_path, "events": root / "events.jsonl"},
            blackboard={},
            status={"task": {}},
        )

    def test_enqueue_and_dispatch_pr_returns_false_without_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core._dispatch_outbox_now",
                return_value={"items": [{"kind": "github_pull_request.create", "status": "failed"}]},
            ):
                self.assertFalse(_enqueue_and_dispatch_pr(ctx, "diff"))

    def test_enqueue_and_dispatch_pr_returns_true_with_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core._dispatch_outbox_now",
                return_value={
                    "items": [
                        {
                            "kind": "github_pull_request.create",
                            "status": "dispatched",
                            "pr_url": "https://github.com/acme/demo/pull/1",
                            "payload": {"run_id": "run-1"},
                        }
                    ]
                },
            ):
                self.assertTrue(_enqueue_and_dispatch_pr(ctx, "diff"))
            self.assertEqual(ctx.blackboard["pull_request"], "https://github.com/acme/demo/pull/1")


if __name__ == "__main__":
    unittest.main()
