"""Provision only runtime security files; never generate human signing authority."""
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

from .contract import CONTRACT_VERSION, validate_keyring


def _plain_path(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("security paths must be absolute and contain no symlinks")
    return path


def _check_file(path, uid, mode=0o600):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("security input must be a regular file with one link")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("security file must be runtime-owned with mode 0600")


def _directory(path, uid, gid):
    path = _plain_path(path)
    if path.exists():
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("existing security directory must be runtime-owned with mode 0700")
        return path
    path.mkdir(parents=True, mode=0o700)
    os.chown(path, uid, gid)
    return path


def _write(path, value, uid, gid):
    if path.exists() or path.is_symlink():
        _check_file(path, uid)
    descriptor, temp_name = tempfile.mkstemp(prefix=".security-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), uid, gid)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def prepare_security(bundle, keyring, host_agent_token_file, secrets_root):
    """Called by authorized Linux bootstrap. Repeated calls preserve audit/replay state."""
    if os.name != "posix":
        raise ValueError("runtime security provisioning requires Linux")
    if bundle.get("schema_version") != CONTRACT_VERSION:
        raise ValueError("unsupported runtime security contract")
    validate_keyring(keyring, bundle["deployment_id"], bundle["organization_id"])
    uid, gid = bundle["uid"], bundle["gid"]
    if os.geteuid() not in (0, uid):
        raise ValueError("provision as root or the configured runtime user")
    source = _plain_path(host_agent_token_file)
    source_info = source.lstat()
    if (not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1
            or stat.S_IMODE(source_info.st_mode) & 0o077):
        raise ValueError("host agent token source must be an owner-only regular file")
    host_token = source.read_bytes().strip()
    if len(host_token) < 32 or any(byte <= 32 or byte >= 127 for byte in host_token):
        raise ValueError("a provisioned host agent token is required")
    paths = {name: _plain_path(path) for name, path in bundle["host_paths"].items()}
    roots = list(paths.values())
    for index, path in enumerate(roots):
        for other in roots[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError("security storage roots must remain independent")
    previous_mask = os.umask(0o077)
    try:
        security = _directory(paths["security"], uid, gid)
        replay = _directory(paths["replay"], uid, gid)
        anchor = _directory(paths["anchor"], uid, gid)
        target_secrets = _plain_path(secrets_root)
        target_secrets.mkdir(parents=True, exist_ok=True)
        audit_key = security / "audit-hmac-key"
        if audit_key.exists() or audit_key.is_symlink():
            _check_file(audit_key, uid)
            if len(audit_key.read_bytes().strip()) < 32:
                raise ValueError("existing audit key is invalid; authorized recovery is required")
        else:
            if (security / ".initialized").exists() or any(replay.iterdir()) or any(anchor.iterdir()):
                raise ValueError("audit key is missing from initialized storage; authorized recovery is required")
            _write(audit_key, secrets.token_hex(32).encode(), uid, gid)
        _write(security / "context-keyring.json", json.dumps(keyring, sort_keys=True).encode(), uid, gid)
        _write(target_secrets / "host_agent_token", host_token, uid, gid)
        _write(security / ".initialized", b"runtime-security-v1\n", uid, gid)
        # The engine must create the replay database; an empty placeholder is invalid.
    finally:
        os.umask(previous_mask)
