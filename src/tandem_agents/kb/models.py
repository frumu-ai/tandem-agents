from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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


class CollectionSummaryOut(BaseModel):
    collection_id: str
    document_count: int
    updated_at: str | None = None
    summary: str = ""
    key_topics: list[str] = Field(default_factory=list)
    kind: str = "wiki"
    mutable: bool = True
    source_collections: list[str] = Field(default_factory=list)


class DocumentSummaryOut(BaseModel):
    collection_id: str
    doc_id: str
    slug: str
    relative_path: str = ""
    title: str
    source_label: str = ""
    path: str
    source_path: str = ""
    content_type: str
    tags: list[str] = Field(default_factory=list)
    kb_role: str = ""
    kb_origin: str = ""
    kb_source_docs: list[str] = Field(default_factory=list)
    kb_backlinks: list[str] = Field(default_factory=list)
    collection_kind: str = "wiki"
    collection_mutable: bool = True
    kb_updated_at: str = ""
    excerpt: str = ""
    size_bytes: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class DocumentDetailOut(DocumentSummaryOut):
    content: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class SearchResultOut(DocumentSummaryOut):
    heading: str = ""
    score: float = 0.0
    snippet: str = ""


class WriteResponseOut(BaseModel):
    document: DocumentDetailOut


class CollectionListOut(BaseModel):
    collections: list[CollectionSummaryOut]


class DocumentListOut(BaseModel):
    collection_id: str | None = None
    query: str | None = None
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    documents: list[DocumentSummaryOut]


class SearchResponseOut(BaseModel):
    collection_id: str | None = None
    query: str
    results: list[SearchResultOut]


class PromptCollectionOverrideOut(BaseModel):
    collection_id: str
    updated_at: str


class PromptOut(BaseModel):
    key: str
    description: str
    supports_collection_override: bool
    default: str
    global_override: str | None = None
    global_override_updated_at: str | None = None
    collection_override: str | None = None
    collection_override_updated_at: str | None = None
    current: str
    scope: str
    requested_collection_id: str | None = None
    collection_overrides: list[PromptCollectionOverrideOut] = Field(default_factory=list)


class PromptListOut(BaseModel):
    requested_collection_id: str | None = None
    prompts: list[PromptOut]


class PromptUpdateIn(BaseModel):
    value: str
    collection_id: str | None = None


class PromptUpdateOut(BaseModel):
    key: str
    collection_id: str | None = None
    value: str
    updated_at: str


class PromptDeleteOut(BaseModel):
    key: str
    collection_id: str | None = None
    removed: int
