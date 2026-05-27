#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

channel="stable"
published="true"
release_notes=""

usage() {
  cat <<'EOF'
Usage:
  release-payload.sh [--channel stable] [--published true|false] [--release-notes "notes"]

Print a JSON payload suitable for POST /api/v1/admin/hosted/releases in
tandem-web, using the current hosted release manifest.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)
      channel="${2:?--channel requires a value}"
      shift 2
      ;;
    --published)
      published="${2:?--published requires true or false}"
      shift 2
      ;;
    --release-notes)
      release_notes="${2:?--release-notes requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [[ "${HOSTED_ALLOW_RELEASE_OVERRIDE:-false}" != "true" ]]; then
  unset HOSTED_RELEASE_TAG
  unset HOSTED_RELEASE_VERSION
fi

source <("${SCRIPT_DIR}/release-manifest.sh")

python3 - "${channel}" "${published}" "${release_notes}" <<'PY'
import json
import os
import sys

channel = sys.argv[1].strip()
published = sys.argv[2].strip().lower() == "true"
release_notes = sys.argv[3].strip()

payload = {
    "version": os.environ["HOSTED_RELEASE_TAG"],
    "channel": channel,
    "engine_image_ref": os.environ["HOSTED_ENGINE_IMAGE"],
    "aca_image_ref": os.environ["HOSTED_ACA_IMAGE"],
    "control_panel_image_ref": os.environ["HOSTED_CONTROL_PANEL_IMAGE"],
    "proxy_image_ref": os.environ["HOSTED_PROXY_IMAGE"],
    "kb_image_ref": os.environ["HOSTED_KB_IMAGE"],
    "manifest_json": {
        "release_version": os.environ["HOSTED_RELEASE_VERSION"],
        "release_tag": os.environ["HOSTED_RELEASE_TAG"],
        "git_sha": os.environ["HOSTED_GIT_SHA"],
        "git_short_sha": os.environ["HOSTED_GIT_SHORT_SHA"],
        "git_ref": os.environ["HOSTED_GIT_REF"],
        "build_date": os.environ["HOSTED_BUILD_DATE"],
        "tandem_release_version": os.environ["HOSTED_TANDEM_RELEASE_VERSION"],
        "tandem_engine_release_version": os.environ["HOSTED_TANDEM_ENGINE_RELEASE_VERSION"],
        "tandem_control_panel_release_version": os.environ["HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION"],
        "public_engine_image_ref": os.environ.get("HOSTED_PUBLIC_ENGINE_IMAGE"),
        "public_aca_image_ref": os.environ.get("HOSTED_PUBLIC_ACA_IMAGE"),
        "hosted_engine_package": "@frumu/tandem-enterprise",
        "public_engine_package": "@frumu/tandem",
    },
    "release_notes": release_notes or None,
    "published": published,
}

print(json.dumps(payload, indent=2))
PY
