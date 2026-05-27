#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

auto_bump_part="${HOSTED_AUTO_BUMP_PART:-}"
build_args=()

usage() {
  cat <<'EOF'
Usage:
  publish-images.sh [--auto-bump [patch|minor|major]] [--no-bump]

Push Hosted images to GHCR. Defaults to publishing the current Hosted release
version without bumping.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto-bump)
      auto_bump_part="${2:-patch}"
      if [[ $# -gt 1 && ! "$2" =~ ^- ]]; then
        shift 2
      else
        auto_bump_part="patch"
        shift
      fi
      ;;
    --auto-bump=*)
      auto_bump_part="${1#*=}"
      shift
      ;;
    --no-bump)
      auto_bump_part=""
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      build_args+=("$1")
      shift
      ;;
  esac
done

if [[ -n "${HOSTED_REGISTRY_USERNAME:-}" && -n "${HOSTED_REGISTRY_TOKEN:-}" ]]; then
  hosted::log "logging in to ghcr.io as ${HOSTED_REGISTRY_USERNAME}"
  printf '%s\n' "${HOSTED_REGISTRY_TOKEN}" | docker login ghcr.io -u "${HOSTED_REGISTRY_USERNAME}" --password-stdin >/dev/null
fi

if [[ "${HOSTED_ALLOW_RELEASE_OVERRIDE:-false}" != "true" ]]; then
  unset HOSTED_RELEASE_TAG
  unset HOSTED_RELEASE_VERSION
fi

if [[ -n "${auto_bump_part}" ]]; then
  build_args+=(--auto-bump "${auto_bump_part}")
fi

exec "${SCRIPT_DIR}/build-images.sh" --push "${build_args[@]}"
