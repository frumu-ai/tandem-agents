#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

output=""

usage() {
  cat <<'EOF'
Usage:
  render-compose.sh [--output FILE]

Render the hosted docker-compose bundle for a single customer deployment.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
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

source <("${SCRIPT_DIR}/release-manifest.sh")

if [[ "${HOSTED_STORAGE_PROFILE:-local}" != "local" ]]; then
  hosted::die "hosted compose v1 only supports storage.profile=local"
fi

if [[ "${HOSTED_ENABLE_OUTBOX:-false}" == "true" ]]; then
  hosted::die "hosted compose v1 does not bundle a separate outbox service yet"
fi

compose="$(
PYTHONPATH="${SCRIPT_DIR}/../../packages/runtime-bundle:${SCRIPT_DIR}${PYTHONPATH:+:$PYTHONPATH}" python3 "${SCRIPT_DIR}/compose.py"
)"

if [[ -n "$output" ]]; then
  mkdir -p "$(dirname "$output")"
  printf '%s\n' "$compose" > "$output"
else
  printf '%s\n' "$compose"
fi
