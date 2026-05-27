#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  build-containers.sh [--build|--up]

By default, this resolves the latest Tandem engine and control panel releases
unless TANDEM_ENGINE_RELEASE_VERSION or TANDEM_CONTROL_PANEL_RELEASE_VERSION is
already pinned to a specific version. TANDEM_ENGINE_PACKAGE selects the engine
npm package and defaults to @frumu/tandem; hosted builds can set it to
@frumu/tandem-enterprise. TANDEM_RELEASE_VERSION remains a backwards-compatible
pin for both packages.
EOF
}

resolve_release_version() {
  local package_name="$1"
  local requested="$2"

  if [[ -n "$requested" && "$requested" != "latest" ]]; then
    printf '%s\n' "$requested"
    return 0
  fi

  npm view "$package_name" version
}

action="up"
case "${1:-}" in
  "")
    ;;
  --build)
    action="build"
    ;;
  --up)
    action="up"
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

engine_package="${TANDEM_ENGINE_PACKAGE:-@frumu/tandem}"
engine_release_version="$(resolve_release_version "$engine_package" "${TANDEM_ENGINE_RELEASE_VERSION:-${TANDEM_RELEASE_VERSION:-}}")"
control_panel_release_version="$(resolve_release_version "@frumu/tandem-panel" "${TANDEM_CONTROL_PANEL_RELEASE_VERSION:-${TANDEM_RELEASE_VERSION:-}}")"

export TANDEM_ENGINE_PACKAGE="$engine_package"
export TANDEM_ENGINE_RELEASE_VERSION="$engine_release_version"
export TANDEM_CONTROL_PANEL_RELEASE_VERSION="$control_panel_release_version"
export TANDEM_RELEASE_VERSION="${TANDEM_RELEASE_VERSION:-$engine_release_version}"

echo "Using TANDEM_ENGINE_PACKAGE=${TANDEM_ENGINE_PACKAGE}"
echo "Using TANDEM_ENGINE_RELEASE_VERSION=${TANDEM_ENGINE_RELEASE_VERSION}"
echo "Using TANDEM_CONTROL_PANEL_RELEASE_VERSION=${TANDEM_CONTROL_PANEL_RELEASE_VERSION}"

cd "$ROOT_DIR"
if [[ "$action" == "build" ]]; then
  exec docker compose build tandem-engine tandem-control-panel aca tandem-kb-mcp
fi

exec docker compose up -d --build tandem-engine tandem-control-panel aca tandem-kb-mcp
