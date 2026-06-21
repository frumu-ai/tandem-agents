from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.engine.engine_runtime import (
    create_tandem_session,
    engine_empty_response_fallback_provider_model,
    engine_provider_smoke_report,
    engine_session_readiness_report,
    engine_session_provider_model,
    engine_visible_path,
    _engine_provider_smoke_timeout_seconds,
    _engine_session_readiness_timeout_seconds,
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



class EngineRuntimeProviderSmokeTest(unittest.TestCase):
    def _cfg(self, *, env: dict[str, str] | None = None):
        def provider_for_role(_role: str) -> tuple[str, str]:
            return "openai", "gpt-4.1-mini"

        def provider_for_role_with_source(role: str) -> dict[str, str]:
            return {
                "role": role,
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "provider_source": "provider",
                "model_source": "provider",
            }

        return SimpleNamespace(
            root=Path("/workspace/tandem-agents"),
            env={"OPENAI_API_KEY": "sk-test", **(env or {})},
            provider=SimpleNamespace(base_url="", fallback_provider="", fallback_model=""),
            provider_for_role=provider_for_role,
            provider_for_role_with_source=provider_for_role_with_source,
        )

    def test_smoke_report_accepts_visible_assistant_transcript(self) -> None:
        cfg = self._cfg()
        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.create_tandem_session",
            return_value="session-1",
        ) as create, mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.prompt_tandem_session_sync",
            return_value={
                "messages": [
                    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "prompt"}]},
                    {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "ACA_SMOKE_OK"}]},
                ]
            },
        ) as prompt, mock.patch("src.tandem_agents.core.engine.engine_runtime.delete_tandem_session") as delete:
            report = engine_provider_smoke_report(cfg, role="worker", directory=Path("/repo"))

        self.assertTrue(report["ok"])
        self.assertEqual(report["reason"], "ok")
        create.assert_called_once()
        prompt.assert_called_once()
        delete.assert_called_once_with(cfg, "session-1")

    def test_smoke_report_fails_when_engine_returns_only_user_prompt(self) -> None:
        cfg = self._cfg()
        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.create_tandem_session",
            return_value="session-1",
        ), mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.prompt_tandem_session_sync",
            return_value={
                "messages": [
                    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "ACA_SMOKE_OK"}]},
                ]
            },
        ), mock.patch("src.tandem_agents.core.engine.engine_runtime.delete_tandem_session"):
            report = engine_provider_smoke_report(cfg, role="worker", directory=Path("/repo"))

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "empty_or_unexpected_transcript")
        self.assertEqual(report["text_length"], 0)

    def test_smoke_report_preserves_exception_class_when_message_is_empty(self) -> None:
        cfg = self._cfg()
        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime.create_tandem_session",
            side_effect=TimeoutError(),
        ):
            report = engine_provider_smoke_report(cfg, role="worker", directory=Path("/repo"))

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "exception")
        self.assertEqual(report["error"], "TimeoutError")
        self.assertEqual(report["error_class"], "TimeoutError")

    def test_smoke_report_can_be_disabled(self) -> None:
        cfg = self._cfg(env={"ACA_ENGINE_PROVIDER_SMOKE_ENABLED": "false"})
        with mock.patch("src.tandem_agents.core.engine.engine_runtime.create_tandem_session") as create:
            report = engine_provider_smoke_report(cfg, role="worker", directory=Path("/repo"))

        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped"])
        create.assert_not_called()

    def test_smoke_timeout_defaults_to_slow_local_engine_budget(self) -> None:
        cfg = self._cfg()

        self.assertEqual(_engine_provider_smoke_timeout_seconds(cfg), 90.0)

    def test_session_readiness_report_accepts_session_list(self) -> None:
        cfg = self._cfg()
        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value=[],
        ) as request:
            report = engine_session_readiness_report(cfg)

        self.assertTrue(report["ok"])
        self.assertEqual(report["reason"], "ok")
        request.assert_called_once_with(cfg, "/session", timeout=2.0)

    def test_session_readiness_report_preserves_timeout_class(self) -> None:
        cfg = self._cfg()
        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            side_effect=TimeoutError(),
        ):
            report = engine_session_readiness_report(cfg)

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "exception")
        self.assertEqual(report["error"], "TimeoutError")
        self.assertEqual(report["error_class"], "TimeoutError")

    def test_session_readiness_timeout_can_be_configured(self) -> None:
        cfg = self._cfg(env={"ACA_ENGINE_SESSION_READINESS_TIMEOUT_SECONDS": "5"})

        self.assertEqual(_engine_session_readiness_timeout_seconds(cfg), 5.0)

    def test_smoke_timeout_override_allows_longer_local_engine_budget(self) -> None:
        cfg = self._cfg(env={"ACA_ENGINE_PROVIDER_SMOKE_TIMEOUT_SECONDS": "240"})

        self.assertEqual(_engine_provider_smoke_timeout_seconds(cfg), 180.0)


class EngineRuntimeProviderResolutionTest(unittest.TestCase):
    def _cfg(
        self,
        *,
        env: dict[str, str] | None = None,
        provider: str = "openai",
        model: str = "gpt-4.1-mini",
        provider_source: str = "provider",
        model_source: str = "provider",
        fallback_provider: str = "",
        fallback_model: str = "",
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
            provider=SimpleNamespace(
                base_url="",
                fallback_provider=fallback_provider,
                fallback_model=fallback_model,
            ),
            provider_for_role=provider_for_role,
            provider_for_role_with_source=provider_for_role_with_source,
        )

    def test_uses_engine_default_when_no_aca_override_is_present(self) -> None:
        cfg = self._cfg(provider_source="default", model_source="default")

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

    def test_uses_credentialed_engine_provider_when_selected_default_missing(self) -> None:
        cfg = self._cfg(provider="openai-codex", model="gpt-5.5", provider_source="default", model_source="default")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "providers": {
                    "mcp_header::github::authorization": {"api_key": "[REDACTED]"},
                    "anthropic": {"default_model": "claude-sonnet-4-6", "api_key": "[REDACTED]"},
                    "openrouter": {"api_key": "[REDACTED]"},
                }
            },
        ):
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "anthropic")
        self.assertEqual(resolved["model"], "claude-sonnet-4-6")
        self.assertEqual(resolved["source"], "engine_default")

    def test_control_panel_global_model_wins_over_engine_default(self) -> None:
        cfg = self._cfg(env={"OPENAI_API_KEY": "sk-test"}, provider="openai", model="gpt-5.5")

        with mock.patch("src.tandem_agents.core.engine.engine_runtime._engine_request_json") as request:
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["model"], "gpt-5.5")
        self.assertEqual(resolved["source"], "aca_config")
        request.assert_not_called()

    def test_configured_route_without_credentials_uses_credentialed_engine_default(self) -> None:
        cfg = self._cfg(provider="openai", model="gpt-4.1-mini")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "default": "openai-codex",
                "providers": {
                    "openai": {"default_model": "gpt-4.1-mini"},
                    "openai-codex": {"default_model": "gpt-5.5", "api_key": "[REDACTED]"},
                },
            },
        ):
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai-codex")
        self.assertEqual(resolved["model"], "gpt-5.5")
        self.assertEqual(resolved["source"], "engine_default_missing_config_credentials")


    def test_aca_env_override_wins_over_engine_default(self) -> None:
        cfg = self._cfg(env={"ACA_PROVIDER": "openai", "ACA_MODEL": "gpt-4.1-mini", "OPENAI_API_KEY": "sk-test"})

        with mock.patch("src.tandem_agents.core.engine.engine_runtime._engine_request_json") as request:
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["model"], "gpt-4.1-mini")
        self.assertEqual(resolved["source"], "aca_config")
        request.assert_not_called()

    def test_role_override_wins_over_engine_default(self) -> None:
        cfg = self._cfg(
            env={"OPENAI_API_KEY": "sk-test"},
            provider="openai",
            model="gpt-4.1-mini",
            provider_source="role",
            model_source="role",
        )

        with mock.patch("src.tandem_agents.core.engine.engine_runtime._engine_request_json") as request:
            resolved = engine_session_provider_model(cfg, "worker")

        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["model"], "gpt-4.1-mini")
        self.assertEqual(resolved["source"], "aca_config")
        request.assert_not_called()

    def test_empty_response_fallback_prefers_configured_fallback_pair(self) -> None:
        cfg = self._cfg(fallback_provider="openai", fallback_model="gpt-4.1-mini")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "providers": {
                    "openai": {"default_model": "gpt-4.1-mini", "api_key": "[REDACTED]"},
                }
            },
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertEqual(
            resolved,
            {"provider": "openai", "model": "gpt-4.1-mini", "source": "aca_fallback_provider"},
        )

    def test_empty_response_fallback_uses_explicit_aca_config_alternate(self) -> None:
        cfg = self._cfg(provider="openai", model="gpt-4.1-mini")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "providers": {
                    "openai": {"default_model": "gpt-4.1-mini", "api_key": "[REDACTED]"},
                }
            },
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertEqual(
            resolved,
            {"provider": "openai", "model": "gpt-4.1-mini", "source": "aca_config_alternate"},
        )

    def test_empty_response_fallback_skips_uncredentialed_config_alternate(self) -> None:
        cfg = self._cfg(provider="openai", model="gpt-4.1-mini")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "providers": {
                    "openai": {"default_model": "gpt-4.1-mini"},
                    "openai-codex": {"default_model": "gpt-5.5", "api_key": "[REDACTED]"},
                }
            },
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertIsNone(resolved)

    def test_empty_response_fallback_uses_credentialed_engine_registry_alternate_when_enabled(self) -> None:
        cfg = self._cfg(
            env={"ACA_ALLOW_ENGINE_REGISTRY_EMPTY_RESPONSE_FALLBACK": "true"},
            provider="openai-codex",
            model="gpt-5.5",
            provider_source="default",
            model_source="default",
        )

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "providers": {
                    "mcp_header::github::authorization": {"api_key": "[REDACTED]"},
                    "openai-codex": {"default_model": "gpt-5.5", "api_key": "[REDACTED]"},
                    "openrouter": {"default_model": "openai/gpt-5.4", "api_key": "[REDACTED]"},
                }
            },
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertEqual(
            resolved,
            {"provider": "openrouter", "model": "openai/gpt-5.4", "source": "engine_registry_alternate"},
        )

    def test_empty_response_fallback_skips_engine_registry_alternate_by_default(self) -> None:
        cfg = self._cfg(provider="openai-codex", model="gpt-5.5", provider_source="default", model_source="default")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={
                "providers": {
                    "openai-codex": {"default_model": "gpt-5.5", "api_key": "[REDACTED]"},
                    "openrouter": {"default_model": "openai/gpt-5.4", "api_key": "[REDACTED]"},
                }
            },
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertIsNone(resolved)


    def test_empty_response_fallback_uses_local_secret_for_config_alternate(self) -> None:
        cfg = self._cfg(env={"OPENAI_API_KEY": "sk-test"}, provider="openai", model="gpt-4.1-mini")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            return_value={"providers": {"openai": {"default_model": "gpt-4.1-mini"}}},
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertEqual(
            resolved,
            {"provider": "openai", "model": "gpt-4.1-mini", "source": "aca_config_alternate"},
        )

    def test_empty_response_fallback_ignores_built_in_defaults(self) -> None:
        cfg = self._cfg(provider_source="default", model_source="default")

        with mock.patch(
            "src.tandem_agents.core.engine.engine_runtime._engine_request_json",
            side_effect=RuntimeError("engine config unavailable"),
        ):
            resolved = engine_empty_response_fallback_provider_model(
                cfg,
                "worker",
                current_provider="openai-codex",
                current_model="gpt-5.5",
            )

        self.assertIsNone(resolved)


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
