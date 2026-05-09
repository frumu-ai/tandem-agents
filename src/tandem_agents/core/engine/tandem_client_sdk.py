from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
import asyncio
import time

from src.tandem_agents.config.config import ResolvedConfig


def _import_sync_client() -> Any:
    try:
        from tandem_client import SyncTandemClient

        return SyncTandemClient
    except Exception:
        fallback = os.environ.get("ACA_TANDEM_CLIENT_PY_PATH", "").strip()
        if fallback:
            root = Path(fallback).expanduser().resolve()
        else:
            root = (Path(__file__).resolve().parents[4] / "vendor" / "tandem-client-py").resolve()
        if root.exists():
            root_text = str(root)
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
            from tandem_client import SyncTandemClient

            return SyncTandemClient
        raise


def _import_async_client() -> Any:
    try:
        from tandem_client import TandemClient

        return TandemClient
    except Exception:
        fallback = os.environ.get("ACA_TANDEM_CLIENT_PY_PATH", "").strip()
        if fallback:
            root = Path(fallback).expanduser().resolve()
        else:
            root = (Path(__file__).resolve().parents[4] / "vendor" / "tandem-client-py").resolve()
        if root.exists():
            root_text = str(root)
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
            from tandem_client import TandemClient

            return TandemClient
        raise


def create_sync_tandem_client(cfg: ResolvedConfig) -> Any:
    SyncTandemClient = _import_sync_client()
    token = cfg.tandem_token()
    if not token:
        raise RuntimeError("Tandem API token is required to create the SDK client.")
    return SyncTandemClient(base_url=cfg.tandem.base_url, token=token)


def execute_with_client(cfg: ResolvedConfig, fn: str, *args: Any, **kwargs: Any) -> Any:
    client = create_sync_tandem_client(cfg)
    try:
        target: Any = client
        for part in fn.split("."):
            target = getattr(target, part)
        result = target(*args, **kwargs)
        if hasattr(result, "model_dump"):
            return result.model_dump(exclude_none=True)
        return result
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except RuntimeError as exc:
                if "Event loop is closed" not in str(exc):
                    raise


def _close_quietly(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise


def _jsonify_sdk_result(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        return result.model_dump(exclude_none=True)
    return result


def with_sync_tandem_client(cfg: ResolvedConfig, fn: Any) -> Any:
    client = create_sync_tandem_client(cfg)
    try:
        return _jsonify_sdk_result(fn(client))
    finally:
        _close_quietly(client)


def sdk_execute_tool(cfg: ResolvedConfig, tool: str, args: dict[str, Any] | None = None) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.execute_tool(tool, args or {}))


def sdk_list_tool_ids(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.list_tool_ids())


def sdk_list_permissions(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.permissions.list())


def sdk_reply_permission(cfg: ResolvedConfig, request_id: str, reply: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.permissions.reply(request_id, reply))


def sdk_create_session(
    cfg: ResolvedConfig,
    *,
    title: str,
    directory: str,
    provider: str,
    model: str,
) -> Any:
    return with_sync_tandem_client(
        cfg,
        lambda client: client.sessions.create(
            title=title,
            directory=directory,
            provider=provider,
            model=model,
        ),
    )


def sdk_delete_session(cfg: ResolvedConfig, session_id: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.sessions.delete(session_id))


def sdk_list_mcp_servers(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.mcp.list())


def sdk_set_mcp_enabled(cfg: ResolvedConfig, name: str, enabled: bool) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.mcp.set_enabled(name, enabled))


def sdk_connect_mcp(cfg: ResolvedConfig, name: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.mcp.connect(name))


def sdk_disconnect_mcp(cfg: ResolvedConfig, name: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.mcp.disconnect(name))


def sdk_refresh_mcp(cfg: ResolvedConfig, name: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.mcp.refresh(name))


def sdk_coder_create_run(cfg: ResolvedConfig, payload: dict[str, Any]) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.coder.create_run(payload))


def sdk_coder_execute_all(
    cfg: ResolvedConfig,
    coder_run_id: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    return with_sync_tandem_client(
        cfg,
        lambda client: client.coder.execute_all(coder_run_id, payload or {}),
    )


def sdk_coder_get_run(cfg: ResolvedConfig, coder_run_id: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.coder.get_run(coder_run_id))


def sdk_coder_cancel_run(cfg: ResolvedConfig, coder_run_id: str, reason: str = "") -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.coder.cancel_run(coder_run_id, reason))


def sdk_coder_list_artifacts(cfg: ResolvedConfig, coder_run_id: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.coder.list_artifacts(coder_run_id))


def sdk_task_intake_preview(cfg: ResolvedConfig, task: dict[str, Any]) -> Any:
    def _call(client: Any) -> Any:
        response = asyncio.run(client._async._http.post("/task-intake/preview", json=task))
        response.raise_for_status()
        return response.json()

    return with_sync_tandem_client(cfg, _call)


def sdk_available() -> bool:
    try:
        _import_sync_client()
        return True
    except Exception:
        return False


def sdk_agent_teams_list_templates(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.list_templates())


def sdk_agent_teams_create_template(cfg: ResolvedConfig, template: dict[str, Any]) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.create_template(template))


def sdk_agent_teams_update_template(cfg: ResolvedConfig, template_id: str, patch: dict[str, Any]) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.update_template(template_id, patch))


def sdk_agent_teams_delete_template(cfg: ResolvedConfig, template_id: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.delete_template(template_id))


def sdk_agent_teams_list_instances(
    cfg: ResolvedConfig,
    mission_id: str | None = None,
    parent_instance_id: str | None = None,
    status: str | None = None,
) -> Any:
    return with_sync_tandem_client(
        cfg,
        lambda client: client.agent_teams.list_instances(
            mission_id=mission_id,
            parent_instance_id=parent_instance_id,
            status=status,
        ),
    )


def sdk_agent_teams_list_missions(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.list_missions())


def sdk_agent_teams_list_approvals(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.list_approvals())


def sdk_agent_teams_spawn(
    cfg: ResolvedConfig,
    role: str,
    justification: str,
    *,
    mission_id: str | None = None,
    parent_instance_id: str | None = None,
    template_id: str | None = None,
    budget_override: dict[str, Any] | None = None,
) -> Any:
    return with_sync_tandem_client(
        cfg,
        lambda client: client.agent_teams.spawn(
            role,
            justification,
            mission_id=mission_id,
            parent_instance_id=parent_instance_id,
            template_id=template_id,
            budget_override=budget_override,
        ),
    )


def sdk_agent_teams_approve_spawn(cfg: ResolvedConfig, approval_id: str, reason: str = "") -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.approve_spawn(approval_id, reason))


def sdk_agent_teams_deny_spawn(cfg: ResolvedConfig, approval_id: str, reason: str = "") -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.agent_teams.deny_spawn(approval_id, reason))


def sdk_missions_list(cfg: ResolvedConfig) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.missions.list())


def sdk_mission_create(
    cfg: ResolvedConfig,
    title: str,
    goal: str,
    work_items: list[dict[str, Any]] | None = None,
) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.missions.create(title=title, goal=goal, work_items=work_items or []))


def sdk_mission_get(cfg: ResolvedConfig, mission_id: str) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.missions.get(mission_id))


def sdk_mission_apply_event(cfg: ResolvedConfig, mission_id: str, event: dict[str, Any]) -> Any:
    return with_sync_tandem_client(cfg, lambda client: client.missions.apply_event(mission_id, event))


def sdk_sessions_prompt_async(
    cfg: ResolvedConfig,
    session_id: str,
    prompt: str,
    *,
    tool_mode: str | None = None,
    tool_allowlist: list[str] | None = None,
    context_mode: str | None = None,
) -> Any:
    def _call(client: Any) -> Any:
        res = client.sessions.prompt_async(
            session_id,
            prompt,
            tool_mode=tool_mode,
            tool_allowlist=tool_allowlist,
            context_mode=context_mode,
        )
        return res.model_dump(exclude_none=True) if hasattr(res, "model_dump") else res
    return with_sync_tandem_client(cfg, _call)


def sdk_run_events(
    cfg: ResolvedConfig,
    run_id: str,
    *,
    since_seq: int | None = None,
    tail: int | None = None,
) -> Any:
    return with_sync_tandem_client(
        cfg,
        lambda client: [
            (e.model_dump(exclude_none=True) if hasattr(e, "model_dump") else e)
            for e in client.run_events(run_id, since_seq=since_seq, tail=tail)
        ],
    )


def _browser_request(cfg: ResolvedConfig, method: str, path: str, **kwargs) -> Any:
    token = cfg.tandem_token()
    if not token:
        raise RuntimeError("Tandem API token is required to use browser tools.")
    base = cfg.tandem.base_url if cfg.tandem else "http://tandem-engine:39733"
    import httpx
    with httpx.Client(timeout=60.0) as client:
        resp = client.request(method, f"{base}/browser{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def _execute_tool(cfg: ResolvedConfig, tool_name: str, args: dict[str, Any]) -> Any:
    token = cfg.tandem_token()
    if not token:
        raise RuntimeError("Tandem API token is required.")
    base = cfg.tandem.base_url if cfg.tandem else "http://tandem-engine:39733"
    import httpx
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{base}/tool/execute",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"tool": tool_name, "args": args},
        )
        resp.raise_for_status()
        result = resp.json()
        output = result.get("output", result)
        if isinstance(output, dict) and "error" in output:
            raise RuntimeError(f"Tool '{tool_name}' error: {output['error']}")
        if isinstance(output, str):
            import json as _json
            try:
                return _json.loads(output)
            except Exception:
                return output
        return output


def sdk_browser_open(cfg: ResolvedConfig, url: str) -> Any:
    return _execute_tool(cfg, "browser_open", {"url": url})


def sdk_browser_navigate(cfg: ResolvedConfig, url: str) -> Any:
    return _execute_tool(cfg, "browser_navigate", {"url": url})


def sdk_browser_click(cfg: ResolvedConfig, session_id: str, element_id: str) -> Any:
    return _execute_tool(cfg, "browser_click", {"session_id": session_id, "element_id": element_id})


def sdk_browser_type(cfg: ResolvedConfig, session_id: str, element_id: str, text: str) -> Any:
    return _execute_tool(cfg, "browser_type", {"session_id": session_id, "element_id": element_id, "text": text})


def sdk_browser_press(cfg: ResolvedConfig, session_id: str, element_id: str, key: str) -> Any:
    return _execute_tool(cfg, "browser_press", {"session_id": session_id, "element_id": element_id, "key": key})


def sdk_browser_wait(
    cfg: ResolvedConfig,
    session_id: str,
    wait_type: str,
    target: str,
    timeout: float = 30.0,
) -> Any:
    return _execute_tool(cfg, "browser_wait", {
        "session_id": session_id,
        "type": wait_type,
        "target": target,
        "timeout": timeout,
    })


def sdk_browser_snapshot(cfg: ResolvedConfig, session_id: str, include_screenshot: bool = False) -> Any:
    return _execute_tool(cfg, "browser_snapshot", {"session_id": session_id, "include_screenshot": include_screenshot})


def sdk_browser_extract(cfg: ResolvedConfig, session_id: str, format: str = "html") -> Any:
    return _execute_tool(cfg, "browser_extract", {"session_id": session_id, "format": format})


def sdk_browser_screenshot(cfg: ResolvedConfig, session_id: str, full_page: bool = False) -> Any:
    return _execute_tool(cfg, "browser_screenshot", {"session_id": session_id, "full_page": full_page})


def sdk_browser_close(cfg: ResolvedConfig, session_id: str) -> Any:
    return _execute_tool(cfg, "browser_close", {"session_id": session_id})


def sdk_browser_status(cfg: ResolvedConfig) -> Any:
    return _execute_tool(cfg, "browser_status", {})


def sdk_stream_run_text(
    cfg: ResolvedConfig,
    session_id: str,
    run_id: str,
    log_writer: Any | None = None,
    timeout_seconds: float = 600.0,
) -> Any:
    TandemClient = _import_async_client()
    token = cfg.tandem_token()
    if not token:
        raise RuntimeError("Tandem API token is required to stream events.")

    async def _runner() -> dict[str, Any]:
        async with TandemClient(base_url=cfg.tandem.base_url, token=token) as client:
            parts: list[str] = []
            completed = False
            deadline = time.time() + timeout_seconds
            async for evt in client.stream(session_id, run_id):
                t = str(getattr(evt, "type", "") or "").strip()
                if t == "session.response":
                    delta = str((getattr(evt, "properties", {}) or {}).get("delta") or "")
                    if delta:
                        parts.append(delta)
                        if log_writer is not None:
                            try:
                                log_writer(delta)
                            except Exception:
                                pass
                if t in {"run.complete", "run.completed", "run.failed", "session.run.finished"}:
                    completed = True
                    break
                if time.time() >= deadline:
                    break
            text = "".join(parts)
            return {"text": text, "completed": completed}

    return asyncio.run(_runner())
