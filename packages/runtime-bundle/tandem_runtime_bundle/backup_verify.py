"""Read-only, fail-closed verification of a v3 encrypted recovery candidate.

This does not extract, rebind storage, start services, or prove audit semantics.
"""

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tarfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .backup_archive import CHUNK_SIZE, canonical_json, read_file
from .backup_decrypt import DecryptingReader
from .backup_source import (CONFIG_FILES, REQUIRED_HOST_ROOTS,
                            REQUIRED_ORDINARY_ROOTS, _unique_object, _uuid)
from .contract import validate_keyring


_HEX = re.compile(r"[a-f0-9]{64}\Z")
_SMALL = 8 * 1024 * 1024
_CONTROL = {
    "config-3/release-manifest.json", "config-4/runtime-security.json",
    "host-security/.initialized", "host-security/storage-roots.json",
    "host-security/audit-hmac-key", "host-security/context-keyring.json",
    "host-policy/current.json", "host-replay/.runtime-security-v3-root",
    "host-anchor/.runtime-security-v3-root",
}
_METADATA_SQL = ("CREATE TABLE replay_metadata (singleton INTEGER PRIMARY KEY "
                 "CHECK (singleton = 1), version INTEGER NOT NULL)")
_ENTRIES_SQL = ("CREATE TABLE replay_entries (replay_key TEXT PRIMARY KEY "
                "CHECK (length(replay_key) = 64), namespace_hash TEXT NOT NULL "
                "CHECK (length(namespace_hash) = 64), fingerprint_hex TEXT NOT NULL "
                "CHECK (length(fingerprint_hex) = 64), expires_at_ms INTEGER NOT NULL "
                "CHECK (expires_at_ms >= 0))")
_INDEX_SQL = "CREATE INDEX replay_entries_namespace_idx ON replay_entries(namespace_hash)"


def _json(data, label, *, canonical=False):
    if len(data) > _SMALL:
        raise ValueError(f"{label} exceeds the verification limit")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{label} is invalid JSON") from None
    if canonical and canonical_json(value) != data:
        raise ValueError(f"{label} is not canonical JSON")
    return value


def _safe_name(name):
    if (not isinstance(name, str) or not name or name.startswith("/")
            or "\\" in name or "\x00" in name or "//" in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            or str(PurePosixPath(name)) != name):
        raise ValueError("backup inventory contains an unsafe archive path")
    return name


def _manifest(path, receipt, kms, scope):
    source = Path(path)
    if source.lstat().st_size > _SMALL:
        raise ValueError("backup manifest exceeds the verification limit")
    payload = read_file(source)
    if (len(payload) != receipt["manifest_size"]
            or hashlib.sha256(payload).hexdigest() != receipt["manifest_sha256"]):
        raise ValueError("backup manifest does not match independent receipt")
    commit = _json(payload, "backup manifest", canonical=True)
    if not isinstance(commit, dict) or set(commit) != {
            "format", "scope", "objects", "backup_kms",
            "manifest_nonce_base64", "manifest_ciphertext_base64"}:
        raise ValueError("backup manifest fields are invalid")
    outer = {key: value for key, value in commit.items()
             if key not in ("manifest_nonce_base64", "manifest_ciphertext_base64")}
    if type(outer["format"]) is not int or outer["format"] != 1 or outer["scope"] != scope:
        raise ValueError("backup manifest scope or format is invalid")
    key = outer["backup_kms"]
    if (not isinstance(key, dict) or set(key) != {
            "key_id", "key_version", "wrapped_dek_base64"}
            or key["key_id"] != kms.key_id or key["key_version"] != kms.key_version):
        raise ValueError("backup KMS reference differs from operator authority")
    objects = outer["objects"]
    if not isinstance(objects, dict) or set(objects) != {"archive", "anchors"}:
        raise ValueError("backup manifest object set is incomplete")
    prefix = "v3/{organization_id}/{deployment_id}/{backup_id}/".format(**scope)
    for name in ("archive", "anchors"):
        item = objects[name]
        if (not isinstance(item, dict) or set(item) != {
                "object_key", "sha256", "size", "chunks", "nonce_prefix"}
                or item["object_key"] != prefix + name + ".aead"
                or not isinstance(item["sha256"], str) or not _HEX.fullmatch(item["sha256"])
                or item["sha256"] != receipt[name + "_sha256"]
                or type(item["size"]) is not int or item["size"] <= 0
                or type(item["chunks"]) is not int or not 0 < item["chunks"] < 2**32):
            raise ValueError("backup ciphertext reference differs from independent receipt")
    try:
        nonce = base64.b64decode(commit["manifest_nonce_base64"], validate=True)
        ciphertext = base64.b64decode(commit["manifest_ciphertext_base64"], validate=True)
    except (TypeError, ValueError, base64.binascii.Error):
        raise ValueError("backup manifest encryption is invalid") from None
    if len(nonce) != 12 or nonce[:1] != b"\x02" or len(ciphertext) > _SMALL:
        raise ValueError("backup manifest nonce or ciphertext is invalid")
    dek = kms.unwrap(key["wrapped_dek_base64"], scope)
    inner = _json(AESGCM(dek).decrypt(nonce, ciphertext, canonical_json(outer)),
                  "sealed backup inventory", canonical=True)
    if not isinstance(inner, dict) or type(inner.get("format")) is not int or inner["format"] != 1 or inner.get("scope") != scope:
        raise ValueError("sealed backup inventory has the wrong scope")
    return inner, objects, dek


def _inventory(inner):
    entries = inner.get("inventory")
    roots = inner.get("roots")
    expected_roots = {f"host-{name}" for name in REQUIRED_HOST_ROOTS}
    expected_roots |= {f"ordinary-{name.lower()}" for name in REQUIRED_ORDINARY_ROOTS}
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 200000
            or not isinstance(roots, dict) or set(roots) != expected_roots):
        raise ValueError("sealed backup inventory is incomplete")
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("sealed backup inventory contains a non-object")
        name = _safe_name(entry.get("archive_path"))
        if (entry.get("type") not in ("file", "directory")
                or type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777
                or any(type(entry.get(field)) is not int or entry[field] < 0
                       for field in ("uid", "gid", "device", "inode"))
                or not isinstance(entry.get("source_path"), str)
                or not (entry["source_path"].startswith("/")
                        or (os.name != "posix" and Path(entry["source_path"]).is_absolute()))):
            raise ValueError("sealed backup inventory metadata is invalid")
        if entry["type"] == "file" and (
                type(entry.get("size")) is not int or entry["size"] < 0
                or not isinstance(entry.get("sha256"), str)
                or not _HEX.fullmatch(entry["sha256"])):
            raise ValueError("sealed backup file metadata is invalid")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("sealed backup inventory has duplicate paths")
    by_name = {entry["archive_path"]: entry for entry in entries}
    for group in expected_roots:
        if roots[group] != by_name.get(group) or roots[group]["type"] != "directory":
            raise ValueError("sealed backup root identity is incomplete")
    for index, suffix in enumerate(CONFIG_FILES):
        if f"config-{index}/{Path(suffix).name}" not in by_name:
            raise ValueError("sealed backup installation configuration is incomplete")
    if "host-replay/assertions.sqlite3" not in by_name:
        raise ValueError("durable replay database is missing")
    if "host-replay/assertions.sqlite3-wal" in by_name:
        raise ValueError("pinned replay engine must not have an uncheckpointed WAL")
    if (not any(name.startswith("host-anchor/") for name in names)
            or any(name not in by_name for name in _CONTROL)):
        raise ValueError("backup security control files are incomplete")
    return entries, by_name


def _tar_object(path, metadata, dek, scope, kind, entries):
    observed = {}
    captured = {}
    replay_image = None
    with DecryptingReader(path, metadata, dek,
                          {**scope, "format": 1, "kind": kind},
                          b"\x00" if kind == "archive" else b"\x01") as reader:
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            for index, member in enumerate(archive):
                if index >= len(entries):
                    raise ValueError("backup tar contains an extra member")
                expected = entries[index]
                name = _safe_name(member.name)
                if (name != expected["archive_path"] or member.uid != expected["uid"]
                        or member.gid != expected["gid"] or member.mode != expected["mode"]
                        or member.linkname or member.mtime != 0):
                    raise ValueError("backup tar member differs from sealed inventory")
                if expected["type"] == "directory":
                    if not member.isdir() or member.size != 0:
                        raise ValueError("backup tar contains an unsafe directory")
                else:
                    if not member.isfile() or member.size != expected["size"]:
                        raise ValueError("backup tar contains an unsafe file")
                    digest = hashlib.sha256()
                    small = bytearray() if name in _CONTROL else None
                    if kind == "archive" and name == "host-replay/assertions.sqlite3":
                        if member.size > 128 * 1024 * 1024:
                            raise ValueError("replay database exceeds in-memory preflight limit")
                        replay_image = bytearray()
                    source = archive.extractfile(member)
                    while block := source.read(CHUNK_SIZE):
                        digest.update(block)
                        if small is not None:
                            small.extend(block)
                            if len(small) > _SMALL:
                                raise ValueError("backup control file exceeds limit")
                        if kind == "archive" and name == "host-replay/assertions.sqlite3":
                            replay_image.extend(block)
                    if digest.hexdigest() != expected["sha256"]:
                        raise ValueError("backup tar file differs from sealed inventory")
                    if small is not None:
                        captured[name] = bytes(small)
                observed[name] = expected
        reader.finish()
    if list(observed) != [entry["archive_path"] for entry in entries]:
        raise ValueError("backup tar is missing a sealed inventory member")
    return captured, bytes(replay_image) if replay_image is not None else None


def _replay(image):
    if not image or image[:16] != b"SQLite format 3\x00":
        raise ValueError("durable replay database is invalid")
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.deserialize(image)
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("durable replay database failed integrity check")
            rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name").fetchall()
            if rows != [
                    ("index", "replay_entries_namespace_idx", "replay_entries", _INDEX_SQL),
                    ("table", "replay_entries", "replay_entries", _ENTRIES_SQL),
                    ("table", "replay_metadata", "replay_metadata", _METADATA_SQL)]:
                raise ValueError("durable replay schema differs from pinned engine")
            if (connection.execute("SELECT version FROM replay_metadata WHERE singleton=1")
                    .fetchone() != (1,)
                    or connection.execute("SELECT COUNT(*) FROM replay_metadata").fetchone() != (1,)):
                raise ValueError("durable replay version is invalid")
            count = connection.execute("SELECT COUNT(*) FROM replay_entries").fetchone()[0]
            invalid = connection.execute(
                "SELECT COUNT(*) FROM replay_entries WHERE length(replay_key)!=64 "
                "OR length(namespace_hash)!=64 OR length(fingerprint_hex)!=64 "
                "OR expires_at_ms<0").fetchone()[0]
            if invalid or count > 100000:
                raise ValueError("durable replay rows are invalid")
            return count
    except sqlite3.DatabaseError:
        raise ValueError("durable replay database cannot be read") from None


def _controls(inner, captured, by_name, scope):
    def parsed(name):
        return _json(captured[name], name)

    bundle = parsed("config-4/runtime-security.json")
    release = parsed("config-3/release-manifest.json")
    policy = parsed("host-policy/current.json")
    binding = parsed("host-security/storage-roots.json")
    identity = inner.get("storage_identity")
    if (not isinstance(bundle, dict) or bundle.get("schema_version") != 3
            or bundle.get("profile") != "hosted-single-node-v3"
            or bundle.get("organization_id") != scope["organization_id"]
            or bundle.get("deployment_id") != scope["deployment_id"]
            or bundle.get("engine_provenance") != inner.get("engine_provenance")
            or bundle.get("images", {}).get("engine") != inner.get("engine_image")
            or release.get("runtime_security_version") != 3
            or release.get("engine_provenance") != inner.get("engine_provenance")
            or release.get("engine_image") != inner.get("engine_image")
            or policy.get("organization_id") != scope["organization_id"]
            or policy.get("deployment_id") != scope["deployment_id"]
            or policy.get("policy_version") != inner.get("policy_version")
            or captured["host-security/.initialized"] != b"runtime-security-v3\n"
            or binding != identity or not isinstance(identity, dict)
            or set(identity) != {"state", "data", "replay", "anchor"}
            or inner.get("policy_sha256") != by_name["host-policy/current.json"]["sha256"]
            or inner.get("audit_key_id") != bundle.get("engine_environment", {}).get(
                "TANDEM_AUDIT_HMAC_KEY_ID")
            or len(captured["host-security/audit-hmac-key"].strip()) < 32):
        raise ValueError("recovery controls differ from sealed scope or storage identity")
    for key, group in (("state", "host-state"), ("data", "ordinary-data"),
                       ("replay", "host-replay"), ("anchor", "host-anchor")):
        record = inner["roots"][group]
        prior = identity[key]
        if (not isinstance(prior, dict)
                or any(prior.get(field) != record[field] for field in (
                    "path", "device", "inode") if field in record)
                or prior.get("path") != record["source_path"]):
            raise ValueError("recovery storage binding differs from sealed root inventory")
        if key in ("replay", "anchor") and (
                not isinstance(prior.get("sentinel"), str)
                or captured[f"host-{key}/.runtime-security-v3-root"] !=
                prior["sentinel"].encode("ascii")):
            raise ValueError("recovery history sentinel differs from storage binding")
    validate_keyring(parsed("host-security/context-keyring.json"),
                     scope["deployment_id"], scope["organization_id"])
    memory = inner.get("memory_kms")
    fields = {"provider", "runtime_principal_id", "kek_id", "kek_version",
              "rotation_epoch"}
    bundle_memory = bundle.get("memory_encryption")
    if (not isinstance(memory, dict) or set(memory) != fields
            or not isinstance(bundle_memory, dict)
            or memory.get("provider") != "google_cloud_kms"
            or any(not isinstance(memory.get(field), str) or not memory[field]
                   for field in ("runtime_principal_id", "kek_id", "kek_version"))
            or type(memory.get("rotation_epoch")) is not int
            or not 0 <= memory["rotation_epoch"] < 2**64
            or any(bundle_memory.get(field) != value
                   for field, value in memory.items())):
        raise ValueError("recovery memory KMS references differ from sealed bundle")
    return memory


def verify_recovery_candidate(manifest_path, archive_path, anchors_path,
                              scope, authority, backup_kms, memory_kms):
    """Return evidence only after all independent and captured checks pass."""
    scope = {field: _uuid(scope[field]) for field in (
        "backup_id", "organization_id", "deployment_id")}
    receipt = authority.attest(scope)
    inner, objects, dek = _manifest(manifest_path, receipt, backup_kms, scope)
    entries, by_name = _inventory(inner)
    anchors = [entry for entry in entries
               if entry["archive_path"] == "host-anchor"
               or entry["archive_path"].startswith("host-anchor/")]
    controls, replay_image = _tar_object(
        archive_path, objects["archive"], dek, scope, "archive", entries)
    anchor_controls, _ = _tar_object(
        anchors_path, objects["anchors"], dek, scope, "anchors", anchors)
    if any(anchor_controls.get(name) != controls.get(name) for name in
           _CONTROL if name.startswith("host-anchor/")):
        raise ValueError("independent anchor copy differs from full archive")
    memory = _controls(inner, controls, by_name, scope)
    replay_entries = _replay(replay_image)
    memory_kms.verify(scope, memory, receipt["memory_challenge"])
    return {"scope": scope, "authorization_id": receipt["authorization_id"],
            "manifest_sha256": receipt["manifest_sha256"],
            "anchors_sha256": receipt["anchors_sha256"],
            "policy_version": inner["policy_version"],
            "replay_entries": replay_entries,
            "anchor_members": len(anchors),
            "engine_image": inner["engine_image"],
            "verdict": "verified_read_only_preflight"}
