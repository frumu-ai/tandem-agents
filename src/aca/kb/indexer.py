from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

try:
    import yaml as _yaml  # type: ignore
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore

from . import prompts as prompt_defaults
from .settings import KBSettings


logger = logging.getLogger("aca.kb.indexer")

PROMPTS_SEEDED_SENTINEL_KEY = "__seeded__"
from .storage import (
    ParsedDocument,
    build_content_hash,
    collection_document_id,
    document_path,
    existing_document_path,
    load_collection_metadata,
    merge_document_frontmatter,
    file_suffix_for_text,
    normalize_collection_id,
    normalize_doc_path,
    normalize_doc_slug,
    normalize_text_list,
    render_document_frontmatter,
    parse_document,
    split_document_frontmatter,
    write_collection_metadata,
    split_chunks,
)


GUIDE_ROLE_WEIGHTS = {
    "guide": 0,
    "faq": 1,
    "policy": 2,
    "reference": 3,
    "runbook": 4,
}

GUIDE_STOPWORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "about",
    "as",
    "at",
    "be",
    "can",
    "could",
    "by",
    "did",
    "do",
    "does",
    "each",
    "every",
    "for",
    "from",
    "get",
    "give",
    "how",
    "help",
    "in",
    "is",
    "i",
    "it",
    "just",
    "like",
    "me",
    "might",
    "may",
    "my",
    "mine",
    "must",
    "need",
    "not",
    "of",
    "on",
    "please",
    "show",
    "tell",
    "or",
    "our",
    "us",
    "we",
    "want",
    "the",
    "their",
    "to",
    "us",
    "use",
    "want",
    "would",
    "with",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "you",
    "your",
}

SEARCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")

DEFINITION_DOC_HINTS = {"about", "overview", "readme", "intro", "introduction", "guide", "glossary"}
POLICY_DOC_HINTS = {"policy", "rules", "conduct", "compliance", "approval", "billing", "refund"}
PROCEDURE_DOC_HINTS = {"runbook", "checklist", "procedure", "process", "workflow", "troubleshooting", "setup"}
FACT_DOC_HINTS = {"faq", "schedule", "contacts", "pricing", "logistics", "reference"}
EXTRACT_GENERIC_TERMS = {
    "answer",
    "approval",
    "approvals",
    "assistant",
    "bot",
    "discord",
    "event",
    "events",
    "knowledgebase",
    "policy",
    "policies",
    "question",
    "rule",
    "rules",
    "slack",
    "telegram",
    "user",
    "users",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return default
    return parsed if parsed is not None else default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _fts_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in SEARCH_TOKEN_PATTERN.findall(query or ""):
        normalized = token.lower()
        if normalized in GUIDE_STOPWORDS:
            continue
        if len(normalized) == 1 and not normalized.isdigit():
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms


def _clean_body(raw_text: str) -> str:
    _, body = split_document_frontmatter(str(raw_text or ""))
    return body.strip()


def _truncate_text(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _source_label(title: str, relative_path: str) -> str:
    raw = str(title or "").strip() or str(relative_path or "").strip() or "Knowledgebase Source"
    stem = raw.rsplit("/", 1)[-1].removesuffix(".md").removesuffix(".txt")
    words = [part for part in re.split(r"[-_\s]+", stem) if part]
    if not words:
        return "Knowledgebase Source"
    return " ".join(word.upper() if word.lower() in {"faq", "api", "kb"} else word[:1].upper() + word[1:] for word in words)


def _inferred_doc_role(profile: dict[str, Any]) -> str:
    role = str(profile.get("kb_role") or "").strip().lower()
    if role:
        return role
    haystack = " ".join(
        str(profile.get(key) or "").lower()
        for key in ("title", "relative_path", "source_path", "summary")
    )
    for candidate, hints in [
        ("policy", POLICY_DOC_HINTS),
        ("runbook", {"runbook"}),
        ("faq", {"faq"}),
        ("guide", DEFINITION_DOC_HINTS | {"guide"}),
        ("reference", FACT_DOC_HINTS),
    ]:
        if any(hint in haystack for hint in hints):
            return candidate
    return ""


def _classify_question(query: str) -> str:
    text = str(query or "").strip().lower()
    if not text:
        return "unknown"
    if text.startswith(("how do ", "how does ", "how should ", "how to ", "what should ", "what do ")) or any(term in text for term in ("steps", "procedure", "process", "runbook")):
        return "procedure"
    if any(term in text for term in ("policy", "policies", "rule", "rules", "allowed", "approval", "refund", "billing", "escalation")):
        return "policy"
    if re.match(r"^(what|who)\s+(is|are|was|were)\b", text) or text.startswith(("tell me about ", "describe ")):
        return "definition"
    if text.startswith(("when ", "where ", "which ", "who ", "can ", "does ", "do ", "is ", "are ")) or "?" in text:
        return "fact"
    return "unknown"


def _definition_subject_terms(query: str) -> list[str]:
    text = str(query or "").strip().lower()
    text = re.sub(r"^(what|who)\s+(is|are|was|were)\s+", "", text)
    text = re.sub(r"^(tell me about|describe)\s+", "", text)
    text = text.strip(" ?.!:")
    return [term for term in _fts_terms(text) if term not in GUIDE_STOPWORDS]


def _sentences(text: str) -> list[str]:
    segments: list[str] = []
    paragraph: list[str] = []
    pending_prefix = ""
    bullet_group_active = False

    def flush_paragraph() -> None:
        nonlocal bullet_group_active
        if not paragraph:
            bullet_group_active = False
            return
        cleaned = re.sub(r"\s+", " ", " ".join(paragraph)).strip()
        paragraph.clear()
        bullet_group_active = False
        if not cleaned:
            return
        split_sentences = [sentence.strip() for sentence in SENTENCE_PATTERN.split(cleaned) if sentence.strip()]
        segments.extend(split_sentences or [cleaned])

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if paragraph and paragraph[-1].endswith(":"):
                pending_prefix = re.sub(r"\s+", " ", " ".join(paragraph)).strip()
                paragraph.clear()
                continue
            flush_paragraph()
            continue
        if line.startswith("#"):
            flush_paragraph()
            pending_prefix = ""
            continue
        is_bullet = bool(re.match(r"^([-*]|\d+[.)])\s+", line))
        line = re.sub(r"^([-*]|\d+[.)])\s+", "", line)
        line = re.sub(r"^>\s*", "", line)
        if is_bullet and pending_prefix:
            paragraph.append(pending_prefix)
            pending_prefix = ""
            bullet_group_active = True
        if is_bullet and (bullet_group_active or (paragraph and paragraph[-1].endswith(":"))):
            paragraph.append(line)
            continue
        if is_bullet:
            flush_paragraph()
            segments.append(line)
            continue
        pending_prefix = ""
        paragraph.append(line)
    flush_paragraph()
    return [segment for segment in segments if segment]


def _sentence_matches_terms(sentence: str, terms: list[str]) -> bool:
    lower = sentence.lower()
    return not terms or any(term in lower for term in terms)


def _is_question_sentence(sentence: str) -> bool:
    lower = sentence.strip().lower()
    return lower.endswith("?") or bool(re.match(r"^(what|who|when|where|why|how|can|could|would|should|do|does|is|are)\b", lower))


def _is_action_request(question: str) -> bool:
    lower = str(question or "").strip().lower()
    return bool(re.match(r"^(can|could|would|will|do|does)\s+(you|the bot|this bot|it|the assistant)\b", lower))


def _expand_extract_terms(terms: list[str]) -> list[str]:
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        if term.endswith("ing") and len(term) > 5:
            stem = term[:-3]
            if len(stem) > 2 and stem[-1] == stem[-2]:
                stem = stem[:-1]
            expanded.append(stem)
        if term.endswith("s") and len(term) > 3:
            expanded.append(term[:-1])
    return list(dict.fromkeys(expanded))


def _definition_sentence_score(sentence: str, terms: list[str]) -> int:
    if _is_question_sentence(sentence):
        return -10
    lower = sentence.lower()
    score = 0
    if _sentence_matches_terms(sentence, terms):
        score += 2
    if re.search(r"\b(is|are|means|refers to|defines|describes)\b", lower):
        score += 3
    if "appears" in lower or "seems" in lower:
        score -= 5
    return score


def _suggest_definition_answer(question: str, evidence: list[dict[str, Any]]) -> str:
    terms = _definition_subject_terms(question)
    candidates: list[tuple[int, str]] = []
    for index, item in enumerate(evidence):
        for sentence in _sentences(str(item.get("content") or "")):
            score = _definition_sentence_score(sentence, terms)
            if score > 0:
                candidates.append((score - index, sentence))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _suggest_extract_answer(question: str, evidence: list[dict[str, Any]], *, max_sentences: int = 3) -> str:
    terms = _fts_terms(question)
    content_terms = _expand_extract_terms([term for term in terms if term not in EXTRACT_GENERIC_TERMS] or terms)
    action_request = _is_action_request(question)
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    position = 0
    for item_index, item in enumerate(evidence):
        for sentence in _sentences(str(item.get("content") or "")):
            position += 1
            normalized = re.sub(r"\s+", " ", sentence).strip()
            if not normalized or normalized.lower() in seen or _is_question_sentence(normalized):
                continue
            seen.add(normalized.lower())
            lower = normalized.lower()
            term_hits = sum(1 for term in content_terms if term in lower)
            if content_terms and term_hits == 0:
                continue

            score = term_hits * 3
            if "exact fact" in lower:
                score += 4
            if any(phrase in lower for phrase in ("does not define", "not define", "no policy", "no answer", "not available in the current knowledgebase")):
                score += 8
            if action_request:
                if any(subject in lower for subject in ("bot", "assistant", "tool")):
                    score += 6
                if any(phrase in lower for phrase in ("must not", "cannot", "can't", "may not", "should not", "only explain", "directly unless")):
                    score += 8
                if any(phrase in lower for phrase in ("moderators may", "may:", "can approve", "approve permanent", "allowed")):
                    score += 3
                if lower.startswith("this document explains"):
                    score -= 6
            candidates.append((score - item_index, -position, normalized))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[str] = []
    for _, _, sentence in candidates[:max_sentences]:
        selected.append(sentence)
    return " ".join(selected)


def _suggest_answer(question: str, answer_mode: str, evidence: list[dict[str, Any]]) -> tuple[str, str]:
    if not evidence:
        return "", "unsupported"
    if answer_mode == "definition":
        answer = _suggest_definition_answer(question, evidence)
    else:
        answer = _suggest_extract_answer(question, evidence)
    if answer:
        return answer, "supported"
    return "", "partial"


class KnowledgebaseIndex:
    def __init__(self, settings: KBSettings):
        self.settings = settings
        self.db_path = settings.index_db_path
        self._sync_lock = threading.RLock()

    @contextmanager
    def connection(self) -> Iterable[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            columns = self._table_columns(conn, "documents")
            if not columns:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS documents (
                        doc_id TEXT PRIMARY KEY,
                        collection_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        title TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        body TEXT NOT NULL,
                        frontmatter_json TEXT NOT NULL DEFAULT '{}',
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        content_hash TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        modified_ns INTEGER NOT NULL
                    );
                    """
                )
            else:
                if "relative_path" not in columns:
                    conn.execute("ALTER TABLE documents ADD COLUMN relative_path TEXT NOT NULL DEFAULT ''")
                    conn.execute(
                        """
                        UPDATE documents
                        SET relative_path = CASE
                            WHEN relative_path IS NULL OR relative_path = '' THEN slug
                            ELSE relative_path
                        END
                        """
                    )
                conn.execute("DROP INDEX IF EXISTS idx_documents_collection_slug")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_collection_relative_path ON documents(collection_id, relative_path)"
                )

            conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_collection_relative_path
                    ON documents(collection_id, relative_path);

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS document_links (
                    source_doc_id TEXT NOT NULL,
                    target_doc_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'source',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_doc_id, target_doc_id, relation)
                );

                CREATE TABLE IF NOT EXISTS proposed_changes (
                    change_id TEXT PRIMARY KEY,
                    collection_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL DEFAULT '',
                    doc_path TEXT NOT NULL DEFAULT '',
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kb_prompts (
                    key TEXT NOT NULL,
                    collection_id TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (key, collection_id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    doc_id UNINDEXED,
                    collection_id UNINDEXED,
                    relative_path UNINDEXED,
                    slug UNINDEXED,
                    title,
                    heading,
                    content,
                    tags
                );
                """
            )

        self._bootstrap_prompts_from_seed()

    def _bootstrap_prompts_from_seed(self) -> None:
        seed_path = getattr(self.settings, "prompts_seed_file", None)
        # Always check the sentinel first; if seeding has already run, never re-run
        # — even if the owner cleared every override via the UI.
        with self.connection() as conn:
            sentinel = conn.execute(
                "SELECT value FROM kb_prompts WHERE key = ? AND collection_id = ''",
                (PROMPTS_SEEDED_SENTINEL_KEY,),
            ).fetchone()
            if sentinel:
                return
            if not seed_path:
                # No seed configured — record sentinel so future restarts skip the
                # check entirely, and so a later-set seed doesn't surprise an owner
                # who has been managing prompts via the UI.
                conn.execute(
                    "INSERT OR REPLACE INTO kb_prompts(key, collection_id, value, updated_at) VALUES (?, '', ?, ?)",
                    (PROMPTS_SEEDED_SENTINEL_KEY, "no-seed", self._utc_now()),
                )
                return

        try:
            seed_path_obj = Path(seed_path)
            if not seed_path_obj.exists():
                logger.warning("KB_PROMPTS_FILE %s does not exist; skipping seed.", seed_path)
                return
        except OSError as exc:
            logger.warning("KB_PROMPTS_FILE %s could not be checked: %s", seed_path, exc)
            return
        if _yaml is None:
            logger.warning(
                "KB_PROMPTS_FILE %s configured but pyyaml is not installed; skipping seed.",
                seed_path,
            )
            return
        try:
            raw = seed_path_obj.read_text(encoding="utf-8")
            payload = _yaml.safe_load(raw) or {}
        except OSError as exc:
            logger.warning("KB_PROMPTS_FILE %s could not be read: %s", seed_path, exc)
            return
        except _yaml.YAMLError as exc:
            logger.warning("KB_PROMPTS_FILE %s contains invalid YAML: %s", seed_path, exc)
            return
        if not isinstance(payload, dict):
            logger.warning(
                "KB_PROMPTS_FILE %s top-level YAML must be a mapping; got %s.",
                seed_path,
                type(payload).__name__,
            )
            return

        with self.connection() as conn:
            now = self._utc_now()
            inserted = 0
            for key, entry in payload.items():
                if not prompt_defaults.is_known_key(str(key)):
                    logger.warning("KB_PROMPTS_FILE %s contains unknown key '%s'; skipping.", seed_path, key)
                    continue
                if isinstance(entry, str):
                    conn.execute(
                        "INSERT OR REPLACE INTO kb_prompts(key, collection_id, value, updated_at) VALUES (?, '', ?, ?)",
                        (str(key), entry, now),
                    )
                    inserted += 1
                    continue
                if isinstance(entry, dict):
                    global_value = entry.get("value")
                    if isinstance(global_value, str):
                        conn.execute(
                            "INSERT OR REPLACE INTO kb_prompts(key, collection_id, value, updated_at) VALUES (?, '', ?, ?)",
                            (str(key), global_value, now),
                        )
                        inserted += 1
                    overrides = entry.get("collections") or {}
                    if isinstance(overrides, dict) and prompt_defaults.supports_collection_override(str(key)):
                        for cid, value in overrides.items():
                            cid_norm = normalize_collection_id(str(cid))
                            if not cid_norm or not isinstance(value, str):
                                continue
                            conn.execute(
                                "INSERT OR REPLACE INTO kb_prompts(key, collection_id, value, updated_at) VALUES (?, ?, ?, ?)",
                                (str(key), cid_norm, value, now),
                            )
                            inserted += 1
            conn.execute(
                "INSERT OR REPLACE INTO kb_prompts(key, collection_id, value, updated_at) VALUES (?, '', ?, ?)",
                (PROMPTS_SEEDED_SENTINEL_KEY, now, now),
            )
            logger.info(
                "Seeded KB prompts from %s (%d override row(s) inserted).",
                seed_path,
                inserted,
            )

    def get_prompt(self, key: str, collection_id: str | None = None) -> str:
        if not prompt_defaults.is_known_key(key):
            raise KeyError(f"Unknown prompt key: {key}")
        normalized = normalize_collection_id(collection_id) if collection_id else ""
        with self.connection() as conn:
            if normalized and prompt_defaults.supports_collection_override(key):
                row = conn.execute(
                    "SELECT value FROM kb_prompts WHERE key = ? AND collection_id = ?",
                    (key, normalized),
                ).fetchone()
                if row:
                    return str(row["value"])
            row = conn.execute(
                "SELECT value FROM kb_prompts WHERE key = ? AND collection_id = ''",
                (key,),
            ).fetchone()
            if row:
                return str(row["value"])
        return prompt_defaults.get_default(key)

    def list_prompts(self, collection_id: str | None = None) -> list[dict[str, Any]]:
        normalized = normalize_collection_id(collection_id) if collection_id else ""
        results: list[dict[str, Any]] = []
        with self.connection() as conn:
            for definition in prompt_defaults.PROMPT_KEYS:
                global_row = conn.execute(
                    "SELECT value, updated_at FROM kb_prompts WHERE key = ? AND collection_id = ''",
                    (definition.key,),
                ).fetchone()
                global_override = str(global_row["value"]) if global_row else None
                global_updated_at = str(global_row["updated_at"]) if global_row else None

                collection_override = None
                collection_updated_at = None
                if normalized and definition.supports_collection_override:
                    coll_row = conn.execute(
                        "SELECT value, updated_at FROM kb_prompts WHERE key = ? AND collection_id = ?",
                        (definition.key, normalized),
                    ).fetchone()
                    if coll_row:
                        collection_override = str(coll_row["value"])
                        collection_updated_at = str(coll_row["updated_at"])

                if collection_override is not None:
                    current = collection_override
                    scope = "collection"
                elif global_override is not None:
                    current = global_override
                    scope = "global"
                else:
                    current = definition.default
                    scope = "default"

                collection_overrides = []
                if definition.supports_collection_override:
                    rows = conn.execute(
                        "SELECT collection_id, updated_at FROM kb_prompts WHERE key = ? AND collection_id != '' ORDER BY collection_id",
                        (definition.key,),
                    ).fetchall()
                    collection_overrides = [
                        {"collection_id": str(r["collection_id"]), "updated_at": str(r["updated_at"])}
                        for r in rows
                    ]

                results.append(
                    {
                        "key": definition.key,
                        "description": definition.description,
                        "supports_collection_override": definition.supports_collection_override,
                        "default": definition.default,
                        "global_override": global_override,
                        "global_override_updated_at": global_updated_at,
                        "collection_override": collection_override,
                        "collection_override_updated_at": collection_updated_at,
                        "current": current,
                        "scope": scope,
                        "requested_collection_id": normalized or None,
                        "collection_overrides": collection_overrides,
                    }
                )
        return results

    def set_prompt(self, key: str, value: str, collection_id: str | None = None) -> dict[str, Any]:
        if not prompt_defaults.is_known_key(key):
            raise ValueError(f"Unknown prompt key: {key}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Prompt value must be a non-empty string.")
        normalized = normalize_collection_id(collection_id) if collection_id else ""
        if normalized and not prompt_defaults.supports_collection_override(key):
            raise ValueError(f"Prompt '{key}' does not support per-collection overrides.")
        now = self._utc_now()
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kb_prompts(key, collection_id, value, updated_at) VALUES (?, ?, ?, ?)",
                (key, normalized, value, now),
            )
        return {"key": key, "collection_id": normalized or None, "value": value, "updated_at": now}

    def delete_prompt(self, key: str, collection_id: str | None = None) -> dict[str, Any]:
        if not prompt_defaults.is_known_key(key):
            raise ValueError(f"Unknown prompt key: {key}")
        normalized = normalize_collection_id(collection_id) if collection_id else ""
        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM kb_prompts WHERE key = ? AND collection_id = ?",
                (key, normalized),
            )
            removed = cursor.rowcount or 0
        return {"key": key, "collection_id": normalized or None, "removed": int(removed)}

    def _row_to_document(self, row: sqlite3.Row, *, include_content: bool = False) -> dict[str, Any]:
        relative_path = str(row["relative_path"]) if "relative_path" in row.keys() else str(row["slug"])
        frontmatter = dict(_parse_json(row["frontmatter_json"], {}))
        clean_body = _clean_body(str(row["body"]))
        collection_metadata = self._collection_metadata(str(row["collection_id"]))
        source_docs = self._normalize_source_doc_ids(str(row["collection_id"]), frontmatter.get("kb_source_docs"))
        backlinks = self._document_backlinks(str(row["doc_id"]))
        title = str(row["title"])
        payload = {
            "collection_id": str(row["collection_id"]),
            "doc_id": str(row["doc_id"]),
            "slug": str(row["slug"]),
            "relative_path": relative_path,
            "title": title,
            "source_label": _source_label(title, relative_path),
            "path": str(row["source_path"]),
            "source_path": relative_path,
            "content_type": str(row["content_type"]),
            "tags": list(_parse_json(row["tags_json"], [])),
            "kb_role": str(frontmatter.get("kb_role") or frontmatter.get("role") or ""),
            "kb_origin": str(frontmatter.get("kb_origin") or "source"),
            "kb_source_docs": source_docs,
            "kb_backlinks": backlinks,
            "collection_kind": str(collection_metadata.get("kind") or "wiki"),
            "collection_mutable": bool(collection_metadata.get("mutable", True)),
            "kb_updated_at": str(frontmatter.get("kb_updated_at") or row["updated_at"]),
            "excerpt": _truncate_text(clean_body, 240),
            "size_bytes": int(row["size_bytes"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        if include_content:
            payload["content"] = str(row["body"])
            payload["frontmatter"] = frontmatter
        return payload

    def _doc_exists(self, conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()

    def _delete_doc_rows(self, conn: sqlite3.Connection, doc_id: str) -> None:
        chunk_ids = [int(row["chunk_id"]) for row in conn.execute("SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()]
        for chunk_id in chunk_ids:
            conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM document_links WHERE source_doc_id = ? OR target_doc_id = ?", (doc_id, doc_id))
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    def _collection_ids_on_disk(self) -> list[str]:
        docs_root = self.settings.docs_root
        if not docs_root.exists():
            return []
        collection_ids: set[str] = set()
        for path in docs_root.iterdir():
            if path.is_dir() and not path.name.startswith("."):
                collection_ids.add(normalize_collection_id(path.name))
        return sorted(collection_ids)

    def _collection_metadata(self, collection_id: str) -> dict[str, Any]:
        return load_collection_metadata(self.settings.docs_root, collection_id)

    def _is_collection_writable(self, collection_id: str) -> bool:
        metadata = self._collection_metadata(collection_id)
        return bool(metadata.get("mutable", True)) and str(metadata.get("kind") or "wiki") != "raw"

    def _ensure_collection_writable(self, collection_id: str) -> None:
        metadata = self._collection_metadata(collection_id)
        if not self._is_collection_writable(collection_id):
            raise ValueError(
                f"Collection '{normalize_collection_id(collection_id)}' is not writable "
                f"(kind={metadata.get('kind')}, mutable={metadata.get('mutable')})."
            )

    def _normalize_source_doc_ids(self, collection_id: str, source_docs: Any) -> list[str]:
        collection_id = normalize_collection_id(collection_id)
        normalized: list[str] = []
        for item in normalize_text_list(source_docs):
            value = str(item or "").strip().strip("/")
            if not value:
                continue
            if "/" in value:
                source_collection, source_path = value.split("/", 1)
                source_doc_id = collection_document_id(source_collection, source_path)
            else:
                source_doc_id = collection_document_id(collection_id, value)
            if source_doc_id not in normalized:
                normalized.append(source_doc_id)
        return normalized

    def _backlinks_for_doc(self, doc_id: str) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT target_doc_id
                FROM document_links
                WHERE source_doc_id = ? AND relation = 'source'
                ORDER BY target_doc_id
                """,
                (doc_id,),
            ).fetchall()
        backlinks = [str(row["target_doc_id"]) for row in rows if str(row["target_doc_id"]).strip()]
        return backlinks

    def _refresh_document_links(self, conn: sqlite3.Connection, *, doc_id: str, source_doc_ids: list[str]) -> None:
        now = self._utc_now()
        conn.execute(
            "DELETE FROM document_links WHERE target_doc_id = ? AND relation = 'source'",
            (doc_id,),
        )
        for source_doc_id in source_doc_ids:
            conn.execute(
                """
                INSERT OR REPLACE INTO document_links (
                    source_doc_id, target_doc_id, relation, created_at, updated_at
                ) VALUES (?, ?, 'source', ?, ?)
                """,
                (source_doc_id, doc_id, now, now),
            )

    def _current_document_paths(self, conn: sqlite3.Connection, collection_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT relative_path FROM documents WHERE collection_id = ?",
            (normalize_collection_id(collection_id),),
        ).fetchall()
        return {str(row["relative_path"]) for row in rows if str(row["relative_path"]).strip()}

    def _update_collection_manifest(self, collection_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        write_collection_metadata(self.settings.docs_root, collection_id, metadata)
        return self._collection_metadata(collection_id)

    def _document_source_docs(self, row: sqlite3.Row) -> list[str]:
        frontmatter = self._document_frontmatter(row)
        return self._normalize_source_doc_ids(str(row["collection_id"]), frontmatter.get("kb_source_docs"))

    def _document_backlinks(self, doc_id: str) -> list[str]:
        return self._backlinks_for_doc(doc_id)

    def _insert_chunks(
        self,
        conn: sqlite3.Connection,
        *,
        doc_id: str,
        collection_id: str,
        slug: str,
        title: str,
        tags: list[str],
        parsed: ParsedDocument,
    ) -> None:
        chunks = split_chunks(parsed.body_text)
        if not chunks:
            chunks = [("", parsed.body_text or "")]
        tag_text = " ".join(tags)
        for ordinal, (heading, content) in enumerate(chunks):
            cursor = conn.execute(
                """
                INSERT INTO chunks (doc_id, ordinal, heading, content)
                VALUES (?, ?, ?, ?)
                """,
                (doc_id, ordinal, heading, content),
            )
            chunk_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO chunks_fts (rowid, doc_id, collection_id, slug, title, heading, content, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, doc_id, collection_id, slug, title, heading, content, tag_text),
            )

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _document_frontmatter(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(_parse_json(row["frontmatter_json"], {}))

    def _document_profile(self, row: sqlite3.Row, *, include_headings: bool = False) -> dict[str, Any]:
        frontmatter = self._document_frontmatter(row)
        collection_metadata = self._collection_metadata(str(row["collection_id"]))
        summary = str(frontmatter.get("kb_summary") or frontmatter.get("summary") or "").strip()
        keywords = normalize_text_list(frontmatter.get("kb_keywords") or frontmatter.get("keywords"))
        use_cases = normalize_text_list(frontmatter.get("kb_use_cases") or frontmatter.get("use_cases"))
        role = str(frontmatter.get("kb_role") or frontmatter.get("role") or "").strip().lower()
        canonical = _is_true(frontmatter.get("kb_canonical") or frontmatter.get("canonical"))
        priority = _coerce_int(frontmatter.get("kb_priority") or frontmatter.get("priority"), 0)
        relative_path = str(row["relative_path"]) if "relative_path" in row.keys() else str(row["slug"])
        title = str(row["title"])
        return {
            "collection_id": str(row["collection_id"]),
            "doc_id": str(row["doc_id"]),
            "slug": str(row["slug"]),
            "relative_path": relative_path,
            "title": title,
            "source_label": _source_label(title, relative_path),
            "path": str(row["source_path"]),
            "source_path": relative_path,
            "excerpt": _truncate_text(_clean_body(str(row["body"])), 240),
            "tags": list(_parse_json(row["tags_json"], [])),
            "summary": summary,
            "keywords": keywords,
            "use_cases": use_cases,
            "kb_role": role,
            "kb_origin": str(frontmatter.get("kb_origin") or "source"),
            "kb_source_docs": self._normalize_source_doc_ids(str(row["collection_id"]), frontmatter.get("kb_source_docs")),
            "kb_backlinks": self._document_backlinks(str(row["doc_id"])),
            "collection_kind": str(collection_metadata.get("kind") or "wiki"),
            "collection_mutable": bool(collection_metadata.get("mutable", True)),
            "kb_updated_at": str(frontmatter.get("kb_updated_at") or row["updated_at"]),
            "kb_priority": priority,
            "kb_canonical": canonical,
            "updated_at": str(row["updated_at"]),
            "created_at": str(row["created_at"]),
            "headings": self._chunk_headings(str(row["doc_id"])) if include_headings else [],
        }

    def _chunk_headings(self, doc_id: str, *, limit: int = 8) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT heading
                FROM chunks
                WHERE doc_id = ? AND heading != ''
                ORDER BY ordinal ASC
                LIMIT ?
                """,
                (doc_id, limit),
            ).fetchall()
        return [str(row["heading"]) for row in rows if str(row["heading"]).strip()]

    def _topic_terms(self, profiles: list[dict[str, Any]]) -> list[str]:
        counter: Counter[str] = Counter()
        for profile in profiles:
            counter.update(term.lower() for term in profile.get("tags", []) if term)
            counter.update(term.lower() for term in profile.get("keywords", []) if term)
            counter.update(term.lower() for term in profile.get("use_cases", []) if term)
            counter.update(term.lower() for term in profile.get("headings", []) if term)
            for text in (profile.get("title", ""), profile.get("summary", ""), profile.get("excerpt", "")):
                for token in SEARCH_TOKEN_PATTERN.findall(str(text).lower()):
                    if token not in GUIDE_STOPWORDS and len(token) > 1:
                        counter[token] += 1
        return [term for term, _ in counter.most_common(12)]

    def _collection_guide(self, collection_id: str, profiles: list[dict[str, Any]]) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        collection_metadata = self._collection_metadata(collection_id)
        if not profiles:
            return {
                "collection_id": collection_id,
                "kind": str(collection_metadata.get("kind") or "wiki"),
                "mutable": bool(collection_metadata.get("mutable", True)),
                "source_collections": list(collection_metadata.get("source_collections") or []),
                "summary": str(collection_metadata.get("summary") or ""),
                "recommended_use_cases": [
                    "store raw sources" if collection_metadata.get("kind") == "raw" else "maintain wiki pages",
                    "file generated outputs" if collection_metadata.get("kind") == "output" else "read and synthesize knowledge",
                ],
                "recommended_queries": [collection_id] if collection_id else [],
                "canonical_documents": [],
                "key_topics": [],
                "doc_count": 0,
                "last_updated_at": None,
                "generated_from": [],
            }

        def guide_rank(profile: dict[str, Any]) -> tuple[int, int, int, datetime]:
            role = str(profile.get("kb_role") or "").strip().lower()
            role_rank = GUIDE_ROLE_WEIGHTS.get(role, len(GUIDE_ROLE_WEIGHTS) + 1)
            canonical_rank = 0 if profile.get("kb_canonical") else 1
            priority_rank = -_coerce_int(profile.get("kb_priority"), 0)
            updated_rank = _parse_iso_datetime(str(profile.get("updated_at") or ""))
            return (role_rank, canonical_rank, priority_rank, updated_rank)

        ordered = sorted(profiles, key=guide_rank)
        canonical_docs = ordered[: min(5, len(ordered))]
        explicit_summary = next((profile.get("summary") for profile in ordered if profile.get("summary")), "")
        if not explicit_summary:
            explicit_summary = str(collection_metadata.get("summary") or "").strip()
        if not explicit_summary:
            explicit_summary = next((profile.get("excerpt") for profile in canonical_docs if profile.get("excerpt")), "")
        if not explicit_summary:
            explicit_summary = "This collection contains uploaded documents."

        use_case_candidates: list[str] = []
        for profile in ordered:
            use_case_candidates.extend(profile.get("use_cases", []))
        recommended_use_cases = []
        for item in use_case_candidates:
            cleaned = str(item).strip()
            if cleaned and cleaned not in recommended_use_cases:
                recommended_use_cases.append(cleaned)
            if len(recommended_use_cases) >= 6:
                break
        if not recommended_use_cases:
            recommended_use_cases = [term for term in self._topic_terms(ordered)[:4]]

        topic_terms = self._topic_terms(ordered)
        recommended_queries = []
        for candidate in [*topic_terms, *[profile.get("title", "") for profile in canonical_docs]]:
            cleaned = str(candidate).strip()
            if cleaned and cleaned not in recommended_queries:
                recommended_queries.append(cleaned)
            if len(recommended_queries) >= 8:
                break

        canonical_payload = []
        for profile in canonical_docs:
            canonical_payload.append(
                {
                    "collection_id": profile["collection_id"],
                    "doc_id": profile["doc_id"],
                    "slug": profile["slug"],
                    "relative_path": profile["relative_path"],
                    "title": profile["title"],
                    "path": profile["path"],
                    "summary": profile.get("summary") or profile.get("excerpt", ""),
                    "kb_role": profile.get("kb_role", ""),
                    "kb_priority": profile.get("kb_priority", 0),
                    "kb_canonical": profile.get("kb_canonical", False),
                    "kb_origin": profile.get("kb_origin", ""),
                    "kb_source_docs": profile.get("kb_source_docs", []),
                    "kb_backlinks": profile.get("kb_backlinks", []),
                    "updated_at": profile.get("updated_at"),
                }
            )

        return {
            "collection_id": collection_id,
            "kind": str(collection_metadata.get("kind") or "wiki"),
            "mutable": bool(collection_metadata.get("mutable", True)),
            "source_collections": list(collection_metadata.get("source_collections") or []),
            "summary": explicit_summary,
            "recommended_use_cases": recommended_use_cases,
            "recommended_queries": recommended_queries,
            "canonical_documents": canonical_payload,
            "key_topics": topic_terms[:8],
            "doc_count": len(ordered),
            "last_updated_at": max(profile.get("updated_at") for profile in ordered if profile.get("updated_at")) if ordered else None,
            "generated_from": [profile["doc_id"] for profile in canonical_docs],
        }

    def sync_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"action": "missing"}

        with self._sync_lock:
            relative = path.relative_to(self.settings.docs_root)
            if len(relative.parts) < 2:
                return {"action": "ignored", "path": str(path)}

            collection_id = normalize_collection_id(relative.parts[0])
            relative_doc_path = Path(*relative.parts[1:]).with_suffix("")
            relative_path = normalize_doc_path(relative_doc_path.as_posix())
            if not relative_path:
                return {"action": "ignored", "path": str(path)}
            slug = normalize_doc_slug(relative_doc_path.name)
            doc_id = collection_document_id(collection_id, relative_path)
            raw_text = path.read_text(encoding="utf-8")
            parsed = parse_document(raw_text, slug=slug, suffix=path.suffix.lower(), filename=path.name)
            content_hash = build_content_hash(raw_text)
            stat = path.stat()

            with self.connection() as conn:
                existing = self._doc_exists(conn, doc_id)
                if existing and str(existing["content_hash"]) == content_hash:
                    return {
                        "action": "unchanged",
                        "doc_id": doc_id,
                        "collection_id": collection_id,
                        "slug": slug,
                        "relative_path": relative_path,
                    }

                if existing:
                    self._delete_doc_rows(conn, doc_id)

                now = self._utc_now()
                conn.execute(
                    """
                    INSERT INTO documents (
                        doc_id, collection_id, relative_path, slug, title, source_path, content_type,
                        body, frontmatter_json, tags_json, content_hash, size_bytes,
                        created_at, updated_at, modified_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        collection_id,
                        relative_path,
                        slug,
                        parsed.title,
                        str(path),
                        parsed.content_type,
                        parsed.raw_text,
                        _json(parsed.frontmatter),
                        _json(parsed.tags),
                        content_hash,
                        stat.st_size,
                        str(existing["created_at"]) if existing else now,
                        now,
                        stat.st_mtime_ns,
                    ),
                )
                self._insert_chunks(
                    conn,
                    doc_id=doc_id,
                    collection_id=collection_id,
                    slug=relative_path,
                    title=parsed.title,
                    tags=parsed.tags,
                    parsed=parsed,
                )
                source_doc_ids = self._normalize_source_doc_ids(collection_id, parsed.frontmatter.get("kb_source_docs"))
                self._refresh_document_links(conn, doc_id=doc_id, source_doc_ids=source_doc_ids)

        return {
            "action": "updated" if existing else "created",
            "doc_id": doc_id,
            "collection_id": collection_id,
            "slug": slug,
            "relative_path": relative_path,
            "path": str(path),
        }

    def remove_document(self, collection_id: str, doc_path: str) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path)
        if not doc_path:
            raise ValueError("doc_path is required.")
        doc_id = collection_document_id(collection_id, doc_path)
        self._ensure_collection_writable(collection_id)
        with self._sync_lock:
            with self.connection() as conn:
                existing = self._doc_exists(conn, doc_id)
                if not existing:
                    return {"action": "missing", "doc_id": doc_id}
                self._delete_doc_rows(conn, doc_id)
        return {"action": "deleted", "doc_id": doc_id}

    def sync_from_disk(self, *, collection_id: str | None = None) -> dict[str, int]:
        self.initialize()
        docs_root = self.settings.docs_root
        docs_root.mkdir(parents=True, exist_ok=True)
        counts = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0, "ignored": 0}
        seen: set[str] = set()

        for path in sorted(docs_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
                continue
            relative = path.relative_to(docs_root)
            if len(relative.parts) < 2:
                counts["ignored"] += 1
                continue
            if collection_id and normalize_collection_id(relative.parts[0]) != normalize_collection_id(collection_id):
                continue
            result = self.sync_file(path)
            action = str(result.get("action") or "ignored")
            counts[action] = counts.get(action, 0) + 1
            if result.get("doc_id"):
                seen.add(str(result["doc_id"]))

        with self.connection() as conn:
            rows = conn.execute("SELECT doc_id, source_path FROM documents").fetchall()
            for row in rows:
                doc_id = str(row["doc_id"])
                source_path = Path(str(row["source_path"]))
                if collection_id and normalize_collection_id(doc_id.split("/", 1)[0]) != normalize_collection_id(collection_id):
                    continue
                if doc_id in seen:
                    continue
                if source_path.exists():
                    continue
                self._delete_doc_rows(conn, doc_id)
                counts["deleted"] += 1
        return counts

    def list_collections(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT collection_id, COUNT(*) AS document_count, MAX(updated_at) AS updated_at
                FROM documents
                GROUP BY collection_id
                ORDER BY collection_id
                """
            ).fetchall()
        document_counts = {str(row["collection_id"]): int(row["document_count"]) for row in rows}
        updated_at_map = {str(row["collection_id"]): str(row["updated_at"]) if row["updated_at"] else None for row in rows}
        collection_ids = sorted(set(document_counts) | set(self._collection_ids_on_disk()))
        collections = []
        for collection_id in collection_ids:
            guide = self.get_collection_guide(collection_id)
            collections.append(
                {
                    "collection_id": collection_id,
                    "document_count": int(document_counts.get(collection_id, 0)),
                    "updated_at": updated_at_map.get(collection_id),
                    "summary": str(guide.get("summary") or ""),
                    "key_topics": list(guide.get("key_topics") or []),
                    "kind": str(guide.get("kind") or "wiki"),
                    "mutable": bool(guide.get("mutable", True)),
                    "source_collections": list(guide.get("source_collections") or []),
                }
            )
        return collections

    def _document_list_filters(
        self,
        *,
        collection_id: str | None = None,
        query: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if collection_id:
            clauses.append("collection_id = ?")
            params.append(normalize_collection_id(collection_id))

        cleaned_query = str(query or "").strip().lower()
        if cleaned_query:
            needle = f"%{cleaned_query}%"
            clauses.append(
                "("
                "lower(doc_id) LIKE ? OR "
                "lower(collection_id) LIKE ? OR "
                "lower(relative_path) LIKE ? OR "
                "lower(source_path) LIKE ? OR "
                "lower(title) LIKE ? OR "
                "lower(body) LIKE ? OR "
                "lower(tags_json) LIKE ?"
                ")"
            )
            params.extend([needle] * 7)

        if not clauses:
            return "", []
        return f" WHERE {' AND '.join(clauses)}", params

    def count_documents(self, *, collection_id: str | None = None, query: str | None = None) -> int:
        where_clause, params = self._document_list_filters(collection_id=collection_id, query=query)
        with self.connection() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM documents{where_clause}", params).fetchone()
        return int(row["count"] if row else 0)

    def list_documents(
        self,
        *,
        collection_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self.settings.max_list_limit))
        offset = max(0, int(offset))
        where_clause, params = self._document_list_filters(collection_id=collection_id, query=query)
        sql = f"SELECT * FROM documents{where_clause} ORDER BY updated_at DESC, doc_id ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_document(row) for row in rows]

    def get_document_by_id(self, doc_id: str) -> dict[str, Any] | None:
        doc_id = str(doc_id or "").strip()
        if not doc_id:
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        return self._row_to_document(row, include_content=True)

    def get_document(self, collection_id: str, doc_path: str) -> dict[str, Any] | None:
        doc_id = collection_document_id(collection_id, doc_path)
        return self.get_document_by_id(doc_id)

    def get_collection_guide(self, collection_id: str) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM documents
                WHERE collection_id = ?
                ORDER BY updated_at DESC
                """,
                (collection_id,),
            ).fetchall()
        profiles = [self._document_profile(row, include_headings=True) for row in rows]
        return self._collection_guide(collection_id, profiles)

    def _fallback_search(self, collection_id: str | None, query: str, limit: int) -> list[dict[str, Any]]:
        query_terms = _fts_terms(query)
        needle = " ".join(query_terms) if query_terms else query.strip().lower()
        if not needle:
            return []
        query_text = """
            SELECT *
            FROM documents
            WHERE (lower(title) LIKE ? OR lower(body) LIKE ? OR lower(relative_path) LIKE ?)
        """
        params: list[Any] = [f"%{needle}%", f"%{needle}%", f"%{needle}%"]
        if collection_id:
            query_text += " AND collection_id = ?"
            params.append(normalize_collection_id(collection_id))
        query_text += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(query_text, params).fetchall()
        results = []
        for row in rows:
            doc = self._row_to_document(row)
            doc.update({"score": 0.0, "heading": "", "snippet": doc["excerpt"]})
            results.append(doc)
        return results

    def _search_boost(
        self,
        profile: dict[str, Any],
        query_terms: list[str],
        *,
        query_intent: str = "unknown",
        chunk_heading: str = "",
        chunk_content: str = "",
    ) -> float:
        boost = 0.0
        role = _inferred_doc_role(profile)
        if role in GUIDE_ROLE_WEIGHTS:
            boost += max(0.0, 0.35 - (GUIDE_ROLE_WEIGHTS[role] * 0.05))
        if profile.get("kb_canonical"):
            boost += 0.35
        boost += min(max(_coerce_int(profile.get("kb_priority"), 0), 0), 10) * 0.02
        title = str(profile.get("title") or "").lower()
        relative_path = str(profile.get("relative_path") or "").lower()
        summary = str(profile.get("summary") or "").lower()
        chunk_content_lower = str(chunk_content or "").lower()
        query_text = " ".join(query_terms)
        if query_text and query_text in title:
            boost += 0.6
        if query_text and query_text in relative_path:
            boost += 0.35
        if query_text and query_text in summary:
            boost += 0.25
        if query_terms and all(term in title for term in query_terms[: min(len(query_terms), 3)]):
            boost += 1.2
        if query_terms and all(term in relative_path for term in query_terms[: min(len(query_terms), 3)]):
            boost += 1.0
        heading = str(chunk_heading or "").lower()
        if query_text and query_text in heading:
            boost += 0.5
        if query_terms and all(term in heading for term in query_terms[: min(len(query_terms), 3)]):
            boost += 1.0
        elif query_terms and any(term in heading for term in query_terms[: min(len(query_terms), 3)]):
            boost += 0.1
        if query_terms and any(term in relative_path for term in query_terms):
            boost += 0.1
        if query_terms and any(term in chunk_content_lower for term in query_terms):
            boost += 0.08
        if query_intent == "definition":
            doc_text = " ".join([title, relative_path, role, summary])
            if any(hint in doc_text for hint in DEFINITION_DOC_HINTS):
                boost += 0.75
            if any(hint in doc_text for hint in ("overview", "about", "company", "introduction", "intro")):
                boost += 0.85
            if any(hint in doc_text for hint in ("readme", "demo question", "suggested question")):
                boost -= 0.25
            if query_terms and any(re.search(rf"\b{re.escape(term)}\b\s+(?:is|are|means|refers to)", chunk_content_lower) for term in query_terms):
                boost += 0.55
        elif query_intent == "policy":
            doc_text = " ".join([title, relative_path, role, summary])
            if any(hint in doc_text for hint in POLICY_DOC_HINTS):
                boost += 0.55
        elif query_intent == "procedure":
            doc_text = " ".join([title, relative_path, role, summary])
            if any(hint in doc_text for hint in PROCEDURE_DOC_HINTS):
                boost += 0.45
        elif query_intent == "fact":
            doc_text = " ".join([title, relative_path, role, summary])
            if any(hint in doc_text for hint in FACT_DOC_HINTS):
                boost += 0.25
        return boost

    def _search_rows(self, collection_filter: str | None, match_query: str, limit: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            sql = """
                SELECT
                    d.*,
                    c.heading AS chunk_heading,
                    c.content AS chunk_content,
                    bm25(chunks_fts) AS score,
                    snippet(chunks_fts, 6, '[', ']', ' ... ', 18) AS snippet
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.rowid
                JOIN documents d ON d.doc_id = c.doc_id
                WHERE chunks_fts MATCH ?
            """
            params: list[Any] = [match_query]
            if collection_filter:
                sql += " AND d.collection_id = ?"
                params.append(collection_filter)
            sql += " ORDER BY score ASC LIMIT ?"
            params.append(limit * 10)
            return conn.execute(sql, params).fetchall()

    def search(self, *, collection_id: str | None = None, query: str, limit: int = 5) -> list[dict[str, Any]]:
        collection_filter = normalize_collection_id(collection_id) if collection_id else None
        query = str(query or "").strip()
        if not query:
            return []

        limit = max(1, min(limit, self.settings.max_search_limit))
        query_terms = _fts_terms(query)
        query_intent = _classify_question(query)
        rows: list[sqlite3.Row] = []
        if query_terms:
            match_queries = [" AND ".join(f'"{term}"' for term in query_terms)]
            if len(query_terms) > 1:
                match_queries.append(" OR ".join(f'"{term}"' for term in query_terms))
            try:
                for match_query in match_queries:
                    rows = self._search_rows(collection_filter, match_query, limit)
                    if rows:
                        break
            except sqlite3.Error:
                return self._fallback_search(collection_filter, query, limit)
        if not rows:
            if query_terms:
                return []
            return self._fallback_search(collection_filter, query, limit)

        deduped: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = str(row["doc_id"])
            item = self._row_to_document(row)
            profile = self._document_profile(row)
            base_score = float(row["score"] or 0.0)
            adjusted_score = base_score - self._search_boost(
                profile,
                query_terms,
                query_intent=query_intent,
                chunk_heading=str(row["chunk_heading"] or ""),
                chunk_content=str(row["chunk_content"] or ""),
            )
            item.update(
                {
                    "heading": str(row["chunk_heading"] or ""),
                    "score": adjusted_score,
                    "snippet": _truncate_text(str(row["snippet"] or row["chunk_content"] or item["excerpt"]), 500),
                }
            )
            existing = deduped.get(doc_id)
            if existing and float(existing["score"]) <= adjusted_score:
                continue
            deduped[doc_id] = item

        ordered = sorted(deduped.values(), key=lambda item: float(item["score"]))
        return ordered[:limit]

    def answer_question(
        self,
        *,
        question: str,
        collection_id: str | None = None,
        max_documents: int = 3,
        max_chars_per_doc: int = 8000,
    ) -> dict[str, Any]:
        question_text = str(question or "").strip()
        normalized_collection = normalize_collection_id(collection_id) if collection_id else None
        answer_mode = _classify_question(question_text)
        if not question_text:
            return {
                "question": "",
                "collection_id": normalized_collection,
                "evidence": [],
                "result": "no_query",
                "answer_mode": "unknown",
                "answer_support": "unsupported",
                "suggested_answer": "",
                "answer_guidance": self.get_prompt("no_query_guidance", normalized_collection),
            }

        max_documents = max(1, min(int(max_documents), self.settings.max_search_limit))
        max_chars_per_doc = max(500, int(max_chars_per_doc))

        collection_fallback_from: str | None = None
        search_collection = normalized_collection
        hits = self.search(
            collection_id=normalized_collection,
            query=question_text,
            limit=max_documents,
        )
        if not hits and normalized_collection and self.count_documents(collection_id=normalized_collection) == 0:
            collection_fallback_from = normalized_collection
            search_collection = None
            hits = self.search(
                collection_id=None,
                query=question_text,
                limit=max_documents,
            )
        if not hits:
            return {
                "question": question_text,
                "collection_id": search_collection,
                "requested_collection_id": normalized_collection,
                "collection_fallback_from": collection_fallback_from,
                "evidence": [],
                "result": "no_matches",
                "answer_mode": answer_mode,
                "answer_support": "unsupported",
                "suggested_answer": "",
                "answer_guidance": self.get_prompt("no_match_guidance", search_collection),
            }

        evidence: list[dict[str, Any]] = []
        for hit in hits:
            doc_id = str(hit.get("doc_id") or "")
            document = self.get_document_by_id(doc_id) or {}
            raw_body = str(document.get("content") or "")
            _, body = split_document_frontmatter(raw_body)
            body = body.strip()
            truncated = False
            if len(body) > max_chars_per_doc:
                body = body[:max_chars_per_doc].rstrip() + "\n…[truncated]"
                truncated = True
            evidence.append(
                {
                    "doc_id": doc_id,
                    "collection_id": hit.get("collection_id"),
                    "title": hit.get("title"),
                    "source_label": hit.get("source_label") or _source_label(str(hit.get("title") or ""), str(hit.get("relative_path") or "")),
                    "relative_path": hit.get("relative_path"),
                    "matched_heading": hit.get("heading"),
                    "snippet": _truncate_text(str(hit.get("snippet") or ""), 500),
                    "score": hit.get("score"),
                    "kb_role": hit.get("kb_role"),
                    "content": body,
                    "content_truncated": truncated,
                }
            )

        suggested_answer, answer_support = _suggest_answer(question_text, answer_mode, evidence)
        return {
            "question": question_text,
            "collection_id": search_collection,
            "requested_collection_id": normalized_collection,
            "collection_fallback_from": collection_fallback_from,
            "evidence": evidence,
            "result": "ok",
            "answer_mode": answer_mode,
            "answer_support": answer_support,
            "suggested_answer": suggested_answer,
            "answer_guidance": self.get_prompt("match_guidance", search_collection),
        }

    def upsert_from_path(self, path: Path) -> dict[str, Any]:
        result = self.sync_file(path)
        if result.get("doc_id") and result.get("action") in {"created", "updated"}:
            document = self.get_document(result["collection_id"], result["relative_path"])
            if document:
                return {"result": result, "document": document}
        return {"result": result}

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def resolve_path(self, collection_id: str, doc_path: str) -> Path:
        existing = existing_document_path(self.settings.docs_root, collection_id, doc_path)
        if existing:
            return existing
        return document_path(self.settings.docs_root, collection_id, doc_path, ".md")

    def write_document(
        self,
        *,
        collection_id: str,
        doc_path: str,
        raw_text: str,
        filename: str | None = None,
        content_type: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        frontmatter_patch: dict[str, Any] | None = None,
        kb_origin: str = "agent",
        kb_source_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path)
        if not doc_path:
            raise ValueError("doc_path is required.")
        self._ensure_collection_writable(collection_id)
        suffix = file_suffix_for_text(filename, content_type)
        path = document_path(self.settings.docs_root, collection_id, doc_path, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        extra_frontmatter: dict[str, Any] = {}
        if frontmatter_patch:
            extra_frontmatter.update({key: value for key, value in frontmatter_patch.items() if value is not None})
        if kb_source_docs is not None:
            extra_frontmatter["kb_source_docs"] = self._normalize_source_doc_ids(collection_id, kb_source_docs)
        extra_frontmatter["kb_origin"] = kb_origin
        extra_frontmatter["kb_updated_at"] = self._utc_now()
        stored_text = merge_document_frontmatter(raw_text, title=title, tags=tags, extra=extra_frontmatter)
        path.write_text(stored_text, encoding="utf-8")
        return self.upsert_from_path(path)

    def create_document(
        self,
        *,
        collection_id: str,
        doc_path: str,
        raw_text: str,
        filename: str | None = None,
        content_type: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        frontmatter_patch: dict[str, Any] | None = None,
        kb_source_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path)
        if not doc_path:
            raise ValueError("doc_path is required.")
        if self.get_document(collection_id, doc_path):
            raise ValueError(f"Document '{collection_id}/{doc_path}' already exists.")
        return self.write_document(
            collection_id=collection_id,
            doc_path=doc_path,
            raw_text=raw_text,
            filename=filename,
            content_type=content_type,
            title=title,
            tags=tags,
            frontmatter_patch=frontmatter_patch,
            kb_origin="admin",
            kb_source_docs=kb_source_docs,
        )

    def update_document(
        self,
        *,
        collection_id: str,
        doc_path: str,
        raw_text: str,
        filename: str | None = None,
        content_type: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        frontmatter_patch: dict[str, Any] | None = None,
        kb_source_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path)
        if not doc_path:
            raise ValueError("doc_path is required.")
        if not self.get_document(collection_id, doc_path):
            raise ValueError(f"Document '{collection_id}/{doc_path}' does not exist.")
        return self.write_document(
            collection_id=collection_id,
            doc_path=doc_path,
            raw_text=raw_text,
            filename=filename,
            content_type=content_type,
            title=title,
            tags=tags,
            frontmatter_patch=frontmatter_patch,
            kb_origin="admin",
            kb_source_docs=kb_source_docs,
        )

    def append_section(
        self,
        *,
        collection_id: str,
        doc_path: str,
        heading: str,
        content: str,
        frontmatter_patch: dict[str, Any] | None = None,
        kb_source_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path)
        if not doc_path:
            raise ValueError("doc_path is required.")
        existing = self.get_document(collection_id, doc_path)
        section_heading = str(heading or "").strip()
        section_content = str(content or "").strip()
        section_parts = []
        if section_heading:
            section_parts.append(f"## {section_heading}")
        if section_content:
            section_parts.append(section_content)
        section_text = "\n\n".join(section_parts).strip()
        if not section_text:
            raise ValueError("append_section requires heading or content.")

        if existing:
            metadata, body = split_document_frontmatter(existing["content"])
            body = body.rstrip()
            new_body = f"{body}\n\n{section_text}".strip() if body else section_text
            raw_text = render_document_frontmatter(metadata, new_body) if metadata else new_body
            return self.write_document(
                collection_id=collection_id,
                doc_path=doc_path,
                raw_text=raw_text,
                filename=Path(existing["path"]).name if existing.get("path") else None,
                content_type=str(existing.get("content_type") or None),
                title=str(existing.get("title") or "") or None,
                tags=list(existing.get("tags") or []) or None,
                frontmatter_patch=frontmatter_patch,
                kb_origin="admin",
                kb_source_docs=kb_source_docs,
            )

        return self.create_document(
            collection_id=collection_id,
            doc_path=doc_path,
            raw_text=section_text,
            title=section_heading or None,
            frontmatter_patch=frontmatter_patch,
            kb_source_docs=kb_source_docs,
        )

    def update_document_metadata(
        self,
        *,
        collection_id: str,
        doc_path: str,
        title: str | None = None,
        tags: list[str] | None = None,
        frontmatter_patch: dict[str, Any] | None = None,
        kb_source_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path)
        if not doc_path:
            raise ValueError("doc_path is required.")
        existing = self.get_document(collection_id, doc_path)
        if not existing:
            raise ValueError(f"Document '{collection_id}/{doc_path}' does not exist.")
        return self.write_document(
            collection_id=collection_id,
            doc_path=doc_path,
            raw_text=existing["content"],
            filename=Path(existing["path"]).name if existing.get("path") else None,
            content_type=str(existing.get("content_type") or None),
            title=title,
            tags=tags,
            frontmatter_patch=frontmatter_patch,
            kb_origin="admin",
            kb_source_docs=kb_source_docs if kb_source_docs is not None else list(existing.get("kb_source_docs") or []),
        )

    def update_collection_metadata(self, collection_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        current = self._collection_metadata(collection_id)
        merged = {**current, **{key: value for key, value in (metadata or {}).items() if value is not None}}
        result = self._update_collection_manifest(collection_id, merged)
        return result

    def list_proposed_changes(self, *, collection_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM proposed_changes"
        params: list[Any] = []
        conditions: list[str] = []
        if collection_id:
            conditions.append("collection_id = ?")
            params.append(normalize_collection_id(collection_id))
        if status:
            conditions.append("status = ?")
            params.append(str(status))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            payload = _parse_json(row["payload_json"], {})
            results.append(
                {
                    "change_id": str(row["change_id"]),
                    "collection_id": str(row["collection_id"]),
                    "doc_id": str(row["doc_id"]),
                    "doc_path": str(row["doc_path"]),
                    "operation": str(row["operation"]),
                    "status": str(row["status"]),
                    "payload": payload,
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return results

    def propose_document_change(
        self,
        *,
        collection_id: str,
        operation: str,
        doc_path: str | None = None,
        raw_text: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        frontmatter_patch: dict[str, Any] | None = None,
        kb_source_docs: list[str] | None = None,
        heading: str | None = None,
        content: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        if not collection_id:
            raise ValueError("collection_id is required.")
        doc_path = normalize_doc_path(doc_path or "")
        if not doc_path:
            raise ValueError("doc_path is required.")
        change_id = uuid4().hex
        payload = {
            "collection_id": collection_id,
            "doc_path": doc_path,
            "operation": str(operation or "").strip().lower(),
            "raw_text": raw_text,
            "title": title,
            "tags": tags or [],
            "frontmatter_patch": frontmatter_patch or {},
            "kb_source_docs": kb_source_docs or [],
            "heading": heading,
            "content": content,
            "filename": filename,
            "content_type": content_type,
        }
        now = self._utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO proposed_changes (
                    change_id, collection_id, doc_id, doc_path, operation, payload_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    change_id,
                    collection_id,
                    collection_document_id(collection_id, doc_path) if doc_path else "",
                    doc_path,
                    payload["operation"],
                    _json(payload),
                    now,
                    now,
                ),
            )
        return {
            "change_id": change_id,
            "collection_id": collection_id,
            "doc_id": collection_document_id(collection_id, doc_path) if doc_path else "",
            "doc_path": doc_path,
            "operation": payload["operation"],
            "status": "pending",
            "payload": payload,
            "created_at": now,
            "updated_at": now,
        }

    def _apply_proposed_change_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation") or "").strip().lower()
        collection_id = normalize_collection_id(str(payload.get("collection_id") or ""))
        doc_path = normalize_doc_path(str(payload.get("doc_path") or ""))
        if not collection_id or not operation:
            raise ValueError("Proposed change is missing collection_id or operation.")

        if operation in {"create", "update", "replace"}:
            raw_text = payload.get("raw_text")
            if raw_text is None and operation != "create":
                raw_text = payload.get("content")
            if raw_text is None:
                raw_text = ""
            if operation == "create":
                result = self.create_document(
                    collection_id=collection_id,
                    doc_path=doc_path,
                    raw_text=str(raw_text),
                    filename=payload.get("filename"),
                    content_type=payload.get("content_type"),
                    title=payload.get("title"),
                    tags=list(payload.get("tags") or []) or None,
                    frontmatter_patch=dict(payload.get("frontmatter_patch") or {}),
                    kb_source_docs=list(payload.get("kb_source_docs") or []) or None,
                )
            else:
                result = self.update_document(
                    collection_id=collection_id,
                    doc_path=doc_path,
                    raw_text=str(raw_text),
                    filename=payload.get("filename"),
                    content_type=payload.get("content_type"),
                    title=payload.get("title"),
                    tags=list(payload.get("tags") or []) or None,
                    frontmatter_patch=dict(payload.get("frontmatter_patch") or {}),
                    kb_source_docs=list(payload.get("kb_source_docs") or []) or None,
                )
            return {"action": operation, "document": result.get("document") if "document" in result else self.get_document(collection_id, doc_path)}

        if operation == "append":
            result = self.append_section(
                collection_id=collection_id,
                doc_path=doc_path,
                heading=str(payload.get("heading") or ""),
                content=str(payload.get("content") or ""),
                frontmatter_patch=dict(payload.get("frontmatter_patch") or {}),
                kb_source_docs=list(payload.get("kb_source_docs") or []) or None,
            )
            return {"action": operation, "document": self.get_document(collection_id, doc_path) or result.get("document")}

        if operation == "delete":
            self._ensure_collection_writable(collection_id)
            path = self.resolve_path(collection_id, doc_path)
            if path.exists():
                path.unlink()
            result = self.remove_document(collection_id, doc_path)
            return {"action": "deleted", "result": result}

        if operation == "metadata":
            result = self.update_document_metadata(
                collection_id=collection_id,
                doc_path=doc_path,
                title=payload.get("title"),
                tags=list(payload.get("tags") or []) or None,
                frontmatter_patch=dict(payload.get("frontmatter_patch") or {}),
                kb_source_docs=list(payload.get("kb_source_docs") or []) or None,
            )
            return {"action": operation, "document": result.get("document") if "document" in result else self.get_document(collection_id, doc_path)}

        raise ValueError(f"Unsupported proposed change operation '{operation}'.")

    def apply_proposed_change(self, change_id: str) -> dict[str, Any]:
        change_id = str(change_id or "").strip()
        if not change_id:
            raise ValueError("change_id is required.")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM proposed_changes WHERE change_id = ?", (change_id,)).fetchone()
            if not row:
                return {"action": "missing", "change_id": change_id}
            if str(row["status"]) == "applied":
                payload = _parse_json(row["payload_json"], {})
                document = self.get_document(str(row["collection_id"]), str(payload.get("doc_path") or row["doc_path"]))
                return {"action": "applied", "change_id": change_id, "document": document, "status": "applied"}
            payload = _parse_json(row["payload_json"], {})
            result = self._apply_proposed_change_payload(payload)
            now = self._utc_now()
            conn.execute(
                "UPDATE proposed_changes SET status = 'applied', updated_at = ? WHERE change_id = ?",
                (now, change_id),
            )
        return {"action": "applied", "change_id": change_id, "status": "applied", "result": result}

    def discard_proposed_change(self, change_id: str) -> dict[str, Any]:
        change_id = str(change_id or "").strip()
        if not change_id:
            raise ValueError("change_id is required.")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM proposed_changes WHERE change_id = ?", (change_id,)).fetchone()
            if not row:
                return {"action": "missing", "change_id": change_id}
            now = self._utc_now()
            conn.execute(
                "UPDATE proposed_changes SET status = 'discarded', updated_at = ? WHERE change_id = ?",
                (now, change_id),
            )
        return {"action": "discarded", "change_id": change_id, "status": "discarded"}

    def lint_collection(self, collection_id: str) -> dict[str, Any]:
        collection_id = normalize_collection_id(collection_id)
        documents = self.list_documents(collection_id=collection_id, limit=self.settings.max_list_limit)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM documents WHERE collection_id = ?", (collection_id,)).fetchall()
        doc_ids = {str(row["doc_id"]) for row in rows}
        backlinks_by_doc = {doc_id: self._document_backlinks(doc_id) for doc_id in doc_ids}
        findings: list[dict[str, Any]] = []
        for row in rows:
            frontmatter = self._document_frontmatter(row)
            doc_id = str(row["doc_id"])
            source_doc_ids = self._normalize_source_doc_ids(collection_id, frontmatter.get("kb_source_docs"))
            if not source_doc_ids and not backlinks_by_doc.get(doc_id):
                findings.append(
                    {
                        "severity": "info",
                        "code": "orphan_document",
                        "doc_id": doc_id,
                        "message": "Document has no source links and no backlinks yet.",
                    }
                )
            missing_sources = [source_doc_id for source_doc_id in source_doc_ids if source_doc_id not in doc_ids and self.get_document_by_id(source_doc_id) is None]
            if missing_sources:
                findings.append(
                    {
                        "severity": "warning",
                        "code": "missing_source_document",
                        "doc_id": doc_id,
                        "missing": missing_sources,
                        "message": "Document references source documents that could not be found.",
                    }
                )
        return {
            "collection_id": collection_id,
            "document_count": len(documents),
            "findings": findings,
            "summary": {
                "info": sum(1 for finding in findings if finding["severity"] == "info"),
                "warning": sum(1 for finding in findings if finding["severity"] == "warning"),
                "error": sum(1 for finding in findings if finding["severity"] == "error"),
            },
        }
