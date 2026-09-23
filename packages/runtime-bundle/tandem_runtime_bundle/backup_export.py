"""Export-only, fail-closed encrypted off-site capture of initialized v3 hosts.

This module deliberately has no restore, extraction, or storage-rebind function.
"""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import uuid

from .backup_archive import (canonical_json, inventory_file, inventory_root,
                             open_no_symlink, plain_path, read_file, write_encrypted_tar)
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


def _service_inactive(unit):
    result = subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                            text=True, timeout=10, check=False)
    if result.stdout.strip() != "inactive" or result.returncode != 3:
        raise ValueError(f"backup requires inactive systemd unit: {unit}")


def assert_quiescent(install_root, deployment_id):
    """Require operator-established downtime; never stop/restart services here."""
    if os.name != "posix" or os.geteuid() != 0:
        raise ValueError("backup export requires a Linux root operator")
    _service_inactive("tandem-hosted-update-agent.service")
    policy_unit = f"tandem-policy-sync-{deployment_id}"
    _service_inactive(policy_unit + ".timer")
    _service_inactive(policy_unit + ".service")
    root = plain_path(install_root)
    result = subprocess.run(["docker", "compose", "--env-file", str(root / "hosted.env"),
                             "-f", str(root / "docker-compose.hosted.yml"),
                             "ps", "--services", "--status", "running"],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0 or result.stdout.strip():
        raise ValueError("backup requires all hosted compose services stopped")


def _validate_staging(staging_root, source_paths, *, strict_host):
    staging = plain_path(staging_root)
    info = staging.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("backup staging root must be an existing directory")
    if strict_host and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("backup staging root must be root-owned mode 0700")
    for source in source_paths:
        path = plain_path(source)
        if staging == path or staging in path.parents or path in staging.parents:
            raise ValueError("backup staging must be outside every captured root")
    return staging


def _sealed_manifest(inner, outer, dek):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    # Domain 2 is disjoint from archive (0) and anchor (1) chunk nonces.
    nonce = b"\x02" + secrets.token_bytes(11)
    ciphertext = AESGCM(dek).encrypt(nonce, canonical_json(inner), canonical_json(outer))
    return {**outer, "manifest_nonce_base64": base64.b64encode(nonce).decode("ascii"),
            "manifest_ciphertext_base64": base64.b64encode(ciphertext).decode("ascii")}


def _file_digest(path):
    payload = read_file(path)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def export_backup(install_root, staging_root, kms, uploader, *, quiescence=None,
                  strict_host=True, backup_id=None):
    """Publish encrypted objects, then one authenticated commit manifest last."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    root, bundle, identity, policy, release = _check_source(install_root, strict_host=strict_host)
    deployment_id = bundle["deployment_id"]
    organization_id = bundle["organization_id"]
    guard = quiescence or assert_quiescent
    guard(root, deployment_id)
    backup_id = _uuid(backup_id) if backup_id else str(uuid.uuid4())
    scope = {"backup_id": backup_id, "organization_id": organization_id,
             "deployment_id": deployment_id}
    source_paths = [*bundle["host_paths"].values(), *bundle["ordinary_paths"].values(), root]
    if kms.key_id == bundle["memory_encryption"]["kek_id"]:
        raise ValueError("backup KMS authority must be separate from hosted memory KMS")
    if strict_host:
        for command in (kms.command, uploader.command):
            path = plain_path(command)
            if any(path == plain_path(source) or path in plain_path(source).parents
                   or plain_path(source) in path.parents for source in source_paths):
                raise ValueError("backup authority commands must be outside captured roots")
    staging = _validate_staging(staging_root, source_paths, strict_host=strict_host)
    before, root_records = collect_inventory(root, bundle)
    anchor_entries = [item for item in before if item["archive_path"].startswith("host-anchor/")
                      or item["archive_path"] == "host-anchor"]
    dek = secrets.token_bytes(32)
    wrapped = kms.wrap(dek, scope)
    prefix = f"v3/{organization_id}/{deployment_id}/{backup_id}"
    with tempfile.TemporaryDirectory(prefix="backup-export-", dir=staging) as temporary:
        temporary = Path(temporary)
        archive_path = temporary / "archive.aead"
        anchor_path = temporary / "anchors.aead"
        objects = {}
        for name, path, selected in (("archive", archive_path, before),
                                     ("anchors", anchor_path, anchor_entries)):
            context = {**scope, "format": 1, "kind": name}
            nonce_domain = b"\x00" if name == "archive" else b"\x01"
            objects[name] = write_encrypted_tar(path, selected, dek,
                                                nonce_domain + secrets.token_bytes(7), context)
        guard(root, deployment_id)
        after, after_roots = collect_inventory(root, bundle)
        if (before != after or root_records != after_roots
                or identity != _check_binding(bundle, strict_host=strict_host)):
            raise ValueError("backup source changed during capture")
        inner = {
            "format": 1, "scope": scope,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "runtime_security_profile": bundle["profile"],
            "engine_image": bundle["images"]["engine"],
            "engine_provenance": bundle["engine_provenance"],
            "release_tag": release.get("release_tag"),
            "storage_identity": identity,
            "roots": root_records,
            "inventory": before,
            "policy_version": policy["policy_version"],
            "policy_sha256": next(item["sha256"] for item in before
                                  if item["archive_path"] == "host-policy/current.json"),
            "audit_key_id": bundle["engine_environment"]["TANDEM_AUDIT_HMAC_KEY_ID"],
            "memory_kms": {key: bundle["memory_encryption"][key] for key in
                           ("provider", "runtime_principal_id", "kek_id", "kek_version", "rotation_epoch")},
        }
        object_metadata = {name: {**details, "object_key": f"{prefix}/{name}.aead"}
                           for name, details in objects.items()}
        outer = {"format": 1, "scope": scope, "objects": object_metadata,
                 "backup_kms": {"key_id": kms.key_id, "key_version": kms.key_version,
                                "wrapped_dek_base64": wrapped}}
        commit = _sealed_manifest(inner, outer, dek)
        commit_path = temporary / "manifest.json"
        commit_path.write_bytes(canonical_json(commit))
        os.chmod(commit_path, 0o600)
        for name, path in (("archive", archive_path), ("anchors", anchor_path)):
            details = object_metadata[name]
            uploader.put_verified(path, details["object_key"], details["sha256"], details["size"])
        # This object is the only completion marker. Earlier objects are orphans
        # on failure and must never be interpreted as a restorable backup.
        digest, size = _file_digest(commit_path)
        guard(root, deployment_id)
        uri = uploader.put_verified(commit_path, f"{prefix}/manifest.json", digest, size)
    return {"backup_id": backup_id, "manifest_uri": uri, "manifest_sha256": digest,
            "organization_id": organization_id, "deployment_id": deployment_id}
