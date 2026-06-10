from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.phases.review_verify import _run_engine_command_checks


class ReviewVerifyTest(unittest.TestCase):
    def test_engine_command_checks_run_at_engine_visible_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            repo_path.mkdir()
            host_repo_path = Path("/host/repo")

            with (
                patch(
                    "src.tandem_agents.core.engine.engine.engine_visible_path",
                    return_value=host_repo_path,
                ),
                patch(
                    "src.tandem_agents.core.engine.engine.execute_engine_tool",
                    return_value={
                        "output": "ok\n",
                        "metadata": {"exit_code": 0, "stderr": ""},
                    },
                ) as execute_mock,
            ):
                results = _run_engine_command_checks(
                    SimpleNamespace(),
                    repo_path,
                    ["cargo check -p tandem-meta-harness-eval"],
                )

        self.assertEqual(results[0]["status"], "pass")
        self.assertEqual(results[0]["executor"], "tandem_engine")
        execute_mock.assert_called_once()
        _, tool_name, args = execute_mock.call_args.args
        self.assertEqual(tool_name, "bash")
        self.assertEqual(
            args["command"],
            "cd /host/repo && cargo check -p tandem-meta-harness-eval",
        )


if __name__ == "__main__":
    unittest.main()
