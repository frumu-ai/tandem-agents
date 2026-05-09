from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("aca.api.auth")
security = HTTPBearer()


def resolve_api_token() -> str:
    expected_token = os.environ.get("ACA_API_TOKEN")
    if not expected_token:
        token_file = os.environ.get("ACA_API_TOKEN_FILE", "").strip()
        if token_file:
            token_path = Path(token_file).expanduser()
            if not token_path.is_absolute():
                root = Path(os.environ.get("ACA_ROOT", ".")).expanduser()
                token_path = (root / token_path).resolve()
            try:
                expected_token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                expected_token = ""
    return str(expected_token or "").strip()


def _strict_token_required() -> bool:
    """Whether the API must refuse to start without a configured token.

    Default: True. Set ACA_API_REQUIRE_TOKEN=false to opt out (development /
    explicit insecure-mode deployments). The default-secure posture means a
    fresh deployment that forgets ACA_API_TOKEN fails fast at startup
    instead of silently accepting any credential.
    """
    raw = os.environ.get("ACA_API_REQUIRE_TOKEN", "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def assert_api_token_configured() -> None:
    """Raise RuntimeError at app startup if strict mode is on and no token is set.

    Call from a FastAPI startup hook so the server refuses to come up rather
    than silently allowing every request through.
    """
    if not _strict_token_required():
        if not resolve_api_token():
            logger.warning(
                "ACA_API_REQUIRE_TOKEN=false and ACA_API_TOKEN is empty. "
                "API will accept any credential — this is insecure and only "
                "appropriate for local development."
            )
        return
    if not resolve_api_token():
        raise RuntimeError(
            "ACA_API_TOKEN is not set and ACA_API_REQUIRE_TOKEN=true (default). "
            "Refusing to start an unauthenticated API. Set ACA_API_TOKEN (or "
            "ACA_API_TOKEN_FILE) before starting the server, or explicitly opt "
            "out with ACA_API_REQUIRE_TOKEN=false (insecure)."
        )


def get_token(auth: HTTPAuthorizationCredentials = Depends(security)) -> str:
    expected_token = resolve_api_token()
    if not expected_token:
        # Strict mode would have prevented startup. Reaching this branch means
        # the operator explicitly opted out; warn loudly per request so the
        # situation is visible in logs.
        logger.warning("ACA_API_TOKEN is not set. API is insecure!")
        return auth.credentials
    if auth.credentials != expected_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Token")
    return auth.credentials
