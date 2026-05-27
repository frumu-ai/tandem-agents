#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

usage() {
  cat <<'EOF'
Usage:
  bump-release.sh [current|patch|minor|major]

Update scripts/hosted/release-version.txt and print shell exports for the new
fallback Hosted release version/tag. Repo git tags remain the preferred source
of truth once releases are tagged.
EOF
}

part="${1:-patch}"
case "${part}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

release_version="$(hosted::bump_release_version "${part}")"
release_tag="${HOSTED_RELEASE_TAG:-v${release_version}}"

printf 'export HOSTED_RELEASE_VERSION=%q\n' "${release_version}"
printf 'export HOSTED_RELEASE_TAG=%q\n' "${release_tag}"
