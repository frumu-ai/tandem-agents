"""Export-only, fail-closed encrypted off-site capture of initialized v3 hosts.

This module deliberately has no restore, extraction, or storage-rebind function.
"""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import uuid

from .backup_archive import canonical_json, plain_path, read_file, write_encrypted_tar
from .backup_source import _check_source, _uuid, collect_inventory


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
    if strict_host:
        descriptor = _open_verified_staging(staging)
        os.close(descriptor)
    for source in source_paths:
        path = plain_path(source)
        if staging == path or staging in path.parents or path in staging.parents:
            raise ValueError("backup staging must be outside every captured root")
    return staging


def _safe_staging_ancestor(info):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("backup staging ancestor permits non-root replacement")


def _open_verified_staging(staging):
    """Pin a root-owned chain without following any component or trusting /tmp."""
    if os.name != "posix":
        raise ValueError("strict backup staging requires Linux")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(staging.anchor, flags)
    try:
        for component in staging.parts[1:]:
            _safe_staging_ancestor(os.fstat(descriptor))
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("backup staging root must be root-owned mode 0700")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _private_output_directory(staging, *, strict_host):
    if not strict_host:
        with tempfile.TemporaryDirectory(prefix="backup-export-", dir=staging) as temporary:
            yield Path(temporary), None
        return
    staging_fd = _open_verified_staging(staging)
    name = "backup-export-" + secrets.token_hex(16)
    directory_fd = None
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=staging_fd)
        created = True
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                               | os.O_CLOEXEC, dir_fd=staging_fd)
        os.fchmod(directory_fd, 0o700)
        yield staging / name, directory_fd
    finally:
        if directory_fd is not None:
            try:
                for filename in ("archive.aead", "anchors.aead", "manifest.json"):
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(directory_fd)
        try:
            if created:
                os.rmdir(name, dir_fd=staging_fd)
        finally:
            os.close(staging_fd)


def _new_output_file(directory, directory_fd, name):
    if name not in ("archive.aead", "anchors.aead", "manifest.json"):
        raise ValueError("invalid backup staging output")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    if directory_fd is None:
        descriptor = os.open(directory / name, flags, 0o600)
    else:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    if os.name == "posix":
        os.fchmod(descriptor, 0o600)
    return descriptor


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
    source_state = _check_source(install_root, strict_host=strict_host)
    root, bundle, identity, policy, release = source_state
    deployment_id = bundle["deployment_id"]
    organization_id = bundle["organization_id"]
    guard = quiescence or assert_quiescent
    guard(root, deployment_id)
    if _check_source(root, strict_host=strict_host) != source_state:
        raise ValueError("backup source changed during quiescence")
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
    if _check_source(root, strict_host=strict_host) != source_state:
        raise ValueError("backup source changed before capture")
    anchor_entries = [item for item in before if item["archive_path"].startswith("host-anchor/")
                      or item["archive_path"] == "host-anchor"]
    dek = secrets.token_bytes(32)
    wrapped = kms.wrap(dek, scope)
    prefix = f"v3/{organization_id}/{deployment_id}/{backup_id}"
    with _private_output_directory(staging, strict_host=strict_host) as (temporary, directory_fd):
        archive_path = temporary / "archive.aead"
        anchor_path = temporary / "anchors.aead"
        objects = {}
        for name, path, selected in (("archive", archive_path, before),
                                     ("anchors", anchor_path, anchor_entries)):
            context = {**scope, "format": 1, "kind": name}
            nonce_domain = b"\x00" if name == "archive" else b"\x01"
            objects[name] = write_encrypted_tar(path, selected, dek,
                                                nonce_domain + secrets.token_bytes(7), context,
                                                output_fd=_new_output_file(
                                                    temporary, directory_fd, path.name))
        guard(root, deployment_id)
        after, after_roots = collect_inventory(root, bundle)
        if (before != after or root_records != after_roots
                or _check_source(root, strict_host=strict_host) != source_state):
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
        with os.fdopen(_new_output_file(temporary, directory_fd, commit_path.name),
                       "wb", buffering=0) as output:
            output.write(canonical_json(commit))
            output.flush()
            os.fsync(output.fileno())
        for name, path in (("archive", archive_path), ("anchors", anchor_path)):
            details = object_metadata[name]
            uploader.put_verified(path, details["object_key"], details["sha256"], details["size"])
        # This object is the only completion marker. Earlier objects are orphans
        # on failure and must never be interpreted as a restorable backup.
        digest, size = _file_digest(commit_path)
        guard(root, deployment_id)
        if _check_source(root, strict_host=strict_host) != source_state:
            raise ValueError("backup source changed before commit")
        manifest_key = f"{prefix}/manifest.json"
        try:
            uri = uploader.put_verified(commit_path, manifest_key, digest, size)
        except (OSError, ValueError) as exc:
            # The remote PUT might have committed even when its acknowledgement
            # or read-back verification failed. Never imply that it did not.
            raise ValueError(
                f"backup manifest completion unconfirmed for backup {backup_id}; "
                f"reconcile {manifest_key} (sha256={digest}, size={size}) "
                "remotely before retry"
            ) from exc
    return {"backup_id": backup_id, "manifest_uri": uri, "manifest_sha256": digest,
            "organization_id": organization_id, "deployment_id": deployment_id}
