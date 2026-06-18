from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.tandem_agents.api.auth import get_token
from src.tandem_agents.mcp.snapshot import build_aca_overview

MCP_PROTOCOL_VERSION = "2025-06-18"

router = APIRouter()


class MCPRemoteOut(BaseModel):
    type: str
    url: str


class MCPManifestOut(BaseModel):
    name: str
    title: str
    description: str
    version: str
    homepage: str
    websiteUrl: str | None = None
    repository: str | None = None
    docs: str | None = None
    remotes: list[MCPRemoteOut]
    capabilities: dict[str, Any]


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def _jsonrpc_result(request_id: str | int | None, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status_code=status_code)


def _public_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/mcp"


def _manifest_payload(request: Request) -> dict[str, Any]:
    public_base_url = _public_base_url(request)
    return {
        "name": "ac.tandem/aca-mcp",
        "title": "ACA Overview MCP",
        "description": "Read-only MCP server for understanding ACA runtime state and safe next actions.",
        "version": "0.1.0",
        "homepage": public_base_url,
        "websiteUrl": public_base_url,
        "repository": None,
        "docs": public_base_url,
        "remotes": [{"type": "streamable-http", "url": public_base_url}],
        "capabilities": {
            "tools": [
                "describe_aca",
            ],
        },
    }


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "describe_aca",
            "description": "Return a compact read-only snapshot of ACA runtime state, task-source shape, repository binding, GitHub MCP state, current run state, and safe next actions.",
            "annotations": {
                "title": "Describe ACA",
                "readOnlyHint": True,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


@router.get("/server.json", response_model=MCPManifestOut)
async def server_manifest(request: Request, _: str = Depends(get_token)) -> dict[str, Any]:
    return _manifest_payload(request)


@router.get("/.well-known/mcp/server.json", response_model=MCPManifestOut, include_in_schema=False)
async def well_known_manifest(request: Request, _: str = Depends(get_token)) -> dict[str, Any]:
    return _manifest_payload(request)


@router.post("/mcp")
async def mcp_rpc(request: Request, payload: JsonRpcRequest, _: str = Depends(get_token)):
    if payload.jsonrpc != "2.0":
        return _jsonrpc_error(payload.id, -32600, "Only JSON-RPC 2.0 is supported.", status_code=400)

    method = payload.method
    params = payload.params or {}

    if method == "initialize":
        return _jsonrpc_result(
            payload.id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {
                    "name": "ac.tandem/aca-mcp",
                    "title": "ACA Overview MCP",
                    "version": "0.1.0",
                },
                "instructions": (
                    "Call describe_aca first to learn the current ACA runtime state, task source, repository binding, GitHub MCP state, and allowed next actions. "
                    "Treat the returned overview as the runtime source of truth for safe follow-up reads. "
                    "Use the overview to decide whether you should inspect the repo, intake the next task, or review the docs before asking for any write operation."
                ),
            },
        )

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "resources/list":
        return _jsonrpc_result(payload.id, {"resources": []})

    if method == "prompts/list":
        return _jsonrpc_result(payload.id, {"prompts": []})

    if method == "tools/list":
        return _jsonrpc_result(payload.id, {"tools": _tool_specs()})

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        tool_args = params.get("arguments") or {}
        if tool_name == "describe_aca":
            root = Path(os.environ.get("ACA_ROOT", "."))
            overview = await asyncio.to_thread(build_aca_overview, root)
            return _jsonrpc_result(payload.id, {"overview": overview})
        return _jsonrpc_error(payload.id, -32601, f"Unknown tool '{tool_name}'.", status_code=404)

    return _jsonrpc_error(payload.id, -32601, f"Unsupported method '{method}'.", status_code=404)


def create_app() -> FastAPI:
    app = FastAPI(title="ACA Overview MCP")
    app.include_router(router)
    return app
