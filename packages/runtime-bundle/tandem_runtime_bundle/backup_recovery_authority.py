"""Root-only external authority protocols for read-only v3 recovery preflight."""

import base64
import hashlib
import re
from urllib.parse import urlsplit

from .backup_commands import call_command, validate_operator_command


_HEX = re.compile(r"[a-f0-9]{64}\Z")


def _decode(value, name, minimum, maximum):
    if not isinstance(value, str):
        raise ValueError(f"{name} is missing")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError(f"{name} is not canonical base64") from None
    if not minimum <= len(decoded) <= maximum or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{name} has an invalid length or encoding")
    return decoded


class RecoveryAuthority:
    """An independently protected receipt and old-host fence authority."""

    def __init__(self, command, remote_host):
        self.command = validate_operator_command(command)
        if (not isinstance(remote_host, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", remote_host)
                or remote_host.endswith((".localhost", ".local"))):
            raise ValueError("recovery authority requires a private backup DNS host")
        self.remote_host = remote_host

    def attest(self, scope):
        result = call_command(self.command, {
            "schema_version": 1, "operation": "authorize_recovery_preflight", **scope,
        })
        key = "v3/{organization_id}/{deployment_id}/{backup_id}/manifest.json".format(**scope)
        uri = result.get("remote_uri")
        parsed = urlsplit(uri) if isinstance(uri, str) else None
        if (type(result.get("schema_version")) is not int or result["schema_version"] != 1
                or any(result.get(field) != value for field, value in scope.items())
                or result.get("manifest_object_key") != key
                or any(not isinstance(result.get(field), str)
                       or not _HEX.fullmatch(result[field])
                       for field in ("manifest_sha256", "archive_sha256", "anchors_sha256"))
                or type(result.get("manifest_size")) is not int
                or result["manifest_size"] <= 0
                or result.get("private") is not True
                or result.get("immutable") is not True
                or result.get("verified") is not True
                or result.get("old_host_fenced") is not True
                or result.get("operator_authorized") is not True
                or not isinstance(result.get("authorization_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", result["authorization_id"])
                or parsed is None or parsed.scheme != "https"
                or parsed.hostname != self.remote_host or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/" + key):
            raise ValueError("recovery authority receipt is missing or outside the fenced scope")
        challenge = result.get("memory_challenge")
        if (not isinstance(challenge, dict) or set(challenge) != {
                "ciphertext_base64", "plaintext_sha256"}
                or not isinstance(challenge["plaintext_sha256"], str)
                or not _HEX.fullmatch(challenge["plaintext_sha256"])):
            raise ValueError("recovery authority omitted an independent memory KMS challenge")
        _decode(challenge["ciphertext_base64"], "memory KMS challenge", 32, 8192)
        return result


class MemoryKmsChallenge:
    """Request decryption with independently provisioned new-host memory KMS access."""

    def __init__(self, command):
        self.command = validate_operator_command(command)

    def verify(self, scope, memory, challenge):
        fields = {key: memory[key] for key in (
            "provider", "runtime_principal_id", "kek_id", "kek_version", "rotation_epoch")}
        result = call_command(self.command, {
            "schema_version": 1, "operation": "decrypt_recovery_challenge",
            **scope, **fields, "ciphertext_base64": challenge["ciphertext_base64"],
        })
        if (type(result.get("schema_version")) is not int or result["schema_version"] != 1
                or any(result.get(key) != value for key, value in {**scope, **fields}.items())):
            raise ValueError("memory KMS challenge response has the wrong scope or key")
        plaintext = _decode(result.get("plaintext_base64"), "memory KMS plaintext", 32, 128)
        if hashlib.sha256(plaintext).hexdigest() != challenge["plaintext_sha256"]:
            raise ValueError("memory KMS challenge did not match independent authority")


class LatestKeyringAuthority:
    """Read the latest runtime-acknowledged keyring from a separate recovery ledger."""

    def __init__(self, command):
        self.command = validate_operator_command(command)

    def attest(self, scope, authorization_id):
        result = call_command(self.command, {
            "schema_version": 1, "operation": "attest_latest_runtime_keyring",
            **scope, "authorization_id": authorization_id,
        })
        if (not isinstance(result, dict) or set(result) != {
                "schema_version", "backup_id", "organization_id", "deployment_id",
                "authorization_id", "generation", "document_sha256",
                "runtime_acknowledged", "old_host_fenced", "latest"}
                or type(result["schema_version"]) is not int
                or result["schema_version"] != 1
                or any(result[field] != scope[field] for field in scope)
                or result["authorization_id"] != authorization_id
                or type(result["generation"]) is not int
                or not 0 < result["generation"] < 2**64
                or not isinstance(result["document_sha256"], str)
                or not _HEX.fullmatch(result["document_sha256"])
                or result["runtime_acknowledged"] is not True
                or result["old_host_fenced"] is not True
                or result["latest"] is not True):
            raise ValueError("latest keyring authority did not attest the fenced recovery scope")
        return result
