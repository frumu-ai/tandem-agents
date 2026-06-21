from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from src.tandem_agents.config.config import DEFAULT_MODEL, DEFAULT_PROVIDER, ResolvedConfig
from src.tandem_agents.core.engine.process_utils import command_exists
from src.tandem_agents.core.engine.tandem_client_sdk import (
    sdk_available,
    sdk_connect_mcp,
    sdk_create_session,
    sdk_delete_session,
    sdk_disconnect_mcp,
    sdk_execute_tool,
    sdk_list_mcp_servers,
    sdk_list_permissions,
    sdk_list_tool_ids,
    sdk_refresh_mcp,
    sdk_reply_permission,
    sdk_set_mcp_enabled,
)
from src.tandem_agents.runtime.state import now_ms

PROVIDER_SECRET_ENV_BY_ID = {
    "anthropic": "ANTHROPIC_API_KEY",
    "cohere": "COHERE_API_KEY",
    "groq": "GROQ_API_KEY",
    "minimax": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
}

ROLE_PROVIDER_ENV_NAMES = {
    "manager": ("ACA_MANAGER_PROVIDER", "AUTOCODER_MANAGER_PROVIDER"),
    "worker": ("ACA_WORKER_PROVIDER", "AUTOCODER_WORKER_PROVIDER"),
    "reviewer": ("ACA_REVIEWER_PROVIDER", "AUTOCODER_REVIEWER_PROVIDER"),
    "tester": ("ACA_TESTER_PROVIDER", "AUTOCODER_TESTER_PROVIDER"),
}
ROLE_MODEL_ENV_NAMES = {
    "manager": ("ACA_MANAGER_MODEL", "AUTOCODER_MANAGER_MODEL"),
    "worker": ("ACA_WORKER_MODEL", "AUTOCODER_WORKER_MODEL"),
    "reviewer": ("ACA_REVIEWER_MODEL", "AUTOCODER_REVIEWER_MODEL"),
    "tester": ("ACA_TESTER_MODEL", "AUTOCODER_TESTER_MODEL"),
}



def _engine_text_from_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        chunks: list[str] = []
        for key in ("delta", "text", "content", "message", "parts", "response", "output", "stdout"):
            if key in value:
                chunks.append(_engine_text_from_part(value.get(key)))
        return "".join(chunk for chunk in chunks if chunk)
    if isinstance(value, list):
        return "".join(_engine_text_from_part(item) for item in value)
    return ""


def _engine_text_from_messages(messages: Any) -> str:
    if hasattr(messages, "model_dump"):
        messages = messages.model_dump(exclude_none=True)
    if isinstance(messages, dict):
        for key in ("messages", "message", "response", "output", "stdout", "text", "content", "parts"):
            text = _engine_text_from_messages(messages.get(key))
            if text.strip():
                return text.strip()
        return ""
    if not isinstance(messages, list):
        return _engine_text_from_part(messages).strip()
    for message in reversed(messages):
        if hasattr(message, "model_dump"):
            message = message.model_dump(exclude_none=True)
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = str(message.get("role") or info.get("role") or "").strip().lower()
        if role and role != "assistant":
            continue
        text = _engine_text_from_part(message.get("parts") or message.get("content") or message)
        if text.strip():
            return text.strip()
    return ""


def _engine_provider_smoke_enabled(cfg: ResolvedConfig) -> bool:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_PROVIDER_SMOKE_ENABLED", "") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _engine_provider_smoke_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_PROVIDER_SMOKE_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return min(180.0, max(3.0, float(raw)))
        except ValueError:
            pass
    return 90.0


def _engine_session_readiness_enabled(cfg: ResolvedConfig) -> bool:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_SESSION_READINESS_ENABLED", "") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _engine_session_readiness_timeout_seconds(cfg: ResolvedConfig) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_ENGINE_SESSION_READINESS_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return min(10.0, max(0.5, float(raw)))
        except ValueError:
            pass
    return 2.0


def engine_session_readiness_report(cfg: ResolvedConfig) -> dict[str, Any]:
    if not _engine_session_readiness_enabled(cfg):
        return {"ok": True, "skipped": True, "reason": "disabled"}
    timeout_seconds = _engine_session_readiness_timeout_seconds(cfg)
    tandem_cfg = getattr(cfg, "tandem", None)
    base_url = str(getattr(tandem_cfg, "base_url", "") or "")
    try:
        response = _engine_request_json(cfg, "/session", timeout=timeout_seconds)
        return {
            "ok": isinstance(response, (list, dict)),
            "reason": "ok" if isinstance(response, (list, dict)) else "unexpected_payload",
            "timeout_seconds": timeout_seconds,
            "base_url": base_url,
        }
    except Exception as exc:
        error_class = exc.__class__.__name__
        error_text = str(exc).strip()
        return {
            "ok": False,
            "reason": "exception",
            "timeout_seconds": timeout_seconds,
            "base_url": base_url,
            "error": error_text or error_class,
            "error_class": error_class,
        }


def engine_provider_smoke_report(
    cfg: ResolvedConfig,
    *,
    role: str = "worker",
    directory: Path | None = None,
) -> dict[str, Any]:
    route = engine_session_provider_model(cfg, role)
    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    if not _engine_provider_smoke_enabled(cfg):
        return {"ok": True, "skipped": True, "reason": "disabled", "provider": provider, "model": model}
    if not provider or not model:
        return {"ok": False, "reason": "missing_provider_model", "provider": provider, "model": model}
    root = directory or cfg.root
    session_id = ""
    timeout_seconds = _engine_provider_smoke_timeout_seconds(cfg)
    try:
        session_id = create_tandem_session(
            cfg,
            title=f"ACA {role} provider smoke",
            directory=root,
            provider=provider,
            model=model,
        )
        response = prompt_tandem_session_sync(
            cfg,
            session_id=session_id,
            prompt="Reply with exactly ACA_SMOKE_OK and no other text.",
            tool_mode="auto",
            require_tool_use=False,
            write_required=False,
            timeout_seconds=timeout_seconds,
        )
        text = _engine_text_from_messages(response)
        ok = "ACA_SMOKE_OK" in text
        return {
            "ok": ok,
            "reason": "ok" if ok else "empty_or_unexpected_transcript",
            "provider": provider,
            "model": model,
            "source": str(route.get("source") or ""),
            "configured_provider": str(route.get("configured_provider") or ""),
            "configured_model": str(route.get("configured_model") or ""),
            "timeout_seconds": timeout_seconds,
            "text_length": len(text),
        }
    except Exception as exc:
        error_class = exc.__class__.__name__
        error_text = str(exc).strip()
        return {
            "ok": False,
            "reason": "exception",
            "provider": provider,
            "model": model,
            "source": str(route.get("source") or ""),
            "timeout_seconds": timeout_seconds,
            "error": error_text or error_class,
            "error_class": error_class,
        }
    finally:
        if session_id:
            try:
                delete_tandem_session(cfg, session_id)
            except Exception:
                pass


def parse_base_url(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported Tandem base URL: {base_url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _engine_headers(cfg: ResolvedConfig, *, include_content_type: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = cfg.tandem_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Tandem-Token"] = token
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _engine_request_json(
    cfg: ResolvedConfig,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    headers = _engine_headers(cfg, include_content_type=payload is not None)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = Request(f"{cfg.tandem.base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Engine request failed ({exc.code}) for {path}: {detail or exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise TimeoutError(
            f"Engine request timed out after {timeout}s for {path} at {cfg.tandem.base_url}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Engine request failed for {path}: could not connect to {cfg.tandem.base_url} — is the engine running?"
        ) from exc
    return json.loads(raw) if raw.strip() else {}


def _engine_health_at(cfg: ResolvedConfig, path: str, timeout: float) -> dict[str, Any]:
    payload = _engine_request_json(cfg, path, timeout=timeout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected Tandem health payload at {path}: {type(payload).__name__}")
    payload.setdefault("endpoint", path)
    return payload


def engine_health(cfg: ResolvedConfig, timeout: float = 5.0) -> dict[str, Any]:
    attempts: list[str] = []
    last_error: Exception | None = None
    for path in ("/global/health", "/health"):
        try:
            return _engine_health_at(cfg, path, timeout)
        except Exception as exc:
            attempts.append(f"{path}: {exc}")
            last_error = exc
    detail = "; ".join(attempts) if attempts else "no health endpoint available"
    raise RuntimeError(f"Unable to reach Tandem health endpoint: {detail}") from last_error


def _config_env_is_set(cfg: ResolvedConfig, names: tuple[str, ...]) -> bool:
    for name in names:
        if str(cfg.env.get(name) or "").strip():
            return True
    return False


def _operator_provider_override_present(cfg: ResolvedConfig, role: str) -> bool:
    if _config_env_is_set(cfg, ROLE_PROVIDER_ENV_NAMES.get(role, ()) + ("ACA_PROVIDER", "AUTOCODER_PROVIDER")):
        return True
    if _config_env_is_set(cfg, ROLE_MODEL_ENV_NAMES.get(role, ()) + ("ACA_MODEL", "AUTOCODER_MODEL")):
        return True
    resolved = cfg.provider_for_role_with_source(role)
    if resolved["provider_source"] in {"role", "provider", "fallback"} or resolved["model_source"] in {
        "role",
        "provider",
        "fallback",
    }:
        return True
    return False


def _engine_provider_payload(cfg: ResolvedConfig) -> dict[str, Any] | None:
    try:
        payload = _engine_request_json(cfg, "/config/providers", timeout=3.0)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _engine_default_provider_model_from_payload(payload: dict[str, Any] | None) -> tuple[str, str] | None:
    if payload is None:
        return None

    selected = payload.get("selected_model") or payload.get("selectedModel") or {}
    if isinstance(selected, dict):
        selected_provider = str(
            selected.get("provider_id") or selected.get("providerId") or selected.get("provider") or ""
        ).strip()
        selected_model = str(
            selected.get("model_id") or selected.get("modelId") or selected.get("model") or ""
        ).strip()
        if selected_provider and selected_model:
            return selected_provider, selected_model

    provider = str(payload.get("default") or payload.get("default_provider") or payload.get("defaultProvider") or "").strip()
    providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
    provider_entry = providers.get(provider) if provider else None
    if not isinstance(provider_entry, dict):
        provider_entry = {}
    model = str(
        provider_entry.get("default_model")
        or provider_entry.get("defaultModel")
        or payload.get("default_model")
        or payload.get("defaultModel")
        or ""
    ).strip()
    if provider and model:
        return provider, model

    if isinstance(providers, dict):
        for candidate_provider, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            candidate = str(candidate_provider or "").strip()
            candidate_model = str(
                entry.get("default_model")
                or entry.get("defaultModel")
                or entry.get("model")
                or ""
            ).strip()
            if candidate and candidate_model and _provider_entry_has_engine_secret(entry):
                return candidate, candidate_model
    return None


def engine_default_provider_model(cfg: ResolvedConfig) -> tuple[str, str] | None:
    """Read Tandem's current default provider/model without exposing secrets."""
    return _engine_default_provider_model_from_payload(_engine_provider_payload(cfg))


def engine_session_provider_model(cfg: ResolvedConfig, role: str) -> dict[str, str]:
    """Resolve the provider/model ACA should pass when creating an engine session.

    ACA's bundled ``config/agent.yaml`` still carries an OpenAI fallback for
    local demos, but Tandem engine/control-panel defaults are the authority for
    hosted/reuse deployments. If the operator did not explicitly override the
    run model, read Tandem's current default and pass that to the session API.
    """
    configured_provider, configured_model = cfg.provider_for_role(role)
    configured_provider = effective_tandem_provider(configured_provider, cfg)
    if _operator_provider_override_present(cfg, role):
        engine_payload = None
        if not _local_provider_secret_available(cfg, configured_provider):
            engine_payload = _engine_provider_payload(cfg)
        if _provider_route_has_credentials(cfg, configured_provider, engine_payload=engine_payload):
            return {"provider": configured_provider, "model": configured_model, "source": "aca_config"}
        engine_default = _engine_default_provider_model_from_payload(
            engine_payload if isinstance(engine_payload, dict) else _engine_provider_payload(cfg)
        )
        if engine_default:
            provider, model = engine_default
            if _provider_route_has_credentials(
                cfg,
                provider,
                engine_payload=engine_payload if isinstance(engine_payload, dict) else _engine_provider_payload(cfg),
            ):
                return {
                    "provider": provider,
                    "model": model,
                    "source": "engine_default_missing_config_credentials",
                }
        return {"provider": configured_provider, "model": configured_model, "source": "aca_config_missing_credentials"}

    engine_default = engine_default_provider_model(cfg)
    if engine_default:
        provider, model = engine_default
        return {"provider": provider, "model": model, "source": "engine_default"}

    return {"provider": configured_provider, "model": configured_model, "source": "aca_fallback"}


def _provider_entry_has_engine_secret(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for key in (
        "api_key",
        "apiKey",
        "access_token",
        "accessToken",
        "token",
        "refresh_token",
        "refreshToken",
    ):
        if str(entry.get(key) or "").strip():
            return True
    auth_kind = str(entry.get("auth_kind") or entry.get("authKind") or "").strip().lower()
    if auth_kind == "oauth" and (
        str(entry.get("account_id") or entry.get("accountId") or "").strip()
        or str(entry.get("expires_at_ms") or entry.get("expiresAtMs") or "").strip()
    ):
        return True
    return False


def _local_provider_secret_available(cfg: ResolvedConfig, provider: str) -> bool:
    env = getattr(cfg, "env", {}) or {}
    secret_env_name = _provider_secret_env_name(provider)
    if secret_env_name and str(env.get(secret_env_name) or "").strip():
        return True
    if str(env.get("ACA_PROVIDER_KEY") or "").strip():
        return True
    return False


def _provider_route_has_credentials(
    cfg: ResolvedConfig,
    provider: str,
    *,
    engine_payload: dict[str, Any] | None,
) -> bool:
    if _local_provider_secret_available(cfg, provider):
        return True
    providers = engine_payload.get("providers") if isinstance(engine_payload, dict) else {}
    if not isinstance(providers, dict):
        return False
    return _provider_entry_has_engine_secret(providers.get(provider))


def _engine_registry_empty_response_fallback_allowed(cfg: ResolvedConfig) -> bool:
    raw = str(
        getattr(cfg, "env", {}).get("ACA_ALLOW_ENGINE_REGISTRY_EMPTY_RESPONSE_FALLBACK", "") or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def engine_empty_response_fallback_provider_model(
    cfg: ResolvedConfig,
    role: str,
    *,
    current_provider: str,
    current_model: str,
) -> dict[str, str] | None:
    """Return an explicit credentialed provider/model for silent engine retries.

    A silent engine session is a routing/engine-health signal, so retrying the
    same selected engine default is usually wasted work. Only operator-provided
    fallback routes or explicit ACA role/global selections are eligible; engine
    registry entries are not enough because a stored key may still lack credits
    or capacity. Hidden built-in defaults are still ignored.
    """

    engine_payload = _engine_provider_payload(cfg)
    provider_cfg = getattr(cfg, "provider", None)
    fallback_provider = str(getattr(provider_cfg, "fallback_provider", "") or "").strip()
    fallback_model = str(getattr(provider_cfg, "fallback_model", "") or "").strip()
    if fallback_provider and fallback_model:
        provider = effective_tandem_provider(fallback_provider, cfg)
        if (
            (provider != current_provider or fallback_model != current_model)
            and _provider_route_has_credentials(cfg, provider, engine_payload=engine_payload)
        ):
            return {"provider": provider, "model": fallback_model, "source": "aca_fallback_provider"}

    resolver = getattr(cfg, "provider_for_role_with_source", None)
    if callable(resolver):
        resolved = resolver(role)
        if (
            resolved["provider_source"] in {"role", "provider"}
            or resolved["model_source"] in {"role", "provider"}
        ):
            provider = effective_tandem_provider(str(resolved.get("provider") or ""), cfg)
            model = str(resolved.get("model") or "").strip()
            if (
                provider
                and model
                and (provider != current_provider or model != current_model)
                and _provider_route_has_credentials(cfg, provider, engine_payload=engine_payload)
            ):
                return {"provider": provider, "model": model, "source": "aca_config_alternate"}

    if not _engine_registry_empty_response_fallback_allowed(cfg):
        return None

    providers = engine_payload.get("providers") if isinstance(engine_payload, dict) else {}
    if isinstance(providers, dict):
        for candidate_provider, entry in providers.items():
            provider = str(candidate_provider or "").strip()
            if not provider or provider == current_provider or "::" in provider:
                continue
            if not isinstance(entry, dict):
                continue
            model = str(
                entry.get("default_model")
                or entry.get("defaultModel")
                or entry.get("model")
                or ""
            ).strip()
            if not model or (provider == current_provider and model == current_model):
                continue
            if _provider_route_has_credentials(cfg, provider, engine_payload=engine_payload):
                return {"provider": provider, "model": model, "source": "engine_registry_alternate"}

    return None


def cli_version() -> str | None:
    from src.tandem_agents.core.engine.process_utils import run_command

    if not command_exists("tandem-engine"):
        return None
    result = run_command(["tandem-engine", "--version"])
    text = f"{result.stdout}\n{result.stderr}".strip()
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", text)
    return match.group(1) if match else None


def _version_satisfies(version_text: str | None, spec_text: str | None) -> tuple[bool | None, str]:
    spec_text = (spec_text or "").strip()
    if not spec_text:
        return True, "no required version set"
    if not version_text:
        return None, f"version unavailable; required {spec_text}"
    try:
        spec = SpecifierSet(spec_text)
        version = Version(version_text)
        return version in spec, f"engine {version_text} against requirement {spec_text}"
    except (InvalidVersion, Exception) as exc:
        return None, f"could not compare versions ({exc})"


def engine_status_report(cfg: ResolvedConfig, *, health_timeout: float = 5.0) -> dict[str, Any]:
    report: dict[str, Any] = {
        "base_url": cfg.tandem.base_url,
        "required_version": cfg.tandem.required_version or None,
        "startup_mode": cfg.tandem.startup_mode,
        "update_policy": cfg.tandem.update_policy,
        "local_cli_version": cli_version(),
        "status": "missing",
        "healthy": False,
        "running": False,
        "version": None,
        "build_id": None,
        "detail": None,
        "update_available": False,
        "checked_at_ms": now_ms(),
    }
    try:
        health = engine_health(cfg, timeout=health_timeout)
    except Exception as exc:
        report["detail"] = str(exc)
        return report

    report["healthy"] = bool(health.get("healthy"))
    report["running"] = bool(health.get("ready"))
    report["version"] = health.get("version") or health.get("build_id")
    report["build_id"] = health.get("build_id")
    report["health_endpoint"] = health.get("endpoint")
    report["phase"] = health.get("phase")
    report["workspace_root"] = health.get("workspace_root")
    report["api_token_required"] = health.get("apiTokenRequired")

    satisfies, detail = _version_satisfies(report["version"], cfg.tandem.required_version)
    report["detail"] = detail
    if satisfies is False:
        report["update_available"] = True
        report["status"] = "update_available" if cfg.tandem.update_policy != "block" else "blocked"
    elif report["healthy"]:
        report["status"] = "running"
    else:
        report["status"] = "detected"

    if report["local_cli_version"] and report["version"] and report["local_cli_version"] != report["version"]:
        report["version_notice"] = (
            f"local CLI {report['local_cli_version']} differs from running engine {report['version']}"
        )
    return report


def execute_engine_tool(cfg: ResolvedConfig, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    if sdk_available():
        payload = sdk_execute_tool(cfg, tool, args or {})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected tool response for {tool}: {type(payload).__name__}")
        return payload
    payload = _engine_request_json(
        cfg,
        "/tool/execute",
        method="POST",
        payload={"tool": tool, "args": args or {}},
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected tool response for {tool}: {type(payload).__name__}")
    return payload


def list_engine_tool_ids(cfg: ResolvedConfig) -> list[str]:
    if sdk_available():
        payload = sdk_list_tool_ids(cfg)
    else:
        payload = _engine_request_json(cfg, "/tool/ids")
    return [item for item in payload if isinstance(item, str)] if isinstance(payload, list) else []


def list_mcp_servers(cfg: ResolvedConfig) -> dict[str, Any]:
    if sdk_available():
        payload = sdk_list_mcp_servers(cfg)
    else:
        payload = _engine_request_json(cfg, "/mcp")
    return payload if isinstance(payload, dict) else {}


def set_mcp_enabled(cfg: ResolvedConfig, name: str, enabled: bool) -> dict[str, Any]:
    if sdk_available():
        payload = sdk_set_mcp_enabled(cfg, name, enabled)
    else:
        payload = _engine_request_json(
            cfg,
            f"/mcp/{name}",
            method="PATCH",
            payload={"enabled": enabled},
        )
    return payload if isinstance(payload, dict) else {}


def connect_mcp_server(cfg: ResolvedConfig, name: str) -> dict[str, Any]:
    if sdk_available():
        payload = sdk_connect_mcp(cfg, name)
    else:
        payload = _engine_request_json(cfg, f"/mcp/{name}/connect", method="POST", payload={})
    return payload if isinstance(payload, dict) else {}


def disconnect_mcp_server(cfg: ResolvedConfig, name: str) -> dict[str, Any]:
    if sdk_available():
        payload = sdk_disconnect_mcp(cfg, name)
    else:
        payload = _engine_request_json(cfg, f"/mcp/{name}/disconnect", method="POST", payload={})
    return payload if isinstance(payload, dict) else {}


def refresh_mcp_server(cfg: ResolvedConfig, name: str) -> dict[str, Any]:
    if sdk_available():
        payload = sdk_refresh_mcp(cfg, name)
    else:
        payload = _engine_request_json(cfg, f"/mcp/{name}/refresh", method="POST", payload={})
    return payload if isinstance(payload, dict) else {}


def list_engine_permissions(cfg: ResolvedConfig) -> dict[str, Any]:
    if sdk_available():
        try:
            payload = sdk_list_permissions(cfg)
        except Exception:
            payload = _engine_request_json(cfg, "/permission", method="GET", timeout=30.0)
    else:
        payload = _engine_request_json(cfg, "/permission", method="GET", timeout=30.0)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected permission payload: {type(payload).__name__}")
    return payload


def reply_engine_permission(cfg: ResolvedConfig, request_id: str, reply: str) -> dict[str, Any]:
    if sdk_available():
        try:
            payload = sdk_reply_permission(cfg, request_id, reply)
        except Exception:
            payload = _engine_request_json(
                cfg,
                f"/permission/{request_id}/reply",
                method="POST",
                payload={"reply": reply},
                timeout=30.0,
            )
    else:
        payload = _engine_request_json(
            cfg,
            f"/permission/{request_id}/reply",
            method="POST",
            payload={"reply": reply},
            timeout=30.0,
        )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected permission reply payload: {type(payload).__name__}")
    return payload


def engine_visible_path(path: Path) -> Path:
    """Map ACA container paths to the filesystem path visible to Tandem engine.

    In hosted deployments ACA can run in Docker while the reused Tandem engine
    runs on the host. ACA sees `/workspace/tandem-agents`, but the host engine
    needs the bind-mount source path. Keep the mapping explicit so local
    non-container runs are unchanged.
    """
    host_root = (os.environ.get("ACA_ENGINE_HOST_ROOT") or "").strip()
    if not host_root:
        return path
    container_root = (
        os.environ.get("ACA_ENGINE_CONTAINER_ROOT")
        or os.environ.get("ACA_ROOT")
        or "/workspace/tandem-agents"
    ).strip()
    if not container_root:
        return path
    resolved = path.expanduser()
    text = str(resolved)
    prefix = container_root.rstrip("/") + "/"
    if text == container_root.rstrip("/"):
        return Path(host_root).expanduser()
    if text.startswith(prefix):
        return Path(host_root).expanduser() / text[len(prefix):]
    return path


def create_tandem_session(
    cfg: ResolvedConfig,
    *,
    title: str,
    directory: Path,
    provider: str,
    model: str,
    temperature: float | None = None,
    permission_rules: list[dict[str, str]] | None = None,
) -> str:
    visible_directory = str(engine_visible_path(directory))
    if permission_rules:
        payload = {
            "title": title,
            "directory": visible_directory,
            "workspace_root": visible_directory,
            "provider": provider,
            "model": {
                "providerID": provider,
                "modelID": model,
            },
            "permission": permission_rules,
        }
        response = _engine_request_json(cfg, "/session", method="POST", payload=payload)
        session_id = str((response or {}).get("id") or "").strip()
    elif sdk_available():
        session_id = str(
            sdk_create_session(
                cfg,
                title=title,
                directory=visible_directory,
                provider=provider,
                model=model,
                temperature=temperature,
            )
            or ""
        ).strip()
    else:
        payload = {
            "title": title,
            "directory": visible_directory,
            "provider": provider,
            "model": {
                "providerID": provider,
                "modelID": model,
            },
        }
        response = _engine_request_json(cfg, "/session", method="POST", payload=payload)
        session_id = str((response or {}).get("id") or "").strip()
    if not session_id:
        raise RuntimeError("Engine did not return a session id.")
    return session_id


def prompt_tandem_session_sync(
    cfg: ResolvedConfig,
    *,
    session_id: str,
    prompt: str,
    tool_allowlist: list[str] | None = None,
    tool_mode: str | None = None,
    require_tool_use: bool = False,
    write_required: bool = False,
    prewrite_requirements: dict[str, Any] | None = None,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "parts": [{"type": "text", "text": prompt}],
        "toolMode": tool_mode or ("required" if require_tool_use else "auto"),
    }
    if tool_allowlist is not None:
        payload["toolAllowlist"] = tool_allowlist
    if write_required:
        payload["writeRequired"] = True
    if prewrite_requirements:
        payload["prewriteRequirements"] = prewrite_requirements
    response = _engine_request_json(
        cfg,
        f"/session/{session_id}/prompt_sync",
        method="POST",
        payload=payload,
        timeout=timeout_seconds,
    )
    return {"messages": response}


def delete_tandem_session(cfg: ResolvedConfig, session_id: str) -> None:
    if sdk_available():
        sdk_delete_session(cfg, session_id)
        return
    _engine_request_json(cfg, f"/session/{session_id}", method="DELETE", timeout=30.0)


def _provider_secret_env_name(provider: str) -> str | None:
    return PROVIDER_SECRET_ENV_BY_ID.get((provider or "").strip().lower())


def _active_providers(cfg: ResolvedConfig) -> list[str]:
    providers = [
        cfg.provider.id,
        cfg.swarm.manager.provider,
        cfg.swarm.worker.provider,
        cfg.swarm.reviewer.provider,
        cfg.swarm.tester.provider,
        cfg.provider.fallback_provider,
    ]
    seen: list[str] = []
    for provider in providers:
        normalized = (provider or "").strip().lower()
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def engine_env(cfg: ResolvedConfig) -> dict[str, str]:
    host, port = cfg.engine_host_port()
    env = dict(cfg.env)
    token_file = cfg.tandem_token_file_path()
    if token_file:
        env["TANDEM_API_TOKEN_FILE"] = str(token_file)
        if token_file.is_file() and os.access(token_file, os.R_OK):
            env.pop(cfg.tandem.token_env, None)
            env.pop("TANDEM_API_TOKEN", None)
            env.pop("TANDEM_TOKEN", None)
    else:
        token = cfg.tandem_token()
        if token:
            env["TANDEM_API_TOKEN"] = token
    env["TANDEM_ENGINE_HOST"] = host
    env["TANDEM_ENGINE_PORT"] = str(port)
    env["TANDEM_ENGINE_URL"] = cfg.tandem.base_url
    generic_provider_key = (env.get("ACA_PROVIDER_KEY") or "").strip()
    if generic_provider_key:
        for provider in _active_providers(cfg):
            secret_env_name = _provider_secret_env_name(provider)
            if secret_env_name and not (env.get(secret_env_name) or "").strip():
                env[secret_env_name] = generic_provider_key
    return env


def write_provider_override_config(
    *,
    cfg: ResolvedConfig,
    provider: str,
    model: str,
    output_path: Path,
) -> Path | None:
    base_url = (cfg.provider.base_url or "").strip()
    if not base_url:
        return None
    effective_provider = provider
    if provider == "custom":
        effective_provider = "openai"
    payload = {
        "default_provider": effective_provider,
        "providers": {
            effective_provider: {
                "url": base_url,
                "default_model": model,
            }
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path


def effective_tandem_provider(provider: str, cfg: ResolvedConfig) -> str:
    base_url = (cfg.provider.base_url or "").strip()
    if provider == "custom" and base_url:
        return "openai"
    return provider


def start_engine(cfg: ResolvedConfig, log_path: Path, timeout_seconds: int = 90) -> dict[str, Any]:
    if not command_exists("tandem-engine"):
        raise RuntimeError("tandem-engine is not installed")
    env = engine_env(cfg)
    command = cfg.tandem.engine_command.strip() or "scripts/tandem-engine-serve.sh"
    args = shlex.split(command)
    if args:
        command_path = Path(args[0]).expanduser()
        if not command_path.is_absolute():
            candidate = (cfg.root_dir / command_path).resolve()
            if candidate.exists():
                args[0] = str(candidate)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        env=env,
        cwd=str(cfg.root_dir),
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
    )
    deadline = time.time() + timeout_seconds
    last_error: str | None = None
    while time.time() < deadline:
        try:
            health = engine_health(cfg, timeout=3.0)
            if health.get("ready") or health.get("healthy"):
                return {
                    "action": "started",
                    "pid": proc.pid,
                    "health": health,
                    "version": health.get("version") or health.get("build_id"),
                }
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for Tandem engine to start: {last_error or 'unknown error'}")


def ensure_engine(cfg: ResolvedConfig, log_dir: Path) -> dict[str, Any]:
    status = engine_status_report(cfg)
    if status["healthy"] and status["status"] != "blocked":
        status["action"] = "reused"
        return status
    if status["status"] == "blocked" and cfg.tandem.update_policy == "block":
        status["action"] = "blocked"
        return status
    if cfg.tandem.startup_mode == "reuse_only":
        status["action"] = "blocked"
        status["detail"] = status.get("detail") or "engine missing and startup_mode=reuse_only"
        return status
    if not command_exists("tandem-engine"):
        status["action"] = "blocked"
        status["detail"] = status.get("detail") or "tandem-engine command is unavailable"
        return status
    engine_log = log_dir / "engine.log"
    started = start_engine(cfg, engine_log)
    started["action"] = "started"
    started["status"] = "running"
    started["checked_at_ms"] = now_ms()
    return started
