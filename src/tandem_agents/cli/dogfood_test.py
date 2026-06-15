from __future__ import annotations

import unittest

from src.tandem_agents.cli.dogfood import _trigger_timeout_seconds


class DogfoodTimeoutTest(unittest.TestCase):
    def test_trigger_timeout_tracks_wait_seconds_with_bounds(self) -> None:
        self.assertEqual(_trigger_timeout_seconds(10), 30.0)
        self.assertEqual(_trigger_timeout_seconds(180), 180.0)
        self.assertEqual(_trigger_timeout_seconds(600), 300.0)

    def test_trigger_timeout_override_wins(self) -> None:
        self.assertEqual(_trigger_timeout_seconds(600, 45), 45.0)
        self.assertEqual(_trigger_timeout_seconds(600, 0), 1.0)


if __name__ == "__main__":
    unittest.main()
