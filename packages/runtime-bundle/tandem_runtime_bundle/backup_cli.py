"""Operator-only entry point for the export-only v3 backup command."""

import argparse
import json
import os

from .backup_commands import BackupKms, OffsiteUploader
from .backup_export import export_backup


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export an initialized v3 host to private off-site storage"
    )
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--backup-kms-command", required=True)
    parser.add_argument("--backup-key-id", required=True)
    parser.add_argument("--backup-key-version", required=True)
    parser.add_argument("--offsite-uploader-command", required=True)
    parser.add_argument("--offsite-host", required=True)
    args = parser.parse_args(argv)
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
