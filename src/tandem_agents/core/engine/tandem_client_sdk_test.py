from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.tandem_agents.core.engine.tandem_client_sdk import _extract_event_text_delta


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
