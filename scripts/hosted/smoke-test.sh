#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

deployment_slug="${HOSTED_DEPLOYMENT_SLUG:-}"
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"
workdir="${HOSTED_SMOKE_WORKDIR:-}"
skip_build=false
keep=false
wait_seconds="${HOSTED_WAIT_SECONDS:-300}"

usage() {
  cat <<'EOF'
Usage:
  smoke-test.sh [--deployment-slug SLUG] [--public-url URL] [--skip-build] [--keep]

Build the hosted images, package a bundle, boot it locally, and verify health.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment-slug)
      deployment_slug="$2"
      shift 2
      ;;
    --public-url)
      public_url="$2"
      shift 2
      ;;
    --workdir)
      workdir="$2"
      shift 2
      ;;
    --skip-build)
      skip_build=true
      shift
      ;;
    --keep)
      keep=true
      shift
      ;;
    --wait-seconds)
      wait_seconds="$2"
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

if [[ -z "$deployment_slug" ]]; then
  deployment_slug="smoke-$(hosted::git_short_sha)"
fi

if [[ -z "$workdir" ]]; then
  workdir="$(mktemp -d "${TMPDIR:-/tmp}/aca-hosted-smoke.XXXXXX")"
fi

bundle_dir="${workdir}/bundle"
install_root="${workdir}/install"
export HOSTED_ALLOW_NONROOT=true
export HOSTED_SKIP_PULL=true

pick_free_port() {
  python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

export HOSTED_ENGINE_PORT="${HOSTED_ENGINE_PORT:-$(pick_free_port)}"
export HOSTED_ACA_PORT="${HOSTED_ACA_PORT:-$(pick_free_port)}"
export HOSTED_CONTROL_PANEL_PORT="${HOSTED_CONTROL_PANEL_PORT:-$(pick_free_port)}"
export HOSTED_HTTP_PORT="${HOSTED_HTTP_PORT:-$(pick_free_port)}"
if [[ -z "$public_url" ]]; then
  public_url="http://127.0.0.1:${HOSTED_HTTP_PORT}"
fi

cleanup_workdir() {
  if [[ "$keep" == true ]]; then
    hosted::log "preserving smoke-test workdir: $workdir"
    return 0
  fi

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    rm -rf "$workdir" >/dev/null 2>&1 || true
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo -n rm -rf "$workdir" >/dev/null 2>&1 && return 0
  fi

  rm -rf "$workdir" >/dev/null 2>&1 || true
  hosted::log "could not fully clean smoke-test workdir: $workdir"
  return 0
}

trap 'status=$?; cleanup_workdir; exit $status' EXIT

if [[ "$skip_build" == false ]]; then
  "${SCRIPT_DIR}/build-images.sh" --load
fi

"${SCRIPT_DIR}/package-bundle.sh" \
  --deployment-slug "$deployment_slug" \
  --install-root "$install_root" \
  --public-url "$public_url" \
  --bundle-dir "$bundle_dir"

hosted::log "starting bootstrap smoke test from ${bundle_dir}"
"${bundle_dir}/bootstrap-server.sh" --bundle-dir "$bundle_dir" --skip-prereqs --wait-seconds "$wait_seconds"

deadline="$((SECONDS + wait_seconds))"

while (( SECONDS < deadline )); do
  if curl -fsS "${public_url%/}/api/system/health" >/dev/null 2>&1; then
    hosted::log "public proxy health check passed."
    break
  fi
  sleep 3
done

if (( SECONDS >= deadline )); then
  hosted::die "public proxy never became healthy"
fi

if [[ "$keep" == false ]]; then
  hosted::as_root docker compose -f "${install_root}/docker-compose.hosted.yml" down --remove-orphans >/dev/null 2>&1 || true
fi

hosted::log "hosted smoke test completed successfully."
