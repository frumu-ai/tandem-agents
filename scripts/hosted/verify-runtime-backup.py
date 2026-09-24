#!/usr/bin/env python3
"""Read-only candidate preflight. Never extracts files or changes root bindings."""

import argparse
import json
import os
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "packages" / "runtime-bundle"))

from tandem_runtime_bundle.backup_commands import BackupKms  # noqa: E402
from tandem_runtime_bundle.backup_recovery_authority import (  # noqa: E402
    LatestKeyringAuthority, MemoryKmsChallenge, RecoveryAuthority)
from tandem_runtime_bundle.backup_verify import verify_recovery_candidate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("backup-id", "organization-id", "deployment-id",
                 "manifest-path", "archive-path", "anchors-path",
                 "backup-kms-command", "backup-key-id", "backup-key-version",
                 "recovery-authority-command", "keyring-authority-command",
                 "offsite-host", "memory-kms-command"):
        parser.add_argument("--" + name, required=True)
    values = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("recovery preflight requires a Linux root operator")
    scope = {key: getattr(values, key) for key in (
        "backup_id", "organization_id", "deployment_id")}
    result = verify_recovery_candidate(
        values.manifest_path, values.archive_path, values.anchors_path, scope,
        RecoveryAuthority(values.recovery_authority_command, values.offsite_host),
        LatestKeyringAuthority(values.keyring_authority_command),
        BackupKms(values.backup_kms_command, values.backup_key_id,
                  values.backup_key_version),
        MemoryKmsChallenge(values.memory_kms_command))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
