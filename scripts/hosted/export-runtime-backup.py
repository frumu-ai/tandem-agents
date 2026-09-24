#!/usr/bin/env python3
"""Operator-only v3 encrypted off-site export. No restore or rebind command."""

from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "packages" / "runtime-bundle"))

from tandem_runtime_bundle.backup_cli import main  # noqa: E402


if __name__ == "__main__":
    main()
