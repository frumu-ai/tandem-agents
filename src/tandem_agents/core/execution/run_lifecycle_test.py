from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.config.config_types import DEFAULT_MODEL, DEFAULT_PROVIDER
from src.tandem_agents.core.execution.run_lifecycle import build_swarm_config_dict


def _config(env: dict[str, str]):
    with tempfile.TemporaryDirectory() as tmp:
        return resolve_config(Path(tmp), env=env)


class ProviderRoleResolutionTest(unittest.TestCase):
    def test_unconfigured_role_reports_default_source(self) -> None:
        cfg = _config({})
        resolved = cfg.provider_for_role_with_source("worker")
        self.assertEqual(resolved["provider"], DEFAULT_PROVIDER)
        self.assertEqual(resolved["model"], DEFAULT_MODEL)
        self.assertEqual(resolved["model_source"], "default")
        self.assertEqual(resolved["provider_source"], "default")

    def test_global_model_reports_provider_source(self) -> None:
        cfg = _config({"ACA_PROVIDER": "anthropic", "ACA_MODEL": "claude-x"})
        resolved = cfg.provider_for_role_with_source("reviewer")
        self.assertEqual(resolved["model"], "claude-x")
        self.assertEqual(resolved["model_source"], "provider")
        # provider_for_role stays consistent with the source-aware resolver.
        self.assertEqual(cfg.provider_for_role("reviewer"), ("anthropic", "claude-x"))

    def test_role_override_reports_role_source_and_wins(self) -> None:
        cfg = _config(
            {
                "ACA_PROVIDER": "openai",
                "ACA_MODEL": "global-model",
                "ACA_WORKER_MODEL": "worker-model",
            }
        )
        resolved = cfg.provider_for_role_with_source("worker")
        self.assertEqual(resolved["model"], "worker-model")
        self.assertEqual(resolved["model_source"], "role")
        # A different role with no override still uses the global model.
        self.assertEqual(cfg.provider_for_role_with_source("tester")["model"], "global-model")

    def test_shared_model_ignores_role_override(self) -> None:
        cfg = _config(
            {
                "ACA_PROVIDER": "openai",
                "ACA_MODEL": "global-model",
                "ACA_WORKER_MODEL": "worker-model",
                "ACA_SHARED_MODEL": "true",
            }
        )
        resolved = cfg.provider_for_role_with_source("worker")
        self.assertEqual(resolved["model"], "global-model")
        self.assertEqual(resolved["model_source"], "provider")


class SwarmConfigDictTest(unittest.TestCase):
    def test_flags_default_fallback_when_unconfigured(self) -> None:
        cfg = _config({})
        with self.assertLogs("aca.run_lifecycle", level="WARNING") as logs:
            payload = build_swarm_config_dict(cfg)
        self.assertTrue(payload["using_default_model_fallback"])
        self.assertEqual(
            set(payload["default_model_fallback_roles"]),
            {"manager", "worker", "reviewer", "tester"},
        )
        self.assertEqual(payload["worker"]["model"], DEFAULT_MODEL)
        self.assertEqual(payload["worker"]["model_source"], "default")
        self.assertTrue(any("No model configured" in line for line in logs.output))

    def test_no_fallback_flag_when_model_configured(self) -> None:
        cfg = _config({"ACA_PROVIDER": "openai", "ACA_MODEL": "configured-model"})
        payload = build_swarm_config_dict(cfg)
        self.assertFalse(payload["using_default_model_fallback"])
        self.assertEqual(payload["default_model_fallback_roles"], [])
        self.assertEqual(payload["manager"]["model"], "configured-model")
        self.assertEqual(payload["manager"]["model_source"], "provider")
        # Backwards-compatible keys preserved.
        self.assertEqual(payload["manager"]["provider"], "openai")


if __name__ == "__main__":
    unittest.main()
