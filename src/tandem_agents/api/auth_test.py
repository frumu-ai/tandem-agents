from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from src.tandem_agents.api.auth import assert_api_token_configured, resolve_api_token


@contextmanager
def _env(**overrides: str | None):
    """Patch env vars for the duration of the context, restoring previous state."""
    previous: dict[str, str | None] = {}
    for key in overrides:
        previous[key] = os.environ.get(key)
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class AssertApiTokenConfiguredTest(unittest.TestCase):
    def test_strict_mode_with_token_passes(self) -> None:
        with _env(
            ACA_API_REQUIRE_TOKEN="true",
            ACA_API_TOKEN="example-token",
            ACA_API_TOKEN_FILE=None,
        ):
            assert_api_token_configured()  # should not raise
            self.assertEqual(resolve_api_token(), "example-token")

    def test_strict_mode_default_without_token_raises(self) -> None:
        # Default behaviour (env unset) is strict: refuse to start.
        with _env(
            ACA_API_REQUIRE_TOKEN=None,
            ACA_API_TOKEN=None,
            ACA_API_TOKEN_FILE=None,
        ):
            with self.assertRaises(RuntimeError) as cm:
                assert_api_token_configured()
            self.assertIn("ACA_API_TOKEN", str(cm.exception))

    def test_strict_mode_explicit_true_without_token_raises(self) -> None:
        with _env(
            ACA_API_REQUIRE_TOKEN="true",
            ACA_API_TOKEN=None,
            ACA_API_TOKEN_FILE=None,
        ):
            with self.assertRaises(RuntimeError):
                assert_api_token_configured()

    def test_opt_out_allows_missing_token(self) -> None:
        with _env(
            ACA_API_REQUIRE_TOKEN="false",
            ACA_API_TOKEN=None,
            ACA_API_TOKEN_FILE=None,
        ):
            # Must not raise — opted out of strict mode.
            assert_api_token_configured()


if __name__ == "__main__":
    unittest.main()
