#!/usr/bin/env python3
"""Shell adapter for the same installable module consumed by tandem-web."""
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))  # packaged bundle
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "packages" / "runtime-bundle"))

from tandem_runtime_bundle import build_security_bundle  # noqa: E402
from tandem_runtime_bundle.prepare import prepare_security  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["render", "prepare"])
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        bundle = build_security_bundle(os.environ)
        if args.action == "prepare":
            keyring_path = os.environ.get("HOSTED_CONTEXT_KEYRING_SOURCE_FILE")
            token_path = os.environ.get("HOSTED_HOST_AGENT_TOKEN_SOURCE_FILE")
            if not keyring_path or not token_path:
                raise ValueError("HOSTED_CONTEXT_KEYRING_SOURCE_FILE and HOSTED_HOST_AGENT_TOKEN_SOURCE_FILE are required")
            with open(keyring_path, encoding="utf-8") as handle:
                keyring = json.load(handle)
            with open(os.environ["HOSTED_CONTROL_PANEL_CONFIG_FILE"], encoding="utf-8") as handle:
                panel_config = json.load(handle)
            prepare_security(bundle, keyring, token_path, panel_config)
            if bundle["schema_version"] == 2:
                from tandem_runtime_bundle.policy_service import install_policy_service
                install_policy_service(bundle, Path(os.environ["HOSTED_INSTALL_ROOT"]) / "hosted-agent",
                                       token_path, activate=True)
            print("Runtime security storage prepared; no key material emitted.")
        elif args.output:
            Path(args.output).write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        else:
            print(json.dumps(bundle, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Runtime security configuration rejected: {exc}\n")


if __name__ == "__main__":
    main()
