from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.engine.engine_runtime import (
    create_tandem_session,
    engine_session_provider_model,
    engine_visible_path,
)


class EngineRuntimePathTest(unittest.TestCase):
    def test_engine_visible_path_maps_container_root_to_host_root(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "ACA_ROOT": "/workspace/tandem-agents",
                "ACA_ENGINE_HOST_ROOT": "/home/evan/tandem-agents",
            },
            clear=False,
        ):
            self.assertEqual(
                engine_visible_path(Path("/workspace/tandem-agents/runs/run-1")),
                Path("/home/evan/tandem-agents/runs/run-1"),
            )

    def test_engine_visible_path_is_noop_without_host_root(self) -> None:
        with mock.patch.dict("os.environ", {"ACA_ROOT": "/workspace/tandem-agents"}, clear=True):
            path = Path("/workspace/tandem-agents/runs/run-1")
            self.assertEqual(engine_visible_path(path), path)


class EngineRuntimeProviderResolutionTest(unittest.TestCase):
    def _cfg(
        self,
        *,
        env: dict[str, str] | None = None,
        provider: str = "openai",
        model: str = "gpt-4.1-mini",
        provider_source: str = "provider",
        model_source: str = "provider",
    ):
        def provider_for_role(_role: str) -> tuple[str, str]:
            return provider, model

        def provider_for_role_with_source(role: str) -> dict[str, str]:
            return {
                "role": role,
                "provider": provider,
                "model": model,
                "provider_source": provider_source,
                "model_source": model_source,
            }

        return SimpleNamespace(
            env=env or {},
            provider=SimpleNamespace(base_url=""),
            provider_for_role=provider_for_role,
            provider_for_role_with_source=provider_for_role_with_source,
        )

    def test_uses_engine_default_when_no_aca_override_is_present(self) -> None:
        cfg = self._cfg()

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "default": "openai-codex",
                "providers": {"openai-codex": {"default_model": "gpt-5.5", "api_key": "[REDACTED]"}},
            },
        ):
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai-codex")
        self.assertEqual(resolved["model"], "gpt-5.5")
        self.assertEqual(resolved["source"], "engine_default")

    def test_control_panel_global_model_does_not_force_raw_provider(self) -> None:
        cfg = self._cfg(provider="openai", model="gpt-5.5")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "default": "openai-codex",
                "providers": {"openai-codex": {"default_model": "gpt-5.5", "api_key": "[REDACTED]"}},
            },
        ):
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai-codex")
        self.assertEqual(resolved["model"], "gpt-5.5")
        self.assertEqual(resolved["source"], "engine_default")

    def test_aca_env_override_wins_over_engine_default(self) -> None:
        cfg = self._cfg(env={"ACA_PROVIDER": "openai", "ACA_MODEL": "gpt-4.1-mini"})

        with mock.patch("src.tandem_agents.core.engine.engine_runtime._engine_request_json") as request:
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["model"], "gpt-4.1-mini")
        self.assertEqual(resolved["source"], "aca_config")
        request.assert_not_called()

    def test_role_override_wins_over_engine_default(self) -> None:
        cfg = self._cfg(provider="openai", model="gpt-4.1-mini", provider_source="role", model_source="role")

        with mock.patch("src.tandem_agents.core.engine.engine_runtime._engine_request_json") as request:
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["model"], "gpt-4.1-mini")
        self.assertEqual(resolved["source"], "aca_config")
        request.assert_not_called()


class EngineRuntimeSessionCreateTest(unittest.TestCase):
    def test_permission_rules_use_raw_create_payload(self) -> None:
        cfg = SimpleNamespace()
        rules = [{"permission": "bash", "pattern": "*", "action": "allow"}]

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.sdk_available",
            return_value=True,
        ), mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.sdk_create_session"
        ) as sdk_create, mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={"id": "session-raw"},
        ) as request:
            session_id = create_tandem_session(
                cfg,
                title="ACA worker",
                directory=Path("/workspace/repo"),
                provider="openai-codex",
                model="gpt-5.5",
                permission_rules=rules,
            )

        self.assertEqual(session_id, "session-raw")
        sdk_create.assert_not_called()
        request.assert_called_once()
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["workspace_root"], "/workspace/repo")
        self.assertEqual(payload["permission"], rules)


if __name__ == "__main__":
    unittest.main()
