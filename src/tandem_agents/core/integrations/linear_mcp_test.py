from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.integrations.linear_mcp import linear_list_issues


class LinearMcpIntegrationTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "task_source:",
                    "  type: linear",
                    "  team: TAN",
                    "repository:",
                    "  path: /workspace/repo",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-4.1-mini",
                    "linear_mcp:",
                    "  enabled: true",
                    "  server: linear",
                    "swarm:",
                    "  enabled: false",
                    "output:",
                    "  root: runs",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def test_linear_list_issues_prefers_linear_native_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            calls: list[dict[str, object]] = []

            def fake_execute(_cfg, aliases, args):
                calls.append(args)
                return {"metadata": {"result": {"issues": [{"id": "lin-1", "identifier": "TAN-1"}]}}}

            with patch("src.tandem_agents.core.integrations.linear_mcp._execute_linear_tool", side_effect=fake_execute):
                issues = linear_list_issues(
                    cfg,
                    team="Tandem",
                    project="Runtime",
                    statuses="Backlog,Todo",
                    labels="Coder Runtime",
                    query="intake",
                )

            self.assertEqual(issues[0]["identifier"], "TAN-1")
            self.assertEqual(
                calls[0],
                {
                    "limit": 50,
                    "team": "Tandem",
                    "project": "Runtime",
                    "query": "intake",
                    "state": "Backlog,Todo",
                    "label": "Coder Runtime",
                },
            )


if __name__ == "__main__":
    unittest.main()
