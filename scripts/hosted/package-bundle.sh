#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

deployment_slug="${HOSTED_DEPLOYMENT_SLUG:-}"
install_root="${HOSTED_INSTALL_ROOT:-}"
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"
bundle_dir="${HOSTED_BUNDLE_DIR:-}"
archive_file="${HOSTED_BUNDLE_ARCHIVE:-}"
bundle_url="${HOSTED_BUNDLE_URL:-}"
bundle_sha256="${HOSTED_BUNDLE_SHA256:-}"
deployment_name="${HOSTED_DEPLOYMENT_NAME:-}"
create_cloud_init=false

usage() {
  cat <<'EOF'
Usage:
  package-bundle.sh [--deployment-slug SLUG] [--install-root PATH] [--public-url URL]
                    [--bundle-dir PATH] [--archive PATH] [--bundle-url URL] [--bundle-sha256 SHA256]

Render the hosted deployment bundle and archive it for upload or cloud-init bootstrapping.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment-slug)
      deployment_slug="$2"
      shift 2
      ;;
    --install-root)
      install_root="$2"
      shift 2
      ;;
    --public-url)
      public_url="$2"
      shift 2
      ;;
    --bundle-dir)
      bundle_dir="$2"
      shift 2
      ;;
    --archive)
      archive_file="$2"
      shift 2
      ;;
    --bundle-url)
      bundle_url="$2"
      create_cloud_init=true
      shift 2
      ;;
    --bundle-sha256)
      bundle_sha256="$2"
      create_cloud_init=true
      shift 2
      ;;
    --deployment-name)
      deployment_name="$2"
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
  hosted::die "--deployment-slug or HOSTED_DEPLOYMENT_SLUG is required"
fi

if [[ -z "$public_url" ]]; then
  hosted::die "--public-url or HOSTED_CONTROL_PANEL_PUBLIC_URL is required"
fi

if [[ -n "$bundle_sha256" && -z "$bundle_url" ]]; then
  hosted::die "--bundle-sha256 requires --bundle-url"
fi

if [[ -z "$install_root" ]]; then
  install_root="/srv/tandem/${deployment_slug}"
fi

if [[ -z "$deployment_name" ]]; then
  deployment_name="$deployment_slug"
fi

if [[ -z "$bundle_dir" ]]; then
  bundle_dir="$(hosted::bundle_dir)"
fi

if [[ -z "$archive_file" ]]; then
  archive_file="${bundle_dir}.tar.gz"
fi

hosted::log "rendering hosted bundle into ${bundle_dir}"
mkdir -p "$bundle_dir"

export HOSTED_DEPLOYMENT_SLUG="$deployment_slug"
export HOSTED_INSTALL_ROOT="$install_root"
export HOSTED_CONTROL_PANEL_PUBLIC_URL="$public_url"
export HOSTED_BUNDLE_DIR="$bundle_dir"

source <("${SCRIPT_DIR}/release-manifest.sh")

"${SCRIPT_DIR}/render-runtime-env.sh" --deployment-slug "$deployment_slug" --install-root "$install_root" --public-url "$public_url" --output "${bundle_dir}/hosted.env"

set -a
# shellcheck disable=SC1090
source "${bundle_dir}/hosted.env"
set +a

"${SCRIPT_DIR}/render-control-panel-config.sh" --deployment-name "$deployment_name" --public-url "$public_url" --output "${bundle_dir}/control-panel-config.json"
"${SCRIPT_DIR}/render-compose.sh" --output "${bundle_dir}/docker-compose.hosted.yml"
"${SCRIPT_DIR}/release-manifest.sh" > "${bundle_dir}/release-manifest.env"

python3 - "${bundle_dir}/release-manifest.json" <<'PY'
import json
import os
import sys

target = sys.argv[1]
data = {
    "release_tag": os.environ["HOSTED_RELEASE_TAG"],
    "git_sha": os.environ["HOSTED_GIT_SHA"],
    "git_short_sha": os.environ["HOSTED_GIT_SHORT_SHA"],
    "git_ref": os.environ["HOSTED_GIT_REF"],
    "build_date": os.environ["HOSTED_BUILD_DATE"],
    "tandem_release_version": os.environ["HOSTED_TANDEM_RELEASE_VERSION"],
    "tandem_engine_release_version": os.environ["HOSTED_TANDEM_ENGINE_RELEASE_VERSION"],
    "tandem_control_panel_release_version": os.environ["HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION"],
    "image_namespace": os.environ["HOSTED_IMAGE_NAMESPACE"],
    "engine_image_repository": os.environ["HOSTED_ENGINE_IMAGE_REPOSITORY"],
    "engine_image": os.environ["HOSTED_ENGINE_IMAGE"],
    "aca_image_repository": os.environ["HOSTED_ACA_IMAGE_REPOSITORY"],
    "aca_image": os.environ["HOSTED_ACA_IMAGE"],
    "control_panel_image_repository": os.environ["HOSTED_CONTROL_PANEL_IMAGE_REPOSITORY"],
    "control_panel_image": os.environ["HOSTED_CONTROL_PANEL_IMAGE"],
    "proxy_image_repository": os.environ["HOSTED_PROXY_IMAGE_REPOSITORY"],
    "proxy_image": os.environ["HOSTED_PROXY_IMAGE"],
    "kb_image_repository": os.environ["HOSTED_KB_IMAGE_REPOSITORY"],
    "kb_image": os.environ["HOSTED_KB_IMAGE"],
    "kb_admin_url": os.environ.get("HOSTED_KB_ADMIN_URL", ""),
    "kb_default_collection_id": os.environ.get("HOSTED_KB_DEFAULT_COLLECTION_ID", ""),
    "deployment_slug": os.environ["HOSTED_DEPLOYMENT_SLUG"],
    "install_root": os.environ["HOSTED_INSTALL_ROOT"],
    "bundle_dir": os.environ["HOSTED_BUNDLE_DIR"],
    "bundle_archive": os.environ.get("HOSTED_BUNDLE_ARCHIVE", ""),
    "public_url": os.environ.get("HOSTED_CONTROL_PANEL_PUBLIC_URL", ""),
}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
PY

stage_exec() {
  local source_path="$1"
  local target_path="$2"
  install -m 0755 "$source_path" "$target_path"
}

stage_copy() {
  local source_path="$1"
  local target_path="$2"
  install -m 0644 "$source_path" "$target_path"
}

stage_exec "${SCRIPT_DIR}/bootstrap-server.sh" "${bundle_dir}/bootstrap-server.sh"
stage_exec "${SCRIPT_DIR}/install-prereqs.sh" "${bundle_dir}/install-prereqs.sh"
stage_exec "${SCRIPT_DIR}/generate-secrets.sh" "${bundle_dir}/generate-secrets.sh"
stage_exec "${SCRIPT_DIR}/render-runtime-env.sh" "${bundle_dir}/render-runtime-env.sh"
stage_exec "${SCRIPT_DIR}/render-control-panel-config.sh" "${bundle_dir}/render-control-panel-config.sh"
stage_exec "${SCRIPT_DIR}/render-compose.sh" "${bundle_dir}/render-compose.sh"
stage_exec "${SCRIPT_DIR}/render-caddyfile.sh" "${bundle_dir}/render-caddyfile.sh"
stage_exec "${SCRIPT_DIR}/render-cloud-init.sh" "${bundle_dir}/render-cloud-init.sh"
stage_exec "${SCRIPT_DIR}/host-hardening.sh" "${bundle_dir}/host-hardening.sh"
stage_exec "${SCRIPT_DIR}/release-manifest.sh" "${bundle_dir}/release-manifest.sh"
stage_copy "${SCRIPT_DIR}/lib.sh" "${bundle_dir}/lib.sh"

"${SCRIPT_DIR}/render-caddyfile.sh" --public-url "$public_url" --upstream "${HOSTED_CONTROL_PANEL_UPSTREAM:-http://tandem-control-panel:${HOSTED_CONTROL_PANEL_PORT:-39734}}" --output "${bundle_dir}/Caddyfile"

cat > "${bundle_dir}/README.md" <<EOF
Hosted Bundle

Deployment: ${deployment_name}
Slug: ${deployment_slug}
Install root: ${install_root}
Public URL: ${public_url}

This bundle includes:

- the VM bootstrap script
- host prereq and hardening scripts
- the rendered compose file
- the deployment-local Caddy front door config
- the cloud-init generator for Hetzner handoff

To bootstrap this bundle on a server, run:

  ./bootstrap-server.sh

The bundle archive is:

  ${archive_file}
EOF

if [[ "$create_cloud_init" == true ]]; then
  "${SCRIPT_DIR}/render-cloud-init.sh" --bundle-url "$bundle_url" --bundle-sha256 "$bundle_sha256" --deployment-slug "$deployment_slug" --public-url "$public_url" --output "${bundle_dir}/cloud-init.user-data.sh"
  chmod +x "${bundle_dir}/cloud-init.user-data.sh"
fi

if [[ -f "$archive_file" ]]; then
  rm -f "$archive_file"
fi

tar -C "$bundle_dir" -czf "$archive_file" .
sha256sum "$archive_file" | tee "${archive_file}.sha256" >/dev/null

hosted::log "bundle archived at ${archive_file}"
hosted::log "sha256: $(cut -d' ' -f1 "${archive_file}.sha256")"
