"""Operator-staged verifier rotation. Private signing keys never enter the host."""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .contract import validate_keyring
from .prepare import _check_file, _plain_path, _write

MAX_DOCUMENT_BYTES = 256 * 1024
FIELDS = {"public_key", "purpose", "deployment_id", "organization_id", "allowed_audiences",
          "status", "not_before_ms", "not_after_ms", "allowed_resource_scope_prefixes", "kms_key_reference"}
STATUSES = {"active": 0, "retired": 1, "revoked": 2}


def parse_document(raw):
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("keyring document is too large")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate keyring field or key id")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def fingerprint(document):
    """Digest of the supplied public document, distinct from engine snapshot metadata."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def validate_document(document, deployment_id, organization_id):
    validate_keyring(document, deployment_id, organization_id)
    if len(json.dumps(document).encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("keyring document is too large")
    for kid, entry in document.items():
        if kid != kid.strip() or any(ord(char) < 33 for char in kid) or len(kid) > 200:
            raise ValueError("invalid key id")
        if set(entry) - FIELDS:
            raise ValueError("unexpected keyring metadata field")
        for field in ("not_before_ms", "not_after_ms"):
            value = entry.get(field)
            if value is not None and (type(value) is not int or not 0 <= value < 2**64):
                raise ValueError("key validity must be an unsigned millisecond timestamp")
        if (entry.get("not_before_ms") is not None and entry.get("not_after_ms") is not None
                and entry["not_after_ms"] <= entry["not_before_ms"]):
            raise ValueError("key validity window is empty")
        prefixes = entry.get("allowed_resource_scope_prefixes", [])
        if not isinstance(prefixes, list) or any(not isinstance(value, str) for value in prefixes):
            raise ValueError("invalid resource scopes")
        reference = entry.get("kms_key_reference")
        if reference is not None and not isinstance(reference, str):
            raise ValueError("invalid public KMS reference")


def validate_transition(previous, proposed):
    for kid, before in previous.items():
        after = proposed.get(kid)
        if after is None:
            raise ValueError("retain retired and revoked key metadata; key removal is not a rotation")
        if STATUSES[after["status"]] < STATUSES[before["status"]]:
            raise ValueError("retired or revoked keys cannot be reactivated")
        def immutable(entry):
            result = copy.deepcopy(entry)
            for name in ("status", "not_before_ms", "not_after_ms"):
                result.pop(name, None)
            raw = result["public_key"]
            result["public_key"] = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_")
            return result
        if immutable(before) != immutable(after):
            raise ValueError("existing key material and scope are immutable; use a new key id")
        if ((after.get("not_before_ms") or 0) < (before.get("not_before_ms") or 0)
                or (after.get("not_after_ms") if after.get("not_after_ms") is not None else 2**64)
                > (before.get("not_after_ms") if before.get("not_after_ms") is not None else 2**64)):
            raise ValueError("existing key validity cannot be extended")


def install_keyring(bundle, proposed, *, expected_fingerprint=None, initialize=False, apply=True):
    """Serialize bootstrap and rotations; one atomic document retains lifecycle tombstones.

    A receipt confirms the durable file only. The existing authorized runtime
    reload (or a controlled restart) must still acknowledge its verifier snapshot.
    """
    if os.name != "posix":
        raise ValueError("keyring installation requires Linux")
    import fcntl

    uid, gid = bundle["uid"], bundle["gid"]
    if os.geteuid() not in (0, uid):
        raise ValueError("keyring installation requires the configured host operator")
    validate_document(proposed, bundle["deployment_id"], bundle["organization_id"])
    now = int(time.time() * 1000)
    if not any(entry["status"] == "active" and (entry.get("not_before_ms") or 0) <= now
               and (entry.get("not_after_ms") is None or now < entry["not_after_ms"])
               for entry in proposed.values()):
        raise ValueError("keyring requires a currently valid active key")
    directory = _plain_path(bundle["host_paths"]["security"])
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("invalid security directory ownership or mode")
    path = directory / "context-keyring.json"
    lock = directory / ".context-keyring.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("invalid keyring lock")
        if info.st_uid != uid:
            if info.st_uid != os.geteuid() or info.st_size != 0:
                raise ValueError("invalid keyring lock owner")
            os.fchown(descriptor, uid, gid)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        previous = None
        if path.exists() or path.is_symlink():
            _check_file(path, uid)
            previous = parse_document(path.read_bytes())
            validate_document(previous, bundle["deployment_id"], bundle["organization_id"])
        elif not initialize or (directory / ".initialized").exists():
            raise ValueError("initialized keyring is missing; authorized recovery is required")
        before = fingerprint(previous) if previous is not None else None
        after = fingerprint(proposed)
        if before == after:
            if apply:
                # Retry also completes durability after a previous directory
                # fsync failure whose atomic replace may already be visible.
                _write(path, json.dumps(proposed, sort_keys=True).encode(), uid, gid)
            return {"status": "already_staged", "document_sha256": after, "runtime_reload_required": True}
        if expected_fingerprint is not None and expected_fingerprint != before:
            raise ValueError("keyring changed since preview; read and review the current document")
        if previous is not None:
            validate_transition(previous, proposed)
        if apply:
            _write(path, json.dumps(proposed, sort_keys=True).encode(), uid, gid)
        return {"status": "staged" if apply else "preview", "previous_document_sha256": before,
                "document_sha256": after, "runtime_reload_required": True}
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description="Preview or stage an operator-reviewed public verifier keyring")
    parser.add_argument("--bundle", required=True, help="Root-owned runtime-security.json copy")
    parser.add_argument("--keyring", required=True, help="Root-owned proposed public metadata JSON")
    parser.add_argument("--expected", required=True, help="SHA-256 of current canonical public document")
    parser.add_argument("--apply", action="store_true", help="Write the validated document; otherwise preview only")
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("run as the authorized Linux host operator")
    from .policy_sync import _operator_file
    try:
        bundle = parse_document(_operator_file(args.bundle, MAX_DOCUMENT_BYTES))
        proposed = parse_document(_operator_file(args.keyring, MAX_DOCUMENT_BYTES))
        if bundle.get("schema_version") not in (1, 2):
            raise ValueError("unsupported runtime security contract")
        result = install_keyring(bundle, proposed, expected_fingerprint=args.expected, apply=args.apply)
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(1, "Keyring staging rejected. Check scope, lifecycle, ownership and expected fingerprint.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
