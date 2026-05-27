#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

push=false
load=false
auto_bump_part=""
platforms="${HOSTED_PLATFORMS:-linux/amd64}"

usage() {
  cat <<'EOF'
Usage:
  build-images.sh [--push|--load] [--auto-bump [patch|minor|major]]

  Build the ACA, Tandem engine, Tandem control-panel, KB MCP, and proxy images
  using the hosted release manifest. Default mode loads images locally for smoke
  testing.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push)
      push=true
      shift
      ;;
    --load)
      load=true
      shift
      ;;
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

if [[ "$push" == true && "$load" == true ]]; then
  hosted::die "choose either --push or --load"
fi

if [[ "${HOSTED_ALLOW_RELEASE_OVERRIDE:-false}" != "true" ]]; then
  unset HOSTED_RELEASE_TAG
  unset HOSTED_RELEASE_VERSION
fi

if ! command -v docker >/dev/null 2>&1; then
  hosted::die "docker is required"
fi
docker buildx version >/dev/null 2>&1 || hosted::die "docker buildx is required"

if [[ -n "${auto_bump_part}" ]]; then
  source <("${SCRIPT_DIR}/bump-release.sh" "${auto_bump_part}")
fi
source <("${SCRIPT_DIR}/release-manifest.sh")

public_engine_image_repository="${HOSTED_PUBLIC_ENGINE_IMAGE_REPOSITORY:-${HOSTED_PUBLIC_ENGINE_IMAGE%:*}}"
public_aca_image_repository="${HOSTED_PUBLIC_ACA_IMAGE_REPOSITORY:-${HOSTED_PUBLIC_ACA_IMAGE%:*}}"
engine_image_repository="${HOSTED_ENGINE_IMAGE_REPOSITORY:-${HOSTED_ENGINE_IMAGE%:*}}"
aca_image_repository="${HOSTED_ACA_IMAGE_REPOSITORY:-${HOSTED_ACA_IMAGE%:*}}"
control_panel_image_repository="${HOSTED_CONTROL_PANEL_IMAGE_REPOSITORY:-${HOSTED_CONTROL_PANEL_IMAGE%:*}}"
proxy_image_repository="${HOSTED_PROXY_IMAGE_REPOSITORY:-${HOSTED_PROXY_IMAGE%:*}}"
kb_image_repository="${HOSTED_KB_IMAGE_REPOSITORY:-${HOSTED_KB_IMAGE%:*}}"
proxy_base_image="${HOSTED_PROXY_BASE_IMAGE:-caddy:2-alpine}"

build_common_args=(
  --build-arg "HOST_UID=${HOSTED_HOST_UID}"
  --build-arg "HOST_GID=${HOSTED_HOST_GID}"
  --build-arg "TANDEM_RELEASE_VERSION=${HOSTED_TANDEM_RELEASE_VERSION}"
  --build-arg "TANDEM_ENGINE_RELEASE_VERSION=${HOSTED_TANDEM_ENGINE_RELEASE_VERSION}"
  --build-arg "TANDEM_CONTROL_PANEL_RELEASE_VERSION=${HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION}"
  --build-arg "VERSION=${HOSTED_RELEASE_TAG}"
  --build-arg "VCS_REF=${HOSTED_GIT_SHA}"
  --build-arg "BUILD_DATE=${HOSTED_BUILD_DATE}"
)

output_args=()
if [[ "$push" == true ]]; then
  output_args+=(--push)
elif [[ "$load" == true ]]; then
  output_args+=(--load)
else
  output_args+=(--load)
fi

cache_root="${HOSTED_BUILD_CACHE_ROOT:-${HOSTED_INSTALL_ROOT:-${HOSTED_REPO_ROOT}}/.docker-build-cache}"
mkdir -p "${cache_root}"

build_image() {
  local image_name="$1"
  local dockerfile="$2"
  local tag_base="$3"
  shift 3

  local cache_args=(
    --cache-to "type=local,dest=${cache_root}/${image_name},mode=max"
  )
  if [[ -f "${cache_root}/${image_name}/index.json" ]]; then
    cache_args+=(--cache-from "type=local,src=${cache_root}/${image_name}")
  fi
  if [[ "$push" == true ]]; then
    if docker buildx imagetools inspect "${tag_base}:buildcache" >/dev/null 2>&1; then
      cache_args+=(--cache-from "type=registry,ref=${tag_base}:buildcache")
    fi
    cache_args+=(
      --cache-to "type=registry,ref=${tag_base}:buildcache,mode=max,oci-mediatypes=true"
    )
  fi

  hosted::log "building ${image_name}:${HOSTED_RELEASE_TAG}"
  docker buildx build \
    --platform "${platforms}" \
    "${build_common_args[@]}" \
    "${cache_args[@]}" \
    "$@" \
    --tag "${tag_base}:${HOSTED_RELEASE_TAG}" \
    --tag "${tag_base}:sha-${HOSTED_GIT_SHORT_SHA}" \
    "${output_args[@]}" \
    --file "${dockerfile}" \
    "${HOSTED_REPO_ROOT}"
}

build_image "engine" "config/Dockerfile.engine" "${public_engine_image_repository}" --build-arg "TANDEM_ENGINE_PACKAGE=@frumu/tandem"
build_image "engine-enterprise" "config/Dockerfile.engine" "${engine_image_repository}" --build-arg "TANDEM_ENGINE_PACKAGE=@frumu/tandem-enterprise"
build_image "aca" "config/Dockerfile" "${public_aca_image_repository}" --build-arg "TANDEM_ENGINE_PACKAGE=@frumu/tandem"
build_image "aca-enterprise" "config/Dockerfile" "${aca_image_repository}" --build-arg "TANDEM_ENGINE_PACKAGE=@frumu/tandem-enterprise"
build_image "control-panel" "config/Dockerfile.control-panel" "${control_panel_image_repository}"
build_image "proxy" "config/Dockerfile.proxy" "${proxy_image_repository}" --build-arg "HOSTED_PROXY_BASE_IMAGE=${proxy_base_image}"
build_image "kb-mcp" "config/Dockerfile.kb" "${kb_image_repository}"
