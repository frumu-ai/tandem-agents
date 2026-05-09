from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .indexer import KnowledgebaseIndex
from .models import (
    CollectionListOut,
    CollectionSummaryOut,
    DocumentDetailOut,
    DocumentListOut,
    DocumentSummaryOut,
    JsonRpcRequest,
    MCPManifestOut,
    MCPRemoteOut,
    PromptDeleteOut,
    PromptListOut,
    PromptUpdateIn,
    PromptUpdateOut,
    SearchResponseOut,
    SearchResultOut,
    WriteResponseOut,
)
from .settings import KBSettings, get_settings

from .storage import normalize_collection_id, normalize_doc_path, normalize_text_list, split_document_frontmatter


logger = logging.getLogger("aca.kb")
security = HTTPBearer(auto_error=False)


def _jsonrpc_result(request_id: str | int | None, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(request_id: str | int | None, code: int, message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status_code=status_code)


def _manifest_payload(kb_settings) -> dict[str, Any]:
    return {
        "name": kb_settings.server_name,
        "title": kb_settings.server_title,
        "description": kb_settings.server_description,
        "version": kb_settings.server_version,
        "homepage": kb_settings.public_base_url,
        "websiteUrl": kb_settings.public_base_url,
        "repository": None,
        "docs": kb_settings.public_base_url,
        "remotes": [{"type": "streamable-http", "url": kb_settings.public_base_url}],
        "capabilities": {
            "tools": [
                "get_kb_guide",
                "get_collection_guide",
                "list_collections",
                "list_documents",
                "answer_question",
                "search_docs",
                "get_document",
                "create_document",
                "append_section",
                "update_document_metadata",
                "update_collection_metadata",
                "propose_document_change",
                "list_proposed_changes",
                "apply_proposed_change",
                "discard_proposed_change",
                "lint_collection",
                "reindex_collection",
            ],
        },
    }


def _tool_specs(kb_settings, index: KnowledgebaseIndex) -> list[dict[str, Any]]:
    max_list_limit = kb_settings.max_list_limit
    max_search_limit = kb_settings.max_search_limit
    default_list_limit = min(25, max_list_limit)
    return [
        {
            "name": "get_kb_guide",
            "description": "Get the global KB guide, compiled-wiki model, and collection summaries.",
            "annotations": {"title": "KB guide", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_collection_guide",
            "description": "Get the generated guide for a single collection, including collection role, canonical docs, and search hints.",
            "annotations": {"title": "Collection guide", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {"collection_id": {"type": "string"}},
                "required": ["collection_id"],
            },
        },
        {
            "name": "list_collections",
            "description": "List available collections and their compiled-wiki metadata.",
            "annotations": {"title": "List collections", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_documents",
            "description": "List documents in a collection, with optional paging and text filtering.",
            "annotations": {"title": "List documents", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": max_list_limit,
                        "default": default_list_limit,
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "query": {"type": "string"},
                },
                "required": ["collection_id"],
            },
        },
        {
            "name": "answer_question",
            "description": index.get_prompt("answer_question_tool_description"),
            "annotations": {"title": "Answer question", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "collection_id": {"type": "string"},
                    "max_documents": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": kb_settings.answer_max_documents,
                    },
                },
                "required": ["question"],
            },
        },
        {
            "name": "search_docs",
            "description": "Search documents in one collection or across all collections when collection_id is omitted. Returns ranked candidate hits with clean snippets for navigation; for question answering, prefer answer_question.",
            "annotations": {"title": "Search docs", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": max_search_limit},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_document",
            "description": "Fetch a document by exact doc_id. collection_id is optional for backwards-compatible slug lookup.",
            "annotations": {"title": "Get document", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "collection_id": {"type": "string"},
                },
                "required": ["doc_id"],
            },
        },
        {
            "name": "create_document",
            "description": "Create a new document in a writable wiki or output collection.",
            "annotations": {"title": "Create document", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "doc_path": {"type": "string"},
                    "raw_text": {"type": "string"},
                    "filename": {"type": "string"},
                    "content_type": {"type": "string"},
                    "title": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "frontmatter_patch": {"type": "object"},
                    "kb_source_docs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["collection_id", "doc_path", "raw_text"],
            },
        },
        {
            "name": "append_section",
            "description": "Append a section to an existing document, or create it if it does not exist.",
            "annotations": {"title": "Append section", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "doc_path": {"type": "string"},
                    "heading": {"type": "string"},
                    "content": {"type": "string"},
                    "frontmatter_patch": {"type": "object"},
                    "kb_source_docs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["collection_id", "doc_path"],
            },
        },
        {
            "name": "update_document_metadata",
            "description": "Update document metadata and preserve the existing body.",
            "annotations": {"title": "Update metadata", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "doc_path": {"type": "string"},
                    "title": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "frontmatter_patch": {"type": "object"},
                    "kb_source_docs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["collection_id", "doc_path"],
            },
        },
        {
            "name": "update_collection_metadata",
            "description": "Update a collection manifest so the KB can mark it as raw, wiki, or output.",
            "annotations": {"title": "Update collection metadata", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["raw", "wiki", "output"]},
                    "mutable": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "source_collections": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["collection_id"],
            },
        },
        {
            "name": "propose_document_change",
            "description": "Create a staged change proposal for risky edits such as replace or delete.",
            "annotations": {"title": "Propose change", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "operation": {"type": "string", "enum": ["create", "update", "replace", "append", "metadata", "delete"]},
                    "doc_path": {"type": "string"},
                    "raw_text": {"type": "string"},
                    "title": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "frontmatter_patch": {"type": "object"},
                    "kb_source_docs": {"type": "array", "items": {"type": "string"}},
                    "heading": {"type": "string"},
                    "content": {"type": "string"},
                    "filename": {"type": "string"},
                    "content_type": {"type": "string"},
                },
                "required": ["collection_id", "operation"],
            },
        },
        {
            "name": "list_proposed_changes",
            "description": "List proposed KB changes for a collection.",
            "annotations": {"title": "List proposed changes", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": max_list_limit},
                },
            },
        },
        {
            "name": "apply_proposed_change",
            "description": "Apply a staged KB change.",
            "annotations": {"title": "Apply proposed change", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "change_id": {"type": "string"},
                },
                "required": ["change_id"],
            },
        },
        {
            "name": "discard_proposed_change",
            "description": "Discard a staged KB change.",
            "annotations": {"title": "Discard proposed change", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "change_id": {"type": "string"},
                },
                "required": ["change_id"],
            },
        },
        {
            "name": "lint_collection",
            "description": "Lint a collection for missing sources, orphan docs, and backlink gaps.",
            "annotations": {"title": "Lint collection", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                },
                "required": ["collection_id"],
            },
        },
        {
            "name": "reindex_collection",
            "description": "Reindex documents from disk for one collection or all collections.",
            "annotations": {"title": "Reindex collection", "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string"},
                },
            },
        },
    ]


def _kb_guide_payload(kb_settings, index: KnowledgebaseIndex) -> dict[str, Any]:
    collections = index.list_collections()
    return {
        "server_name": kb_settings.server_name,
        "server_title": kb_settings.server_title,
        "purpose": "Compiled wiki knowledgebase with immutable raw sources, maintained wiki pages, and generated outputs.",
        "docs_root": str(kb_settings.docs_root),
        "index_root": str(kb_settings.index_root),
        "collection_model": {
            "collection_id": "normalized namespace for a collection such as acme or billing",
            "doc_id": "collection_id plus normalized relative path, for example acme/guides/onboarding",
            "relative_path": "normalized path within a collection, without suffix",
            "supported_suffixes": [".md", ".txt"],
            "collection_kinds": ["raw", "wiki", "output"],
        },
        "compiled_wiki_model": {
            "raw": "immutable source evidence that agents can read but should not rewrite",
            "wiki": "maintained synthesis pages, concept pages, and curated indices",
            "output": "generated answers, reports, and artifacts filed back into the KB",
        },
        "agent_guidance": [
            "Use this KB whenever the user asks about uploaded docs, policies, onboarding, support, pricing, research notes, or generated wiki pages.",
            "Call get_kb_guide or get_collection_guide first when you need to understand what the uploaded docs cover.",
            "If you do not know the right collection, call search_docs without collection_id so the server can search all collections.",
            "Use the exact doc_id returned by search_docs or list_documents when calling get_document.",
            "Treat collection metadata as the source of truth for whether a collection is raw, wiki, or output.",
        ],
        "search_behavior": {
            "default_scope": "collection-specific when you know it, otherwise search all collections",
            "query_normalization": "natural-language queries are tokenized and normalized before FTS search",
            "fallback": "LIKE-based fallback is used if FTS parsing fails or returns no hits",
        },
        "write_behavior": {
            "small_edits": ["create_document", "append_section", "update_document_metadata", "update_collection_metadata"],
            "staged_edits": ["propose_document_change", "list_proposed_changes", "apply_proposed_change", "discard_proposed_change"],
            "maintenance": ["lint_collection", "reindex_collection"],
        },
        "write_surface": {
            "admin_routes": [
                "POST /admin/documents",
                "PUT /admin/documents/{collection_id}/{doc_path}",
                "DELETE /admin/documents/{collection_id}/{doc_path}",
                "POST /admin/reindex",
            ],
            "read_only_tools": ["list_collections", "list_documents", "search_docs", "get_document", "get_collection_guide", "get_kb_guide"],
        },
        "current_collections": collections,
        "collection_guides": [index.get_collection_guide(item["collection_id"]) for item in collections],
    }


def _admin_token(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> str:
    kb_settings = request.app.state.kb_settings
    expected = kb_settings.admin_api_key_value()
    if not expected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="KB admin API key is not configured.")
    if credentials is None or credentials.credentials != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid KB admin API key.")
    return credentials.credentials


def _ensure_text_payload(file: UploadFile | None, content: str | None) -> tuple[str, str | None]:
    if file is not None:
        text = file.file.read().decode("utf-8")
        return text, file.filename
    if content is not None:
        return content, None
    raise HTTPException(status_code=400, detail="Either file or content is required.")


def _resolve_doc_reference(collection_id: str | None, doc_id: str) -> tuple[str, str, str]:
    raw_doc_id = str(doc_id or "").strip().strip("/")
    if not raw_doc_id:
        raise HTTPException(status_code=400, detail="doc_id is required.")
    if "/" in raw_doc_id:
        resolved_collection, resolved_path = raw_doc_id.split("/", 1)
        resolved_collection = normalize_collection_id(resolved_collection)
        resolved_path = normalize_doc_path(resolved_path)
        if not resolved_collection or not resolved_path:
            raise HTTPException(status_code=400, detail="doc_id is invalid.")
        if collection_id:
            normalized_collection = normalize_collection_id(collection_id)
            if normalized_collection != resolved_collection:
                raise HTTPException(status_code=400, detail="collection_id does not match doc_id.")
        return resolved_collection, resolved_path, f"{resolved_collection}/{resolved_path}"
    if not collection_id:
        raise HTTPException(status_code=400, detail="collection_id is required when doc_id does not include a collection prefix.")
    resolved_collection = normalize_collection_id(collection_id)
    resolved_path = normalize_doc_path(raw_doc_id)
    if not resolved_path:
        raise HTTPException(status_code=400, detail="doc_id is invalid.")
    return resolved_collection, resolved_path, f"{resolved_collection}/{resolved_path}"


def _parse_limit(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(minimum, min(limit, maximum))


def _parse_offset(value: Any) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError):
        offset = 0
    return max(0, offset)


def _document_summary(document: dict[str, Any]) -> DocumentSummaryOut:
    return DocumentSummaryOut(**document)


def _document_detail(document: dict[str, Any]) -> DocumentDetailOut:
    return DocumentDetailOut(**document)


def _search_result(document: dict[str, Any]) -> SearchResultOut:
    return SearchResultOut(**document)


def _tool_document_detail(document: dict[str, Any]) -> dict[str, Any]:
    payload = dict(document)
    if "content" in payload:
        _, body = split_document_frontmatter(str(payload.get("content") or ""))
        payload["content"] = body.strip()
    if "excerpt" in payload:
        _, excerpt = split_document_frontmatter(str(payload.get("excerpt") or ""))
        payload["excerpt"] = excerpt.strip()
    return payload


def create_app(kb_settings=None) -> FastAPI:
    kb_settings = kb_settings or get_settings()
    app = FastAPI(
        title=kb_settings.server_title,
        version=kb_settings.server_version,
        description=kb_settings.server_description,
        docs_url=None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    index = KnowledgebaseIndex(kb_settings)
    app.state.kb_index = index
    app.state.kb_settings = kb_settings
    app.state.kb_stop_event = threading.Event()
    app.state.kb_sync_task = None

    async def sync_once() -> None:
        await asyncio.to_thread(index.sync_from_disk)

    async def reconcile_loop() -> None:
        while not app.state.kb_stop_event.is_set():
            try:
                await asyncio.to_thread(index.sync_from_disk)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("KB reconcile loop failed")
            try:
                await asyncio.to_thread(app.state.kb_stop_event.wait, kb_settings.reconcile_interval_seconds)
            except asyncio.CancelledError:
                raise

    @app.on_event("startup")
    async def _startup() -> None:
        index.initialize()
        await sync_once()
        app.state.kb_stop_event.clear()
        app.state.kb_sync_task = asyncio.create_task(reconcile_loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        app.state.kb_stop_event.set()
        task = app.state.kb_sync_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": kb_settings.server_name, "version": kb_settings.server_version}

    @app.get("/server.json", response_model=MCPManifestOut)
    async def server_manifest() -> dict[str, Any]:
        return _manifest_payload(app.state.kb_settings)

    @app.get("/.well-known/mcp/server.json", response_model=MCPManifestOut, include_in_schema=False)
    async def well_known_manifest() -> dict[str, Any]:
        return _manifest_payload(app.state.kb_settings)

    @app.post("/mcp")
    async def mcp_rpc(request: Request, payload: JsonRpcRequest):
        if payload.jsonrpc != "2.0":
            return _jsonrpc_error(payload.id, -32600, "Only JSON-RPC 2.0 is supported.", status_code=400)

        method = payload.method
        params = payload.params or {}

        if method == "initialize":
            return _jsonrpc_result(
                payload.id,
                {
                    "protocolVersion": kb_settings.protocol_version,
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "serverInfo": {
                        "name": kb_settings.server_name,
                        "title": kb_settings.server_title,
                        "version": kb_settings.server_version,
                    },
                    "instructions": request.app.state.kb_index.get_prompt("mcp_initialize_instructions"),
                },
            )

        if method == "notifications/initialized":
            return Response(status_code=202)

        if method == "resources/list":
            return _jsonrpc_result(payload.id, {"resources": []})

        if method == "prompts/list":
            return _jsonrpc_result(payload.id, {"prompts": []})

        if method == "tools/list":
            return _jsonrpc_result(payload.id, {"tools": _tool_specs(kb_settings, request.app.state.kb_index)})

        if method == "tools/call":
            tool_name = str(params.get("name") or "").strip()
            tool_args = params.get("arguments") or {}
            index: KnowledgebaseIndex = request.app.state.kb_index

            if tool_name == "get_kb_guide":
                return _jsonrpc_result(payload.id, {"guide": _kb_guide_payload(kb_settings, index)})

            if tool_name == "get_collection_guide":
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or ""))
                if not collection_id:
                    return _jsonrpc_error(payload.id, -32602, "get_collection_guide requires collection_id.")
                return _jsonrpc_result(payload.id, {"guide": index.get_collection_guide(collection_id)})

            if tool_name == "list_collections":
                return _jsonrpc_result(payload.id, {"collections": index.list_collections()})

            if tool_name == "list_documents":
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or ""))
                if not collection_id:
                    return _jsonrpc_error(payload.id, -32602, "list_documents requires collection_id.")
                limit = _parse_limit(tool_args.get("limit"), default=kb_settings.default_search_limit, minimum=1, maximum=kb_settings.max_list_limit)
                offset = _parse_offset(tool_args.get("offset"))
                query = str(tool_args.get("query") or "").strip() or None
                documents = index.list_documents(collection_id=collection_id, limit=limit, offset=offset, query=query)
                total = index.count_documents(collection_id=collection_id, query=query)
                return _jsonrpc_result(
                    payload.id,
                    {
                        "collection_id": collection_id,
                        "query": query,
                        "limit": limit,
                        "offset": offset,
                        "total": total,
                        "has_more": offset + len(documents) < total,
                        "documents": documents,
                    },
                )

            if tool_name == "search_docs":
                query = str(tool_args.get("query") or "").strip()
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or "")) or None
                if not query:
                    return _jsonrpc_error(payload.id, -32602, "search_docs requires query.")
                limit = _parse_limit(tool_args.get("limit"), default=kb_settings.default_search_limit, minimum=1, maximum=kb_settings.max_search_limit)
                results = index.search(collection_id=collection_id, query=query, limit=limit)
                return _jsonrpc_result(payload.id, {"collection_id": collection_id, "query": query, "results": results})

            if tool_name == "answer_question":
                question = str(tool_args.get("question") or "").strip()
                if not question:
                    return _jsonrpc_error(payload.id, -32602, "answer_question requires question.")
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or "")) or None
                max_documents = _parse_limit(
                    tool_args.get("max_documents"),
                    default=kb_settings.answer_default_documents,
                    minimum=1,
                    maximum=kb_settings.answer_max_documents,
                )
                answer = index.answer_question(
                    question=question,
                    collection_id=collection_id,
                    max_documents=max_documents,
                    max_chars_per_doc=kb_settings.answer_max_chars_per_doc,
                )
                return _jsonrpc_result(payload.id, answer)

            if tool_name == "get_document":
                collection_id, doc_path, resolved_doc_id = _resolve_doc_reference(tool_args.get("collection_id"), str(tool_args.get("doc_id") or ""))
                document = index.get_document_by_id(resolved_doc_id)
                if not document:
                    document = index.get_document(collection_id, doc_path)
                if not document:
                    return _jsonrpc_error(payload.id, -32602, "Document not found.", status_code=404)
                return _jsonrpc_result(payload.id, {"document": _tool_document_detail(document)})

            if tool_name == "create_document":
                try:
                    result = index.create_document(
                        collection_id=str(tool_args.get("collection_id") or ""),
                        doc_path=str(tool_args.get("doc_path") or ""),
                        raw_text=str(tool_args.get("raw_text") or ""),
                        filename=tool_args.get("filename"),
                        content_type=tool_args.get("content_type"),
                        title=tool_args.get("title"),
                        tags=normalize_text_list(tool_args.get("tags")) or None,
                        frontmatter_patch=dict(tool_args.get("frontmatter_patch") or {}),
                        kb_source_docs=normalize_text_list(tool_args.get("kb_source_docs")) or None,
                    )
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, result)

            if tool_name == "append_section":
                try:
                    result = index.append_section(
                        collection_id=str(tool_args.get("collection_id") or ""),
                        doc_path=str(tool_args.get("doc_path") or ""),
                        heading=str(tool_args.get("heading") or ""),
                        content=str(tool_args.get("content") or ""),
                        frontmatter_patch=dict(tool_args.get("frontmatter_patch") or {}),
                        kb_source_docs=normalize_text_list(tool_args.get("kb_source_docs")) or None,
                    )
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, result)

            if tool_name == "update_document_metadata":
                try:
                    result = index.update_document_metadata(
                        collection_id=str(tool_args.get("collection_id") or ""),
                        doc_path=str(tool_args.get("doc_path") or ""),
                        title=tool_args.get("title"),
                        tags=normalize_text_list(tool_args.get("tags")) or None,
                        frontmatter_patch=dict(tool_args.get("frontmatter_patch") or {}),
                        kb_source_docs=normalize_text_list(tool_args.get("kb_source_docs")) or None,
                    )
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, result)

            if tool_name == "update_collection_metadata":
                collection_id = str(tool_args.get("collection_id") or "")
                if not collection_id.strip():
                    return _jsonrpc_error(payload.id, -32602, "update_collection_metadata requires collection_id.")
                metadata = {
                    "kind": tool_args.get("kind"),
                    "mutable": tool_args.get("mutable"),
                    "summary": tool_args.get("summary"),
                    "source_collections": tool_args.get("source_collections"),
                }
                try:
                    result = index.update_collection_metadata(collection_id, metadata)
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, {"collection": result})

            if tool_name == "propose_document_change":
                collection_id = str(tool_args.get("collection_id") or "")
                operation = str(tool_args.get("operation") or "")
                if not collection_id.strip() or not operation.strip():
                    return _jsonrpc_error(payload.id, -32602, "propose_document_change requires collection_id and operation.")
                try:
                    result = index.propose_document_change(
                        collection_id=collection_id,
                        operation=operation,
                        doc_path=tool_args.get("doc_path"),
                        raw_text=tool_args.get("raw_text"),
                        title=tool_args.get("title"),
                        tags=normalize_text_list(tool_args.get("tags")) or None,
                        frontmatter_patch=dict(tool_args.get("frontmatter_patch") or {}),
                        kb_source_docs=normalize_text_list(tool_args.get("kb_source_docs")) or None,
                        heading=tool_args.get("heading"),
                        content=tool_args.get("content"),
                        filename=tool_args.get("filename"),
                        content_type=tool_args.get("content_type"),
                    )
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, {"proposal": result})

            if tool_name == "list_proposed_changes":
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or "")) or None
                status_value = str(tool_args.get("status") or "").strip() or None
                limit = _parse_limit(tool_args.get("limit"), default=kb_settings.default_search_limit, minimum=1, maximum=kb_settings.max_list_limit)
                return _jsonrpc_result(
                    payload.id,
                    {
                        "collection_id": collection_id,
                        "changes": index.list_proposed_changes(collection_id=collection_id, status=status_value, limit=limit),
                    },
                )

            if tool_name == "apply_proposed_change":
                change_id = str(tool_args.get("change_id") or "").strip()
                if not change_id:
                    return _jsonrpc_error(payload.id, -32602, "apply_proposed_change requires change_id.")
                try:
                    result = index.apply_proposed_change(change_id)
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, result)

            if tool_name == "discard_proposed_change":
                change_id = str(tool_args.get("change_id") or "").strip()
                if not change_id:
                    return _jsonrpc_error(payload.id, -32602, "discard_proposed_change requires change_id.")
                try:
                    result = index.discard_proposed_change(change_id)
                except ValueError as exc:
                    return _jsonrpc_error(payload.id, -32602, str(exc))
                return _jsonrpc_result(payload.id, result)

            if tool_name == "lint_collection":
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or ""))
                if not collection_id:
                    return _jsonrpc_error(payload.id, -32602, "lint_collection requires collection_id.")
                return _jsonrpc_result(payload.id, {"lint": index.lint_collection(collection_id)})

            if tool_name == "reindex_collection":
                collection_id = normalize_collection_id(str(tool_args.get("collection_id") or "")) or None
                summary = await asyncio.to_thread(index.sync_from_disk, collection_id=collection_id)
                return _jsonrpc_result(payload.id, {"summary": summary, "collection_id": collection_id})

            return _jsonrpc_error(payload.id, -32601, f"Unknown tool '{tool_name}'.", status_code=404)

        return _jsonrpc_error(payload.id, -32601, f"Unsupported method '{method}'.", status_code=404)

    @app.get("/admin/collections", response_model=CollectionListOut)
    async def admin_collections(_: str = Depends(_admin_token)) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        return {"collections": index.list_collections()}

    @app.get("/admin/documents", response_model=DocumentListOut)
    async def admin_documents(
        _: str = Depends(_admin_token),
        collection_id: str | None = Query(None),
        limit: int = Query(min(25, kb_settings.max_list_limit), ge=1, le=kb_settings.max_list_limit),
        offset: int = Query(0, ge=0),
        query: str | None = Query(None),
    ) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        normalized_collection_id = normalize_collection_id(collection_id) if collection_id else None
        normalized_query = str(query or "").strip() or None
        documents = index.list_documents(
            collection_id=normalized_collection_id,
            limit=limit,
            offset=offset,
            query=normalized_query,
        )
        total = index.count_documents(collection_id=normalized_collection_id, query=normalized_query)
        return {
            "collection_id": normalized_collection_id,
            "query": normalized_query,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(documents) < total,
            "documents": documents,
        }

    @app.get("/admin/documents/{collection_id}/{doc_path:path}", response_model=WriteResponseOut)
    async def admin_get_document(_: str = Depends(_admin_token), collection_id: str = "", doc_path: str = "") -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        document = index.get_document(collection_id, doc_path)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found.")
        return {"document": document}

    @app.post("/admin/documents", response_model=WriteResponseOut)
    async def admin_create_document(
        _: str = Depends(_admin_token),
        collection_id: str = Form(...),
        slug: str | None = Form(None),
        title: str | None = Form(None),
        tags: str | None = Form(None),
        content: str | None = Form(None),
        file: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise HTTPException(status_code=400, detail="collection_id is required.")
        raw_text, filename = _ensure_text_payload(file, content)
        if len(raw_text.encode("utf-8")) > kb_settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Uploaded document is too large.")
        filename_stem = Path(file.filename).stem if file and file.filename else ""
        doc_path = normalize_doc_path(slug or filename_stem)
        if not doc_path:
            raise HTTPException(status_code=400, detail="slug is required.")
        normalized_tags = normalize_text_list(tags)
        try:
            index.write_document(
                collection_id=collection_id,
                doc_path=doc_path,
                raw_text=raw_text,
                filename=filename,
                content_type=file.content_type if file else None,
                title=title,
                tags=normalized_tags or None,
                kb_origin="admin",
            )
        except ValueError as exc:
            raise HTTPException(status_code=403 if "not writable" in str(exc).lower() else 400, detail=str(exc)) from exc
        document = index.get_document(collection_id, doc_path)
        if not document:
            raise HTTPException(status_code=500, detail="Document write succeeded but indexing failed.")
        return {"document": document}

    @app.put("/admin/documents/{collection_id}/{doc_path:path}", response_model=WriteResponseOut)
    async def admin_update_document(
        _: str = Depends(_admin_token),
        collection_id: str = "",
        doc_path: str = "",
        title: str | None = Form(None),
        tags: str | None = Form(None),
        content: str | None = Form(None),
        file: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        collection_id = normalize_collection_id(collection_id)
        doc_path = normalize_doc_path(doc_path)
        existing = index.get_document(collection_id, doc_path)
        if not existing:
            raise HTTPException(status_code=404, detail="Document not found.")
        raw_text, filename = _ensure_text_payload(file, content if content is not None else existing["content"])
        if len(raw_text.encode("utf-8")) > kb_settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Uploaded document is too large.")
        normalized_tags = normalize_text_list(tags)
        try:
            index.write_document(
                collection_id=collection_id,
                doc_path=doc_path,
                raw_text=raw_text,
                filename=filename,
                content_type=file.content_type if file else existing["content_type"],
                title=title,
                tags=normalized_tags or None,
                kb_origin="admin",
            )
        except ValueError as exc:
            raise HTTPException(status_code=403 if "not writable" in str(exc).lower() else 400, detail=str(exc)) from exc
        document = index.get_document(collection_id, doc_path)
        if not document:
            raise HTTPException(status_code=500, detail="Document write succeeded but indexing failed.")
        return {"document": document}

    @app.delete("/admin/documents/{collection_id}/{doc_path:path}")
    async def admin_delete_document(_: str = Depends(_admin_token), collection_id: str = "", doc_path: str = "") -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        collection_id = normalize_collection_id(collection_id)
        doc_path = normalize_doc_path(doc_path)
        if not index._is_collection_writable(collection_id):
            raise HTTPException(status_code=403, detail=f"Collection '{collection_id}' is not writable.")
        path = index.resolve_path(collection_id, doc_path)
        if path.exists():
            path.unlink()
        try:
            result = index.remove_document(collection_id, doc_path)
        except ValueError as exc:
            raise HTTPException(status_code=403 if "not writable" in str(exc).lower() else 400, detail=str(exc)) from exc
        if result.get("action") == "missing":
            raise HTTPException(status_code=404, detail="Document not found.")
        return result

    @app.post("/admin/reindex")
    async def admin_reindex(_: str = Depends(_admin_token), collection_id: str | None = Query(None)) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        summary = await asyncio.to_thread(index.sync_from_disk, collection_id=collection_id)
        return {"summary": summary, "collection_id": normalize_collection_id(collection_id) if collection_id else None}

    @app.get("/admin/prompts", response_model=PromptListOut)
    async def admin_list_prompts(
        _: str = Depends(_admin_token),
        collection_id: str | None = Query(None),
    ) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        normalized = normalize_collection_id(collection_id) if collection_id else None
        prompts = index.list_prompts(collection_id=normalized)
        return {"requested_collection_id": normalized, "prompts": prompts}

    @app.put("/admin/prompts/{key}", response_model=PromptUpdateOut)
    async def admin_set_prompt(
        key: str,
        payload: PromptUpdateIn,
        _: str = Depends(_admin_token),
    ) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        try:
            return index.set_prompt(key=key, value=payload.value, collection_id=payload.collection_id)
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if message.startswith("Unknown prompt key") else 400
            raise HTTPException(status_code=status_code, detail=message) from exc

    @app.delete("/admin/prompts/{key}", response_model=PromptDeleteOut)
    async def admin_delete_prompt(
        key: str,
        _: str = Depends(_admin_token),
        collection_id: str | None = Query(None),
    ) -> dict[str, Any]:
        index: KnowledgebaseIndex = app.state.kb_index
        try:
            return index.delete_prompt(key=key, collection_id=collection_id)
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if message.startswith("Unknown prompt key") else 400
            raise HTTPException(status_code=status_code, detail=message) from exc

    return app


app = create_app()
