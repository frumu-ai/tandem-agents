from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_SUFFIXES = {".md", ".txt"}
COLLECTION_METADATA_FILENAME = ".kb_collection.yaml"
COLLECTION_KIND_VALUES = {"raw", "wiki", "output"}
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
WHITESPACE_PATTERN = re.compile(r"\s+")
PATH_SEPARATOR_PATTERN = re.compile(r"[\\/]+")


def slugify(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def normalize_collection_id(value: str) -> str:
    return slugify(value)


def normalize_doc_slug(value: str) -> str:
    return slugify(value)


def normalize_doc_path(value: str) -> str:
    text = str(value or "").strip().strip("/\\")
    if not text:
        return ""
    parts = [normalize_doc_slug(part) for part in PATH_SEPARATOR_PATTERN.split(text) if normalize_doc_slug(part)]
    return "/".join(parts)


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,\n]", value)
        return [normalize_doc_slug(candidate) for candidate in candidates if normalize_doc_slug(candidate)]
    if isinstance(value, list):
        return [normalize_doc_slug(item) for item in value if normalize_doc_slug(str(item))]
    return []


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,\n]", value)
        return [candidate.strip() for candidate in candidates if candidate.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_collection_list(value: Any) -> list[str]:
    return [normalize_collection_id(item) for item in normalize_text_list(value) if normalize_collection_id(item)]


def normalize_collection_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind in COLLECTION_KIND_VALUES:
        return kind
    return "wiki"


def normalize_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def titleize_slug(value: str) -> str:
    text = str(value or "").replace("-", " ").replace("_", " ").strip()
    return text.title() if text else "Untitled Document"


def strip_markdown(text: str) -> str:
    stripped = re.sub(r"`([^`]*)`", r"\1", text)
    stripped = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", stripped)
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
    stripped = re.sub(r"^#{1,6}\s+", "", stripped, flags=re.MULTILINE)
    stripped = WHITESPACE_PATTERN.sub(" ", stripped)
    return stripped.strip()


def build_excerpt(text: str, limit: int = 240) -> str:
    excerpt = strip_markdown(text)
    return excerpt[:limit].rstrip()


def build_content_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedDocument:
    raw_text: str
    body_text: str
    frontmatter: dict[str, Any]
    title: str
    tags: list[str]
    excerpt: str
    content_type: str
    headings: list[str]


def _parse_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_PATTERN.match(raw_text)
    if not match:
        return {}, raw_text
    metadata_raw, body = match.groups()
    try:
        parsed = yaml.safe_load(metadata_raw) or {}
    except yaml.YAMLError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}, body


def split_document_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    if raw_text.lstrip().startswith("---"):
        return _parse_frontmatter(raw_text)
    return {}, raw_text


def render_document_frontmatter(metadata: dict[str, Any], body_text: str) -> str:
    metadata = dict(metadata or {})
    rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    body = str(body_text or "").lstrip("\n")
    if body:
        return f"---\n{rendered}\n---\n\n{body}"
    return f"---\n{rendered}\n---\n"


def merge_document_frontmatter(
    raw_text: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    metadata, body = split_document_frontmatter(raw_text)
    metadata = dict(metadata)
    original_metadata = dict(metadata)
    changed = False
    if title is not None:
        metadata["title"] = title
        changed = True
    if tags is not None:
        metadata["tags"] = tags
        changed = True
    if extra:
        for key, value in extra.items():
            if value is not None:
                metadata[key] = value
                changed = True
    if not changed and metadata == original_metadata and raw_text.lstrip().startswith("---"):
        return raw_text
    if not metadata:
        return raw_text
    return render_document_frontmatter(metadata, body)


def parse_document(raw_text: str, *, slug: str, suffix: str, filename: str | None = None) -> ParsedDocument:
    frontmatter, body = split_document_frontmatter(raw_text)
    headings = [match.group(1).strip() for match in HEADING_PATTERN.finditer(body)]
    title = str(frontmatter.get("title") or (headings[0] if headings else titleize_slug(slug))).strip()
    tags = normalize_tags(frontmatter.get("tags"))
    excerpt = build_excerpt(body)
    content_type = "text/markdown" if suffix == ".md" else "text/plain"
    if filename and filename.lower().endswith(".md"):
        content_type = "text/markdown"
    return ParsedDocument(
        raw_text=raw_text,
        body_text=body.strip(),
        frontmatter=frontmatter,
        title=title or titleize_slug(slug),
        tags=tags,
        excerpt=excerpt,
        content_type=content_type,
        headings=headings,
    )


def split_chunks(body_text: str, *, max_chars: int = 1200) -> list[tuple[str, str]]:
    lines = body_text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines, current_heading
        text = "\n".join(current_lines).strip()
        if text or current_heading:
            sections.append((current_heading, current_lines[:]))
        current_lines = []

    for line in lines:
        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            flush()
            current_heading = heading_match.group(1).strip()
            continue
        current_lines.append(line)

    flush()

    if not sections:
        sections = [("", lines[:])]

    chunks: list[tuple[str, str]] = []
    for heading, section_lines in sections:
        section_text = "\n".join(section_lines).strip()
        if not section_text:
            continue
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section_text) if part.strip()]
        if not paragraphs:
            paragraphs = [section_text]
        buffer: list[str] = []
        buffer_len = 0
        for paragraph in paragraphs:
            paragraph_len = len(paragraph)
            if buffer and buffer_len + paragraph_len + 2 > max_chars:
                chunks.append((heading, "\n\n".join(buffer).strip()))
                buffer = [paragraph]
                buffer_len = paragraph_len
            else:
                buffer.append(paragraph)
                buffer_len += paragraph_len + 2
        if buffer:
            chunks.append((heading, "\n\n".join(buffer).strip()))

    if not chunks:
        text = body_text.strip()
        if text:
            chunks.append(("", text))
    return chunks


def collection_document_id(collection_id: str, slug: str) -> str:
    return f"{normalize_collection_id(collection_id)}/{normalize_doc_path(slug)}"


def collection_dir(docs_root: Path, collection_id: str) -> Path:
    return docs_root / normalize_collection_id(collection_id)


def collection_metadata_path(docs_root: Path, collection_id: str) -> Path:
    return collection_dir(docs_root, collection_id) / COLLECTION_METADATA_FILENAME


def normalize_collection_metadata(collection_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(metadata or {})
    normalized_kind = normalize_collection_kind(raw.get("kind"))
    normalized_mutable = normalize_bool(raw.get("mutable"), default=normalized_kind != "raw")
    if normalized_kind == "raw":
        normalized_mutable = False
    return {
        "collection_id": normalize_collection_id(collection_id),
        "kind": normalized_kind,
        "mutable": normalized_mutable,
        "summary": str(raw.get("summary") or "").strip(),
        "source_collections": normalize_collection_list(raw.get("source_collections") or raw.get("sources")),
    }


def load_collection_metadata(docs_root: Path, collection_id: str) -> dict[str, Any]:
    path = collection_metadata_path(docs_root, collection_id)
    if not path.exists():
        return normalize_collection_metadata(collection_id, None)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return normalize_collection_metadata(collection_id, raw)


def write_collection_metadata(docs_root: Path, collection_id: str, metadata: dict[str, Any]) -> Path:
    path = collection_metadata_path(docs_root, collection_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_collection_metadata(collection_id, metadata)
    rendered = yaml.safe_dump(
        {
            "kind": normalized["kind"],
            "mutable": normalized["mutable"],
            "summary": normalized["summary"],
            "source_collections": normalized["source_collections"],
        },
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    path.write_text(f"{rendered}\n", encoding="utf-8")
    return path


def document_path(docs_root: Path, collection_id: str, slug: str, suffix: str = ".md") -> Path:
    normalized = normalize_doc_path(slug)
    if not normalized:
        raise ValueError("document path cannot be empty")
    return collection_dir(docs_root, collection_id) / Path(*normalized.split("/")).with_suffix(suffix)


def existing_document_path(docs_root: Path, collection_id: str, slug: str) -> Path | None:
    base = collection_dir(docs_root, collection_id)
    normalized_slug = normalize_doc_path(slug)
    if not normalized_slug:
        return None
    for suffix in (".md", ".txt"):
        candidate = base / Path(*normalized_slug.split("/")).with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def file_suffix_for_text(filename: str | None, content_type: str | None = None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in SUPPORTED_SUFFIXES:
            return suffix
    if content_type and "markdown" in content_type.lower():
        return ".md"
    if content_type and "plain" in content_type.lower():
        return ".txt"
    return ".md"
