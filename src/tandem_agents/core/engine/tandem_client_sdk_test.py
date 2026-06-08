from __future__ import annotations

import unittest
import unittest.mock
from types import SimpleNamespace

from src.tandem_agents.core.engine.tandem_client_sdk import (
    _extract_event_text_delta,
    _sessions_create_supports_temperature,
)


class SessionsCreateTemperatureSupportTest(unittest.TestCase):
    def test_detects_explicit_temperature_param(self) -> None:
        def create(*, title="", directory=".", provider=None, model=None, temperature=None):
            return None

        self.assertTrue(_sessions_create_supports_temperature(create))

    def test_detects_var_keyword(self) -> None:
        def create(*, title="", directory=".", provider=None, model=None, **kwargs):
            return None

        self.assertTrue(_sessions_create_supports_temperature(create))

    def test_rejects_current_signature_without_temperature(self) -> None:
        # Mirrors the installed tandem-client today (no sampling support).
        def create(*, title="", directory=".", provider=None, model=None):
            return None

        self.assertFalse(_sessions_create_supports_temperature(create))


class SdkCreateSessionPassthroughTest(unittest.TestCase):
    def _fake_client(self, supports_temperature: bool):
        captured: dict = {}

        if supports_temperature:
            def create(*, title="", directory=".", provider=None, model=None, temperature=None):
                captured.update(
                    title=title, directory=directory, provider=provider, model=model, temperature=temperature
                )
                return "session-1"
        else:
            def create(*, title="", directory=".", provider=None, model=None):
                captured.update(title=title, directory=directory, provider=provider, model=model)
                return "session-1"

        client = SimpleNamespace(sessions=SimpleNamespace(create=create))
        return client, captured

    def _call(self, client, temperature):
        from src.tandem_agents.core.engine import tandem_client_sdk as sdk

        with unittest.mock.patch.object(
            sdk, "with_sync_tandem_client", side_effect=lambda cfg, fn: fn(client)
        ):
            return sdk.sdk_create_session(
                cfg=SimpleNamespace(),
                title="t",
                directory="/d",
                provider="openai",
                model="m",
                temperature=temperature,
            )

    def test_forwards_temperature_when_supported(self) -> None:
        client, captured = self._fake_client(supports_temperature=True)
        self.assertEqual(self._call(client, 0.0), "session-1")
        self.assertEqual(captured["temperature"], 0.0)

    def test_omits_temperature_when_unsupported(self) -> None:
        client, captured = self._fake_client(supports_temperature=False)
        self.assertEqual(self._call(client, 0.0), "session-1")
        self.assertNotIn("temperature", captured)

    def test_omits_temperature_when_none(self) -> None:
        client, captured = self._fake_client(supports_temperature=True)
        self._call(client, None)
        self.assertIsNone(captured.get("temperature"))


class TandemClientSdkEventTextTest(unittest.TestCase):
    def test_extracts_message_part_updated_text(self) -> None:
        event = SimpleNamespace(
            type="message.part.updated",
            properties={"part": {"text": "worker explanation"}},
        )

        self.assertEqual(_extract_event_text_delta(event), "worker explanation")

    def test_extracts_message_part_updated_delta_text(self) -> None:
        event = SimpleNamespace(
            type="message.part.updated",
            properties={"delta": {"text": "delta text"}},
        )

        self.assertEqual(_extract_event_text_delta(event), "delta text")

    def test_extracts_legacy_session_response_delta(self) -> None:
        event = SimpleNamespace(
            type="session.response",
            properties={"delta": "legacy text"},
        )

        self.assertEqual(_extract_event_text_delta(event), "legacy text")
