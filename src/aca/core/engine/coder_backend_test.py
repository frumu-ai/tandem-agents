from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.aca.core.engine import coder_backend


class _Config(SimpleNamespace):
    def provider_for_role(self, _role: str) -> tuple[str, str]:
        return ("openai", "gpt-5.5")


def _cfg(*, wait_timeout: int = 3600, poll_interval: int = 15) -> _Config:
    return _Config(
        execution=SimpleNamespace(
            backend="coder",
            coder_wait_timeout_seconds=wait_timeout,
            coder_poll_interval_seconds=poll_interval,
        ),
        task_source=SimpleNamespace(type="github_project"),
        repository=SimpleNamespace(default_branch="main"),
        github_mcp=SimpleNamespace(enabled=False, scope="none"),
    )


def _task() -> dict[str, object]:
    return {
        "title": "Fix stale cache invalidation",
        "source": {
            "type": "github_project",
            "project": "123",
            "issue_number": 42,
            "issue_url": "https://github.com/acme/demo/issues/42",
        },
    }


class CoderBackendLongRunningTest(unittest.TestCase):
    def test_coder_run_uses_configured_wait_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "README.md").write_text("# Demo\n", encoding="utf-8")
            repo = {"path": str(repo_path), "slug": "acme/demo", "dirty": False}
            cfg = _cfg(wait_timeout=7200, poll_interval=30)

            with (
                patch.object(coder_backend, "sdk_available", return_value=True),
                patch.object(coder_backend, "sdk_coder_create_run", return_value={"ok": True}) as create_run,
                patch.object(coder_backend, "sdk_coder_execute_all", return_value={"run": {"status": "running"}}),
                patch.object(
                    coder_backend,
                    "sdk_coder_get_run",
                    return_value={"run": {"status": "completed", "phase": "handoff"}},
                ),
            ):
                result = coder_backend.execute_coder_run(
                    cfg,
                    run_id="run-1",
                    repo=repo,
                    task=_task(),
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["wait_timeout_seconds"], 7200)
            self.assertEqual(result["poll_interval_seconds"], 30)
            create_payload = create_run.call_args.args[1]
            self.assertEqual(create_payload["workflow_mode"], "issue_fix")
            self.assertEqual(create_payload["github_ref"]["number"], 42)

    def test_non_terminal_run_reports_monitor_timeout_not_engine_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "README.md").write_text("# Demo\n", encoding="utf-8")
            repo = {"path": str(repo_path), "slug": "acme/demo", "dirty": False}
            cfg = _cfg(wait_timeout=1, poll_interval=1)

            with (
                patch.object(coder_backend, "sdk_available", return_value=True),
                patch.object(coder_backend, "sdk_coder_create_run", return_value={"ok": True}),
                patch.object(coder_backend, "sdk_coder_execute_all", return_value={"run": {"status": "running"}}),
                patch.object(
                    coder_backend,
                    "sdk_coder_get_run",
                    return_value={"run": {"status": "running", "phase": "coding"}},
                ),
                patch.object(coder_backend.time, "time", side_effect=[0.0, 0.0, 0.0, 1.0]),
                patch.object(coder_backend.time, "sleep", return_value=None),
            ):
                result = coder_backend.execute_coder_run(
                    cfg,
                    run_id="run-1",
                    repo=repo,
                    task=_task(),
                )

            self.assertEqual(result["status"], "running")
            self.assertTrue(result["monitor_timeout"])
            self.assertIn("did not reach a terminal state", result["last_error"])
            self.assertIn("still be executing", result["last_error"])


if __name__ == "__main__":
    unittest.main()
