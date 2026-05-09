from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from src.tandem_agents.utils.utils import atomic_write_json, now_ms

_STORE_ROOT: Path | None = None
_STORE_LOCK = threading.Lock()
_RUN_LOCKS: dict[str, threading.Lock] = {}


def configure_artifact_store_root(root: Path) -> None:
    global _STORE_ROOT
    _STORE_ROOT = root.expanduser().resolve()
    _STORE_ROOT.mkdir(parents=True, exist_ok=True)


def artifact_store_root() -> Path:
    if _STORE_ROOT is None:
        default_root = Path.cwd() / "artifact-store"
        default_root.mkdir(parents=True, exist_ok=True)
        return default_root.resolve()
    _STORE_ROOT.mkdir(parents=True, exist_ok=True)
    return _STORE_ROOT


def _run_lock(run_id: str) -> threading.Lock:
    with _STORE_LOCK:
        lock = _RUN_LOCKS.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _RUN_LOCKS[run_id] = lock
        return lock


def _manifest_path(run_id: str) -> Path:
    return artifact_store_root() / "runs" / run_id / "manifest.json"


def _blob_path(sha256_hex: str) -> Path:
    digest = sha256_hex.strip().lower()
    return artifact_store_root() / "objects" / digest[:2] / digest[2:]


def _load_manifest(run_id: str) -> dict[str, Any]:
    path = _manifest_path(run_id)
    if not path.exists():
        return {"run_id": run_id, "updated_at_ms": now_ms(), "entries": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            loaded.setdefault("run_id", run_id)
            loaded.setdefault("entries", [])
            loaded.setdefault("updated_at_ms", now_ms())
            return loaded
    except Exception:
        pass
    return {"run_id": run_id, "updated_at_ms": now_ms(), "entries": []}


def _save_manifest(run_id: str, manifest: dict[str, Any]) -> None:
    path = _manifest_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, manifest)


def mirror_run_file(run_id: str, source_path: Path, logical_path: str) -> dict[str, Any] | None:
    source_path = Path(source_path)
    if not source_path.exists() or not source_path.is_file():
        return None
    logical = str(logical_path or "").strip()
    if not logical:
        logical = source_path.name
    data = source_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    blob = _blob_path(digest)
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists():
        blob.write_bytes(data)
    entry = {
        "path": logical,
        "sha256": digest,
        "size": len(data),
        "source_path": str(source_path),
        "object_path": str(blob),
        "updated_at_ms": now_ms(),
    }
    lock = _run_lock(run_id)
    with lock:
        manifest = _load_manifest(run_id)
        entries = [item for item in manifest.get("entries", []) if str(item.get("path") or "") != logical]
        entries.append(entry)
        manifest["entries"] = sorted(entries, key=lambda item: str(item.get("path") or ""))
        manifest["updated_at_ms"] = now_ms()
        _save_manifest(run_id, manifest)
    return entry


def mirror_run_tree(run_id: str, source_root: Path, *, logical_prefix: str = "") -> list[dict[str, Any]]:
    source_root = Path(source_root)
    if not source_root.exists():
        return []
    entries: list[dict[str, Any]] = []
    prefix = str(logical_prefix or "").strip().strip("/")
    for item in sorted(source_root.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(source_root).as_posix()
        logical = f"{prefix}/{rel}" if prefix else rel
        entry = mirror_run_file(run_id, item, logical)
        if entry is not None:
            entries.append(entry)
    return entries


def restore_run_tree(run_id: str, destination_root: Path) -> list[str]:
    manifest = _load_manifest(run_id)
    restored: list[str] = []
    destination_root = Path(destination_root)
    for entry in manifest.get("entries", []):
        logical_path = str(entry.get("path") or "").strip()
        object_path = Path(str(entry.get("object_path") or "")).expanduser()
        if not logical_path or not object_path.exists():
            continue
        target = destination_root / logical_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(object_path.read_bytes())
        restored.append(logical_path)
    return restored

