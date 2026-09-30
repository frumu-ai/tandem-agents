"""Root-only Google Cloud KMS transport for scoped v3 backup DEKs.

The read-only recovery preflight checks independent authority before invoking
this command. Root and provisioning of the unwrap credential are trusted here;
this transport does not independently attest recovery authorization.
"""

import base64
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .backup_archive import canonical_json


CONFIG_PATH = Path("/etc/tandem-backup-kms/config.json")
_KEY = re.compile(r"projects/[A-Za-z0-9._~-]+/locations/[A-Za-z0-9._~-]+/"
                  r"keyRings/[A-Za-z0-9._~-]+/cryptoKeys/[A-Za-z0-9._~-]+\Z")
_MAX_INPUT = 16 * 1024


def _crc32c(data):
    """CRC32C over short KMS requests, without a platform-specific dependency."""
    value = 0xffffffff
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82f63b78 if value & 1 else 0)
    return value ^ 0xffffffff


def _crc_value(value):
    value = getattr(value, "value", value)
    if type(value) is not int or not 0 <= value < 2**32:
        raise ValueError("KMS response omitted its checksum")
    return value


def _private_file(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute private file")
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError(f"{label} has a replaceable ancestor")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError(f"{label} must be root-owned mode 0600")
    return path


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate KMS configuration or request field")
        result[name] = value
    return result


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("invalid backup scope UUID") from None
    return value


def _b64(value, length_min, length_max):
    if not isinstance(value, str):
        raise ValueError("invalid KMS binary field")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("invalid KMS binary field") from None
    if (not length_min <= len(decoded) <= length_max
            or base64.b64encode(decoded).decode("ascii") != value):
        raise ValueError("invalid KMS binary field")
    return decoded


def load_config(path=CONFIG_PATH):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("backup KMS transport requires Linux root")
    config_path = _private_file(path, "backup KMS configuration")
    if config_path.stat().st_size > _MAX_INPUT:
        raise ValueError("backup KMS configuration exceeds limit")
    config = json.loads(config_path.read_bytes(), object_pairs_hook=_unique_object)
    if (not isinstance(config, dict) or set(config) != {
            "schema_version", "key_id", "key_version", "organization_id",
            "deployment_id", "credentials_file", "operation"}
            or type(config["schema_version"]) is not int
            or config["schema_version"] != 1
            or not isinstance(config["key_id"], str)
            or not _KEY.fullmatch(config["key_id"])
            or config["operation"] not in ("wrap", "unwrap")
            or not isinstance(config["key_version"], str)
            or not re.fullmatch(re.escape(config["key_id"]) +
                                r"/cryptoKeyVersions/[1-9][0-9]*", config["key_version"])):
        raise ValueError("backup KMS configuration is invalid")
    _uuid(config["organization_id"])
    _uuid(config["deployment_id"])
    config["credentials_file"] = str(_private_file(
        config["credentials_file"], "backup KMS credentials"))
    return config


def _request_scope(request, config):
    if (type(request.get("schema_version")) is not int
            or request["schema_version"] != 1
            or request.get("key_id") != config["key_id"]
            or request.get("key_version") != config["key_version"]
            or request.get("organization_id") != config["organization_id"]
            or request.get("deployment_id") != config["deployment_id"]):
        raise ValueError("backup KMS request is outside configured scope")
    _uuid(request.get("backup_id"))
    scope = {name: request[name] for name in (
        "backup_id", "organization_id", "deployment_id")}
    aad = canonical_json({"purpose": "tandem-v3-backup-dek", "schema_version": 1,
                          "key_id": config["key_id"],
                          "key_version": config["key_version"], **scope})
    return scope, aad


def _client(credentials_file):
    import google.auth
    from google.cloud import kms_v1

    credentials, _ = google.auth.load_credentials_from_file(
        credentials_file, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return kms_v1.KeyManagementServiceClient(credentials=credentials)


def execute(request, config, client):
    if not isinstance(request, dict):
        raise ValueError("backup KMS request must be an object")
    operation = request.get("operation")
    field = ("plaintext_dek_base64" if operation == "wrap" else
             "wrapped_dek_base64" if operation == "unwrap" else None)
    if operation != config["operation"] or field is None or set(request) != {
            "schema_version", "operation", "key_id", "key_version",
            "backup_id", "organization_id", "deployment_id", field}:
        raise ValueError("backup KMS request fields are invalid")
    scope, aad = _request_scope(request, config)
    if operation == "wrap":
        dek = _b64(request[field], 32, 32)
        response = client.encrypt(request={
            "name": config["key_version"], "plaintext": dek,
            "additional_authenticated_data": aad,
            "plaintext_crc32c": _crc32c(dek),
            "additional_authenticated_data_crc32c": _crc32c(aad),
        }, timeout=30)
        ciphertext = bytes(response.ciphertext)
        if (response.name != config["key_version"]
                or response.verified_plaintext_crc32c is not True
                or response.verified_additional_authenticated_data_crc32c is not True
                or not 32 <= len(ciphertext) <= 8192
                or _crc_value(response.ciphertext_crc32c) != _crc32c(ciphertext)):
            raise ValueError("backup KMS encryption was not verified")
        return {"schema_version": 1, "key_id": config["key_id"],
                "key_version": config["key_version"], **scope,
                "wrapped_dek_base64": base64.b64encode(ciphertext).decode("ascii")}
    ciphertext = _b64(request[field], 32, 8192)
    response = client.decrypt(request={
        "name": config["key_id"], "ciphertext": ciphertext,
        "additional_authenticated_data": aad,
        "ciphertext_crc32c": _crc32c(ciphertext),
        "additional_authenticated_data_crc32c": _crc32c(aad),
    }, timeout=30)
    dek = bytes(response.plaintext)
    if len(dek) != 32 or _crc_value(response.plaintext_crc32c) != _crc32c(dek):
        raise ValueError("backup KMS decryption was not verified")
    return {"schema_version": 1, "key_id": config["key_id"],
            "key_version": config["key_version"], **scope,
            "plaintext_dek_base64": base64.b64encode(dek).decode("ascii")}


def main():
    try:
        config = load_config()
        raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            raise ValueError("backup KMS request exceeds limit")
        request = json.loads(raw, object_pairs_hook=_unique_object)
        response = execute(request, config, _client(config["credentials_file"]))
    except Exception:
        # Do not print Cloud API details, credential paths, scope or DEKs.
        sys.stderr.write("Backup KMS request rejected.\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
