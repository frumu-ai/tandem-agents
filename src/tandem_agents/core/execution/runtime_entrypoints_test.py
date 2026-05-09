from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.execution.runtime_entrypoints import runtime_role


class RuntimeEntrypointsTest(unittest.TestCase):
    def _config(self, root: Path, *, role: str | None = None):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        env_lines = [
            "ACA_TASK_SOURCE_TYPE=manual",
            "ACA_TASK_SOURCE_PROMPT=Do the thing",
            "ACA_REPO_SLUG=frumu-ai/example",
            "ACA_PROVIDER=openai",
            "ACA_MODEL=gpt-4.1-mini",
        ]
        if role:
            env_lines.append(f"ACA_COORDINATION_ROLE={role}")
        (root / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        (root / "agent.yaml").write_text(
            "\n".join(
                [
                    "agent:",
                    "  name: ACA",
                    "task_source:",
                    "  type: manual",
                    "  prompt: Do the thing",
                    "repository:",
                    "  slug: frumu-ai/example",
                    "provider:",
                    "  id: openai",
                    "  model: gpt-4.1-mini",
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

    def test_defaults_to_coordinator_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            self.assertEqual(runtime_role(cfg), "coordinator")

    def test_honors_worker_role_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp), role="worker")
            self.assertEqual(runtime_role(cfg), "worker")


if __name__ == "__main__":
    unittest.main()
