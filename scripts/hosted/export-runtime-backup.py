#!/usr/bin/env python3
"""Operator-only v3 encrypted off-site export. No restore or rebind command."""

import argparse
import json
import os
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "packages" / "runtime-bundle"))

from tandem_runtime_bundle.backup_commands import BackupKms, OffsiteUploader  # noqa: E402
from tandem_runtime_bundle.backup_export import export_backup  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Export an initialized v3 host to private off-site storage")
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--backup-kms-command", required=True)
    parser.add_argument("--backup-key-id", required=True)
    parser.add_argument("--backup-key-version", required=True)
    parser.add_argument("--offsite-uploader-command", required=True)
    parser.add_argument("--offsite-host", required=True)
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.exit(1, "Backup export requires a Linux root operator.\n")
    try:
        kms = BackupKms(args.backup_kms_command, args.backup_key_id, args.backup_key_version)
        uploader = OffsiteUploader(args.offsite_uploader_command, args.offsite_host)
        result = export_backup(args.install_root, args.staging_root, kms, uploader)
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        parser.exit(1, f"Backup export rejected: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
