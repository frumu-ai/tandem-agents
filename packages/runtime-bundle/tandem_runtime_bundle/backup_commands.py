"""Strict interfaces for externally provisioned backup KMS and off-site storage."""

import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit

from .backup_archive import canonical_json, plain_path


_HEX256 = re.compile(r"[a-f0-9]{64}\Z")


def validate_operator_command(value):
    """Commands are installed by the host operator, never by workload mounts."""
    if os.name != "posix" or os.geteuid() != 0:
        raise ValueError("backup export requires a Linux root operator")
    path = plain_path(value)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) not in (0o500, 0o700)):
        raise ValueError("backup command must be a root-only executable (0500 or 0700)")
    for parent in path.parents:
        info = parent.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (mode & 0o022 and not (mode & stat.S_ISVTX))):
            raise ValueError("backup command ancestors must prevent non-root replacement")
    return path


def call_command(path, payload, *, timeout=30):
    try:
        completed = subprocess.run([str(path)], input=canonical_json(payload),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout, check=False,
                                   env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C"})
    except subprocess.TimeoutExpired:
        raise ValueError("backup authority command timed out") from None
    if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > 16384:
        raise ValueError("backup authority command failed or returned invalid output")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("backup authority command returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("backup authority command returned a non-object")
    return value


class BackupKms:
    def __init__(self, command, key_id, key_version):
        self.command = validate_operator_command(command)
        self.key_id = key_id
        self.key_version = key_version
        if (not isinstance(key_id, str) or not isinstance(key_version, str)
                or not re.fullmatch(r"[A-Za-z0-9._:/-]{10,500}", key_id)
                or not key_version.startswith(key_id + "/")
                or not re.fullmatch(r"[A-Za-z0-9._:/-]{12,600}", key_version)):
            raise ValueError("backup KMS key and version must be explicit resources")

    def wrap(self, dek, scope):
        result = call_command(self.command, {
            "schema_version": 1, "operation": "wrap", "key_id": self.key_id,
            "key_version": self.key_version, "backup_id": scope["backup_id"],
            "organization_id": scope["organization_id"],
            "deployment_id": scope["deployment_id"],
            "plaintext_dek_base64": base64.b64encode(dek).decode("ascii"),
        })
        if (result.get("schema_version") != 1 or result.get("key_id") != self.key_id
                or result.get("key_version") != self.key_version):
            raise ValueError("backup KMS did not attest the configured key version")
        encoded = result.get("wrapped_dek_base64")
        if not isinstance(encoded, str):
            raise ValueError("backup KMS did not return a wrapped DEK")
        try:
            wrapped = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("backup KMS returned invalid ciphertext") from None
        if len(wrapped) < 32 or len(wrapped) > 8192:
            raise ValueError("backup KMS returned an invalid wrapped DEK length")
        return base64.b64encode(wrapped).decode("ascii")

    def unwrap(self, wrapped_dek_base64, scope):
        """Ask the independent backup authority for this exact scoped DEK."""
        if not isinstance(wrapped_dek_base64, str):
            raise ValueError("backup KMS ciphertext is missing")
        try:
            wrapped = base64.b64decode(wrapped_dek_base64, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("backup KMS ciphertext is invalid") from None
        if not 32 <= len(wrapped) <= 8192:
            raise ValueError("backup KMS ciphertext length is invalid")
        result = call_command(self.command, {
            "schema_version": 1, "operation": "unwrap", "key_id": self.key_id,
            "key_version": self.key_version, **scope,
            "wrapped_dek_base64": wrapped_dek_base64,
        })
        if (type(result.get("schema_version")) is not int or result["schema_version"] != 1
                or result.get("key_id") != self.key_id
                or result.get("key_version") != self.key_version
                or any(result.get(key) != value for key, value in scope.items())):
            raise ValueError("backup KMS unwrap did not attest key and scope")
        encoded = result.get("plaintext_dek_base64")
        if not isinstance(encoded, str):
            raise ValueError("backup KMS unwrap omitted the DEK")
        try:
            dek = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("backup KMS unwrap returned invalid DEK") from None
        if len(dek) != 32:
            raise ValueError("backup KMS unwrap returned an invalid DEK length")
        return dek


class OffsiteUploader:
    def __init__(self, command, remote_host):
        self.command = validate_operator_command(command)
        if (not isinstance(remote_host, str) or not remote_host
                or remote_host.lower() != remote_host or "/" in remote_host
                or ":" in remote_host or "." not in remote_host
                or remote_host.endswith((".localhost", ".local"))):
            raise ValueError("a private off-site HTTPS host must be configured")
        try:
            ipaddress.ip_address(remote_host)
        except ValueError:
            pass
        else:
            raise ValueError("off-site host must be a DNS name, not an IP literal")
        self.remote_host = remote_host

    def _check_receipt(self, result, key, sha256, size, *, verified):
        if (result.get("schema_version") != 1 or result.get("object_key") != key
                or result.get("sha256") != sha256 or result.get("size") != size
                or result.get("private") is not True or result.get("if_absent") is not True
                or result.get("verified") is not verified):
            raise ValueError("off-site uploader returned a mismatched receipt")
        uri = result.get("remote_uri")
        if not isinstance(uri, str):
            raise ValueError("off-site uploader omitted remote URI")
        parsed = urlsplit(uri)
        if (parsed.scheme != "https" or parsed.hostname != self.remote_host
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path):
            raise ValueError("off-site uploader did not attest a private HTTPS destination")
        return uri

    def put_verified(self, source_path, object_key, sha256, size):
        if not _HEX256.fullmatch(sha256) or size <= 0:
            raise ValueError("invalid off-site object digest or length")
        source = plain_path(source_path)
        if (not object_key.startswith("v3/") or ".." in object_key.split("/")
                or any(not component for component in object_key.split("/"))):
            raise ValueError("invalid off-site object key")
        put = call_command(self.command, {
            "schema_version": 1, "operation": "put_if_absent", "source_path": str(source),
            "object_key": object_key, "sha256": sha256, "size": size,
            "private": True,
        }, timeout=300)
        if put.get("created") is not True:
            raise ValueError("off-site uploader did not create an immutable object")
        uri = self._check_receipt(put, object_key, sha256, size, verified=False)
        check = call_command(self.command, {
            "schema_version": 1, "operation": "verify", "object_key": object_key,
            "sha256": sha256, "size": size, "private": True,
        }, timeout=300)
        if self._check_receipt(check, object_key, sha256, size, verified=True) != uri:
            raise ValueError("off-site verifier returned a different object URI")
        return uri
