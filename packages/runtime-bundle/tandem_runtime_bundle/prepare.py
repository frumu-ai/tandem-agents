"""Provision only runtime security files; never generate human signing authority."""
import json
import copy
import os
import secrets
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .contract import validate_keyring


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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _v3_storage_identity(bundle, uid):
    """Bind an initialized v3 security root to the exact workload mounts."""
    identity = {}
    for name, value in (("state", bundle["host_paths"]["state"]),
                        ("data", bundle["ordinary_paths"]["DATA"])):
        path = _plain_path(value)
        try:
            info = path.lstat()
        except OSError:
            raise ValueError("v3 workload roots must exist before security provisioning") from None
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError("v3 workload roots must be runtime-owned directories without group or world write")
        identity[name] = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    return identity


def _check_memory_commands(bundle, gid):
    """Require preprovisioned executables isolated from shared writable secrets."""
    from .policy_contract import MEMORY_COMMAND_DIR

    try:
        directory = _plain_path(bundle["host_paths"]["memory_kms_commands"])
        memory = bundle["memory_encryption"]
        info = directory.lstat()
    except (KeyError, OSError):
        raise ValueError("v3 memory KMS command directory must be preprovisioned") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != gid
            or stat.S_IMODE(info.st_mode) != 0o750):
        raise ValueError("v3 memory KMS command directory must be root-owned with runtime-group traverse")
    # Owning a parent directory is enough to replace this root-owned directory
    # before a later bind mount. A root-owned sticky parent (for example /tmp)
    # still protects a root-owned child from non-root replacement.
    for parent in directory.parents:
        info = parent.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (mode & 0o022 and not (mode & stat.S_ISVTX))):
            raise ValueError("v3 memory KMS command ancestors must prevent non-root replacement")
    for operation in ("encrypt", "decrypt"):
        try:
            reference = PurePosixPath(memory[f"{operation}_command"])
            if reference.parent != MEMORY_COMMAND_DIR or reference.name in ("", ".", ".."):
                raise ValueError()
            command = _plain_path(directory / reference.name)
            info = command.lstat()
        except (KeyError, OSError, ValueError):
            raise ValueError(f"v3 memory KMS {operation} command must be preprovisioned") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != 0 or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != 0o550):
            raise ValueError(f"v3 memory KMS {operation} command must be root-owned and group-executable")


def prepare_security(bundle, keyring, host_agent_token_file, panel_config=None):
    """Called by authorized Linux bootstrap. Repeated calls preserve audit/replay state."""
    if os.name != "posix":
        raise ValueError("runtime security provisioning requires Linux")
    if bundle.get("schema_version") not in (1, 2, 3):
        raise ValueError("unsupported runtime security contract")
    if bundle["schema_version"] == 3:
        from .policy_contract import verify_memory_engine_image
        provenance = bundle.get("engine_provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("v3 engine provenance must be an object")
        verify_memory_engine_image(bundle.get("images", {}).get("engine"),
                                   bundle.get("engine_source_revision"),
                                   provenance.get("binary_sha256"),
                                   provenance.get("attestation_sha256"))
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
    for ordinary in bundle["ordinary_paths"].values():
        ordinary = _plain_path(ordinary)
        for path in roots:
            if path == ordinary or path in ordinary.parents or ordinary in path.parents:
                raise ValueError("security storage must remain independent of workload mounts")
    for index, path in enumerate(roots):
        for other in roots[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError("security storage roots must remain independent")
    previous_mask = os.umask(0o077)
    try:
        v3_identity = None
        if bundle["schema_version"] == 3:
            v3_identity = _v3_storage_identity(bundle, uid)
            _check_memory_commands(bundle, gid)
        # A new security root is not proof that the workload mounts are new.
        # The bootstrap places only a panel config in DATA before provisioning.
        if bundle["schema_version"] == 3 and not (paths["security"] / ".initialized").exists():
            state = paths["state"]
            if state.exists() and (not state.is_dir() or any(state.iterdir())):
                raise ValueError("existing state requires authorized encrypted memory migration")
            data = _plain_path(bundle["ordinary_paths"]["DATA"])
            if data.exists():
                if not data.is_dir():
                    raise ValueError("existing workload data requires authorized encrypted memory migration")
                for entry in data.iterdir():
                    info = entry.lstat()
                    if (entry.name != "control-panel-config.json" or not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1):
                        raise ValueError("existing workload data requires authorized encrypted memory migration")
        security = _directory(paths["security"], uid, gid)
        marker = security / ".initialized"
        if marker.exists():
            _check_file(marker, uid)
            previous_version = marker.read_bytes()
            if previous_version not in (b"runtime-security-v1\n", b"runtime-security-v2\n", b"runtime-security-v3\n"):
                raise ValueError("unknown initialized runtime security version")
            previous_number = int(previous_version[len(b"runtime-security-v"):].strip())
            if previous_number == 3 and bundle["schema_version"] != 3:
                raise ValueError("runtime security downgrade would disable hosted memory encryption")
            if bundle["schema_version"] == 3 and previous_number != 3:
                raise ValueError("runtime security v3 requires fresh storage or authorized memory migration")
            if previous_number == 2 and bundle["schema_version"] == 1:
                raise ValueError("runtime security downgrade would disable policy synchronization")
            if previous_number == 3:
                binding = security / "storage-roots.json"
                try:
                    _check_file(binding, uid)
                    if json.loads(binding.read_bytes()) != v3_identity:
                        raise ValueError("v3 workload roots changed; authorized recovery is required")
                except (OSError, json.JSONDecodeError):
                    raise ValueError("v3 workload root binding is missing or invalid") from None
        if bundle["schema_version"] in (2, 3):
            _directory(paths["policy"], uid, gid)
        replay = _directory(paths["replay"], uid, gid)
        anchor = _directory(paths["anchor"], uid, gid)
        panel_auth = _directory(paths["panel_auth"], uid, gid)
        audit_key = security / "audit-hmac-key"
        if audit_key.exists() or audit_key.is_symlink():
            _check_file(audit_key, uid)
            if len(audit_key.read_bytes().strip()) < 32:
                raise ValueError("existing audit key is invalid; authorized recovery is required")
        else:
            if (security / ".initialized").exists() or any(replay.iterdir()) or any(anchor.iterdir()):
                raise ValueError("audit key is missing from initialized storage; authorized recovery is required")
            _write(audit_key, secrets.token_hex(32).encode(), uid, gid)
        from .keyring_lifecycle import install_keyring
        install_keyring(bundle, keyring, initialize=not marker.exists())
        _write(panel_auth / "host-agent-token", host_token, uid, gid)
        config = copy.deepcopy(panel_config or {"version": 1})
        config.setdefault("hosted", {}).update(bundle["panel_hosted"])
        _write(panel_auth / "control-panel-config.json", json.dumps(config).encode(), uid, gid)
        if v3_identity is not None:
            _write(security / "storage-roots.json", json.dumps(v3_identity, sort_keys=True).encode(), uid, gid)
        _write(marker, f"runtime-security-v{bundle['schema_version']}\n".encode(), uid, gid)
        # The engine must create the replay database; an empty placeholder is invalid.
    finally:
        os.umask(previous_mask)
