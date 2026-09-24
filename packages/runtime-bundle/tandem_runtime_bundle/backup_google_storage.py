"""Root-only Google Cloud Storage transport for encrypted v3 backup objects.

This command satisfies the export uploader protocol. It is not the independent
recovery receipt, operator authorization, or old-host fence authority.
"""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .backup_archive import open_no_symlink, plain_path


CONFIG_PATH = Path("/etc/tandem-backup-gcs/config.json")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_NAMES = {"archive.aead", "anchors.aead", "manifest.json"}
_MAX_INPUT = 16 * 1024
_CHUNK = 1024 * 1024


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate backup storage field")
        result[name] = value
    return result


def _canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("invalid backup object scope") from None


def _object_key(value):
    if not isinstance(value, str):
        raise ValueError("invalid backup object key")
    parts = value.split("/")
    if len(parts) != 5 or parts[0] != "v3" or parts[-1] not in _NAMES:
        raise ValueError("invalid backup object key")
    for item in parts[1:4]:
        _canonical_uuid(item)
    return parts[1], parts[2], parts[3]


def _private_file(value, label):
    path = plain_path(value)
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


def load_config(path=CONFIG_PATH):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("backup storage transport requires Linux root")
    path = _private_file(path, "backup storage configuration")
    if path.stat().st_size > _MAX_INPUT:
        raise ValueError("backup storage configuration exceeds limit")
    descriptor = open_no_symlink(path)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_INPUT + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_INPUT:
        raise ValueError("backup storage configuration exceeds limit")
    config = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(config, dict) or set(config) != {
            "schema_version", "bucket", "project_id", "credentials_file",
            "organization_id", "deployment_id", "minimum_retention_seconds"}
            or type(config["schema_version"]) is not int
            or config["schema_version"] != 1
            or not isinstance(config["bucket"], str)
            or not _BUCKET.fullmatch(config["bucket"])
            or not isinstance(config["project_id"], str)
            or not _PROJECT.fullmatch(config["project_id"])
            or type(config["minimum_retention_seconds"]) is not int
            or not 1 <= config["minimum_retention_seconds"] <= 3155760000):
        raise ValueError("backup storage configuration is invalid")
    _canonical_uuid(config["organization_id"])
    _canonical_uuid(config["deployment_id"])
    credential_path = plain_path(config["credentials_file"])
    if credential_path.parent != path.parent:
        raise ValueError("backup storage credentials must be beside configuration")
    config["credentials_file"] = str(_private_file(
        credential_path, "backup storage credentials"))
    if credential_path.stat().st_size > 1024 * 1024:
        raise ValueError("backup storage credentials exceed limit")
    return config


def _client(config):
    if "STORAGE_EMULATOR_HOST" in os.environ:
        raise ValueError("backup storage emulator is not allowed")
    import google.auth
    from google.cloud import storage

    credentials, _ = google.auth.load_credentials_from_file(
        config["credentials_file"],
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return storage.Client(project=config["project_id"], credentials=credentials)


def _checked_bucket(client, config):
    bucket = client.bucket(config["bucket"])
    bucket.reload(timeout=30)
    iam = bucket.iam_configuration
    effective = bucket.retention_policy_effective_time
    if (bucket.name != config["bucket"]
            or iam.uniform_bucket_level_access_enabled is not True
            or iam.public_access_prevention != "enforced"
            or bucket.retention_policy_locked is not True
            or type(bucket.retention_period) is not int
            or bucket.retention_period < config["minimum_retention_seconds"]
            or not isinstance(effective, datetime)
            or effective.tzinfo is None or effective > datetime.now(timezone.utc)
            or bucket.versioning_enabled is not False):
        raise ValueError("backup storage bucket lacks required private retention policy")
    return bucket


def _request(request, config):
    if not isinstance(request, dict):
        raise ValueError("backup storage request must be an object")
    operation = request.get("operation")
    fields = {"schema_version", "operation", "object_key", "sha256", "size", "private"}
    if operation == "put_if_absent":
        fields.add("source_path")
    if (operation not in ("put_if_absent", "verify") or set(request) != fields
            or type(request["schema_version"]) is not int
            or request["schema_version"] != 1 or request["private"] is not True
            or not isinstance(request["sha256"], str)
            or not _SHA256.fullmatch(request["sha256"])
            or type(request["size"]) is not int
            or not 0 < request["size"] < 2**63):
        raise ValueError("backup storage request fields are invalid")
    organization_id, deployment_id, _ = _object_key(request["object_key"])
    if (organization_id != config["organization_id"]
            or deployment_id != config["deployment_id"]):
        raise ValueError("backup storage object key is outside configured scope")
    return operation


class _HashSink:
    def __init__(self):
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data):
        self.digest.update(data)
        self.size += len(data)
        return len(data)

    def flush(self):
        pass

    def tell(self):
        return self.size

    def seek(self, offset, whence=io.SEEK_SET):
        # The SDK can restart a transcoded download at offset zero. The
        # resulting bytes still have to match the encrypted source digest.
        if offset != 0 or whence != io.SEEK_SET:
            raise OSError("backup storage readback only supports reset")
        self.digest = hashlib.sha256()
        self.size = 0
        return 0


def _verify(bucket, request, *, generation=None):
    blob = bucket.blob(request["object_key"])
    blob.reload(timeout=30)
    actual_generation = blob.generation
    if (type(actual_generation) is not int or actual_generation <= 0
            or generation is not None and actual_generation != generation
            or type(blob.size) is not int or blob.size != request["size"]):
        raise ValueError("backup storage object metadata differs from request")
    sink = _HashSink()
    blob.download_to_file(sink, if_generation_match=actual_generation,
                          raw_download=True, checksum="crc32c", timeout=120)
    if (sink.size != request["size"]
            or sink.digest.hexdigest() != request["sha256"]):
        raise ValueError("backup storage readback differs from request")
    live = bucket.blob(request["object_key"])
    live.reload(timeout=30)
    if live.generation != actual_generation:
        raise ValueError("backup storage live object changed during readback")
    return actual_generation


def _put(bucket, request):
    source = _private_file(request["source_path"], "encrypted backup source")
    if source.name != request["object_key"].split("/")[-1]:
        raise ValueError("encrypted backup source name differs from object key")
    descriptor = open_no_symlink(source)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != request["size"]):
            raise ValueError("encrypted backup source changed before upload")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
            if digest.hexdigest() != request["sha256"]:
                raise ValueError("encrypted backup source hash differs from request")
            handle.seek(0)
            blob = bucket.blob(request["object_key"])
            blob.upload_from_file(handle, size=request["size"],
                                  if_generation_match=0, checksum="crc32c",
                                  content_type="application/octet-stream", timeout=120)
        after = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("encrypted backup source changed during upload")
        if type(blob.generation) is not int or blob.generation <= 0:
            raise ValueError("backup storage omitted created generation")
        return blob.generation
    finally:
        os.close(descriptor)


def execute(request, config, client):
    operation = _request(request, config)
    bucket = _checked_bucket(client, config)
    if operation == "put_if_absent":
        generation = _put(bucket, request)
        # Recheck settings after the create; the credential should not be able
        # to mutate bucket policy, but an administrator could race this command.
        _checked_bucket(client, config)
        verified = False
    else:
        generation = _verify(bucket, request)
        _checked_bucket(client, config)
        verified = True
    response = {name: request[name] for name in ("schema_version", "object_key",
                                                 "sha256", "size", "private")}
    response.update({"if_absent": True, "verified": verified,
                     "remote_generation": generation,
                     "remote_uri": (f"https://{config['bucket']}.storage.googleapis.com/"
                                    + request["object_key"])})
    if operation == "put_if_absent":
        response["created"] = True
    return response


def main():
    try:
        config = load_config()
        raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            raise ValueError("backup storage request exceeds limit")
        request = json.loads(raw, object_pairs_hook=_unique_object)
        response = execute(request, config, _client(config))
    except Exception:
        # No cloud errors, credential paths, source paths or object identifiers.
        sys.stderr.write("Backup storage request rejected.\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
