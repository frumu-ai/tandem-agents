"""Validate initialized v3 backup sources and inventory every captured root."""

import json
import os
from pathlib import Path
import re
import stat
import uuid

from .backup_archive import (inventory_file, inventory_root, open_no_symlink,
                             plain_path, read_file)
from .contract import validate_keyring


REQUIRED_HOST_ROOTS = ("state", "security", "replay", "anchor", "panel_auth", "policy")
REQUIRED_ORDINARY_ROOTS = ("DATA", "REPOS", "RUNS", "SECRETS", "PANEL_STATE",
                           "KB_DOCS", "KB_INDEX", "PROXY_DATA", "PROXY_CONFIG")
CONFIG_FILES = ("hosted.env", "docker-compose.hosted.yml", "release-manifest.env",
                "release-manifest.json", "runtime-security.json", "proxy/Caddyfile")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate backup configuration field")
        value[key] = item
    return value


def _json_file(path):
    try:
        if path.lstat().st_size > 8 * 1024 * 1024:
            raise ValueError("backup configuration document exceeds 8 MiB")
        return json.loads(read_file(path), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"invalid backup input JSON: {path}") from None


def _uuid(value):
    try:
        parsed = str(uuid.UUID(value))
    except (TypeError, ValueError):
        raise ValueError("backup scope contains an invalid UUID") from None
    if parsed != value:
        raise ValueError("backup scope UUID must be canonical")
    return parsed


def _check_binding(bundle, *, strict_host):
    security = plain_path(bundle["host_paths"]["security"])
    if read_file(security / ".initialized") != b"runtime-security-v3\n":
        raise ValueError("only initialized runtime security v3 may be exported")
    binding = _json_file(security / "storage-roots.json")
    if strict_host:
        # The provisioning guard owns the exact ownership and sentinel rules.
        from .prepare import _v3_storage_identity
        identity = _v3_storage_identity(bundle, bundle["uid"], include_history=True)
    else:
        # Non-root tests exercise the same path/inode/sentinel binding on their
        # host, without weakening the operator CLI's Linux ownership checks.
        identity = {}
        for name, value in (("state", bundle["host_paths"]["state"]),
                            ("data", bundle["ordinary_paths"]["DATA"]),
                            ("replay", bundle["host_paths"]["replay"]),
                            ("anchor", bundle["host_paths"]["anchor"])):
            path = plain_path(value)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("v3 backup history root is missing")
            identity[name] = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
            if name in ("replay", "anchor"):
                try:
                    sentinel = read_file(path / ".runtime-security-v3-root")
                except OSError:
                    raise ValueError("v3 backup history sentinel is missing") from None
                if not re.fullmatch(b"[a-f0-9]{64}", sentinel):
                    raise ValueError("v3 backup history sentinel is invalid")
                identity[name]["sentinel"] = sentinel.decode("ascii")
    if binding != identity:
        raise ValueError("v3 storage identity changed; authorized recovery is required")
    return identity


def _check_source(install_root, *, strict_host):
    root = plain_path(install_root)
    bundle = _json_file(root / "runtime-security.json")
    if (not isinstance(bundle, dict) or bundle.get("schema_version") != 3
            or bundle.get("profile") != "hosted-single-node-v3"):
        raise ValueError("backup export requires a v3 runtime-security bundle")
    organization_id = _uuid(bundle.get("organization_id"))
    deployment_id = _uuid(bundle.get("deployment_id"))
    hosts = bundle.get("host_paths")
    ordinary = bundle.get("ordinary_paths")
    if (not isinstance(hosts, dict) or not isinstance(ordinary, dict)
            or set(hosts) != set(REQUIRED_HOST_ROOTS) | {"memory_kms_commands"}
            or set(ordinary) != set(REQUIRED_ORDINARY_ROOTS)):
        raise ValueError("v3 backup root contract is incomplete")
    locations = [plain_path(value) for value in [*hosts.values(), *ordinary.values()]]
    for index, path in enumerate(locations):
        for other in locations[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError("v3 backup roots must be independent")
    anchor_root = plain_path(hosts["anchor"])
    if root == anchor_root or root in anchor_root.parents or anchor_root in root.parents:
        raise ValueError("v3 audit anchor must be outside the installation tree")
    memory = bundle.get("memory_encryption", {})
    provenance = bundle.get("engine_provenance", {})
    if (memory.get("provider") != "google_cloud_kms"
            or any(not memory.get(key) for key in
                   ("runtime_principal_id", "kek_id", "kek_version"))
            or type(memory.get("rotation_epoch")) is not int
            or not 0 <= memory["rotation_epoch"] < 2**64
            or not re.fullmatch(r"[a-f0-9]{40}", provenance.get("source_revision", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", provenance.get("binary_sha256", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", provenance.get("attestation_sha256", ""))
            or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}",
                                bundle.get("images", {}).get("engine", ""))):
        raise ValueError("v3 memory or engine provenance is incomplete")
    if strict_host:
        from .prepare import _check_memory_commands
        _check_memory_commands(bundle, bundle["gid"])
        from .prepare import _check_file
        for name in ("security", "panel_auth", "policy"):
            info = plain_path(hosts[name]).lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != bundle["uid"]
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError(f"v3 {name} backup root has unsafe ownership or mode")
        security_root = plain_path(hosts["security"])
        for suffix in (".initialized", "storage-roots.json", "audit-hmac-key",
                       "context-keyring.json"):
            _check_file(security_root / suffix, bundle["uid"])
        _check_file(plain_path(hosts["policy"]) / "current.json", bundle["uid"])
    identity = _check_binding(bundle, strict_host=strict_host)
    security = plain_path(hosts["security"])
    audit_key = read_file(security / "audit-hmac-key")
    if len(audit_key.strip()) < 32:
        raise ValueError("v3 audit HMAC key is missing or invalid")
    keyring = _json_file(security / "context-keyring.json")
    validate_keyring(keyring, deployment_id, organization_id)
    replay = plain_path(hosts["replay"]) / "assertions.sqlite3"
    from .backup_archive import file_info
    file_info(replay)
    descriptor = open_no_symlink(replay)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            replay_header = handle.read(16)
    finally:
        os.close(descriptor)
    if replay_header != b"SQLite format 3\x00":
        raise ValueError("v3 durable replay database is missing or invalid")
    policy = _json_file(plain_path(hosts["policy"]) / "current.json")
    if (not isinstance(policy, dict) or policy.get("schema_version") != 1
            or policy.get("organization_id") != organization_id
            or policy.get("deployment_id") != deployment_id
            or type(policy.get("policy_version")) is not int
            or policy["policy_version"] < 1):
        raise ValueError("on-host policy snapshot is missing or out of scope")
    release = _json_file(root / "release-manifest.json")
    if (not isinstance(release, dict) or release.get("runtime_security_version") != 3
            or release.get("engine_provenance") != bundle["engine_provenance"]
            or release.get("engine_image") != bundle["images"]["engine"]):
        raise ValueError("installed release and v3 provenance do not match")
    return root, bundle, identity, policy, release


def collect_inventory(root, bundle):
    hosts = bundle["host_paths"]
    ordinary = bundle["ordinary_paths"]
    entries = []
    root_records = {}
    for name in REQUIRED_HOST_ROOTS:
        group = f"host-{name}"
        group_entries = inventory_root(group, hosts[name])
        root_records[group] = group_entries[0]
        entries.extend(group_entries)
    for name in sorted(ordinary):
        group = f"ordinary-{name.lower()}"
        group_entries = inventory_root(group, ordinary[name])
        root_records[group] = group_entries[0]
        entries.extend(group_entries)
    for index, suffix in enumerate(CONFIG_FILES):
        path = root / suffix
        entry = inventory_file(f"config-{index}", path)
        entries.append(entry)
    return entries, root_records
