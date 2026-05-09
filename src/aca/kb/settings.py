from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class KBSettings:
    server_name: str
    server_title: str
    server_description: str
    server_version: str
    public_base_url: str
    port: int
    docs_root: Path
    index_root: Path
    admin_api_key_file: Path
    admin_api_key: str
    reconcile_interval_seconds: float
    default_search_limit: int
    max_search_limit: int
    max_list_limit: int
    max_upload_bytes: int
    answer_default_documents: int = 3
    answer_max_documents: int = 5
    answer_max_chars_per_doc: int = 8000
    protocol_version: str = "2025-06-18"
    prompts_seed_file: Path | None = None

    @property
    def index_db_path(self) -> Path:
        return self.index_root / "kb.sqlite3"

    def admin_api_key_value(self) -> str:
        try:
            file_exists = self.admin_api_key_file.exists()
        except OSError:
            file_exists = False
        if file_exists:
            try:
                return self.admin_api_key_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        return self.admin_api_key.strip()


def _env_optional_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


@lru_cache(maxsize=1)
def get_settings() -> KBSettings:
    port = _env_int("KB_PORT", 39736)
    public_base_url = os.environ.get("KB_PUBLIC_BASE_URL", "").strip() or f"http://127.0.0.1:{port}/mcp"
    return KBSettings(
        server_name=os.environ.get("KB_SERVER_NAME", "ac.tandem/kb-mcp"),
        server_title=os.environ.get("KB_SERVER_TITLE", "Tandem Knowledgebase MCP"),
        server_description=os.environ.get(
            "KB_SERVER_DESCRIPTION",
            "Local MCP server for private business knowledgebase retrieval and admin uploads.",
        ),
        server_version=os.environ.get("KB_SERVER_VERSION", "0.1.0"),
        public_base_url=public_base_url,
        port=port,
        docs_root=_env_path("KB_DOCS_ROOT", "./kb-data/docs").resolve(),
        index_root=_env_path("KB_INDEX_ROOT", "./kb-data/index").resolve(),
        admin_api_key_file=_env_path("KB_ADMIN_API_KEY_FILE", "./secrets/kb_admin_api_key").resolve(),
        admin_api_key=os.environ.get("KB_ADMIN_API_KEY", ""),
        reconcile_interval_seconds=_env_float("KB_RECONCILE_INTERVAL_SECONDS", 10.0),
        default_search_limit=_env_int("KB_DEFAULT_SEARCH_LIMIT", 5),
        max_search_limit=_env_int("KB_MAX_SEARCH_LIMIT", 20),
        max_list_limit=_env_int("KB_MAX_LIST_LIMIT", 200),
        max_upload_bytes=_env_int("KB_MAX_UPLOAD_BYTES", 5 * 1024 * 1024),
        answer_default_documents=_env_int("KB_ANSWER_DEFAULT_DOCUMENTS", 3),
        answer_max_documents=_env_int("KB_ANSWER_MAX_DOCUMENTS", 5),
        answer_max_chars_per_doc=_env_int("KB_ANSWER_MAX_CHARS_PER_DOC", 8000),
        prompts_seed_file=_env_optional_path("KB_PROMPTS_FILE"),
    )

