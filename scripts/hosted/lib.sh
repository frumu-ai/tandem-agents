#!/usr/bin/env bash

if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "hosted scripts require bash" >&2
  exit 1
fi

hosted::root_dir() {
  if [[ -n "${HOSTED_REPO_ROOT:-}" ]]; then
    cd "${HOSTED_REPO_ROOT}" && pwd
    return 0
  fi

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if [[ -f "${script_dir}/hosted.env" || -f "${script_dir}/docker-compose.hosted.yml" ]]; then
    cd "${script_dir}" && pwd
    return 0
  fi

  cd "${script_dir}/../.." && pwd
}

HOSTED_REPO_ROOT="$(hosted::root_dir)"

hosted::log() {
  printf '%s\n' "$*" >&2
}

hosted::die() {
  hosted::log "error: $*"
  exit 1
}

hosted::require() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || hosted::die "required command not found: ${cmd}"
}

hosted::trim() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

hosted::as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
    return 0
  fi
  if [[ "${HOSTED_ALLOW_NONROOT:-false}" == "true" ]]; then
    "$@"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo -n "$@"
    return 0
  fi
  hosted::die "root privileges are required and sudo is not available: $*"
}

hosted::git_sha() {
  git -C "${HOSTED_REPO_ROOT}" rev-parse HEAD
}

hosted::git_short_sha() {
  git -C "${HOSTED_REPO_ROOT}" rev-parse --short=12 HEAD
}

hosted::git_ref() {
  local ref
  ref="$(git -C "${HOSTED_REPO_ROOT}" branch --show-current 2>/dev/null || true)"
  if [[ -n "$ref" ]]; then
    printf '%s\n' "$ref"
    return 0
  fi
  ref="$(git -C "${HOSTED_REPO_ROOT}" describe --tags --exact-match 2>/dev/null || true)"
  if [[ -n "$ref" ]]; then
    printf '%s\n' "$ref"
    return 0
  fi
  printf '%s\n' "detached"
}

hosted::build_date() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

hosted::release_version_file() {
  printf '%s/scripts/hosted/release-version.txt\n' "${HOSTED_REPO_ROOT}"
}

hosted::validate_release_version() {
  local version="${1:-}"
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || hosted::die \
    "HOSTED_RELEASE_VERSION must be semver-like (major.minor.patch), got: ${version:-<empty>}"
}

hosted::tandem_release_repo() {
  if [[ -n "${HOSTED_TANDEM_RELEASE_REPO:-}" ]]; then
    printf '%s\n' "$(hosted::trim "${HOSTED_TANDEM_RELEASE_REPO}")"
    return 0
  fi
  printf '%s\n' "frumu-ai/tandem"
}

hosted::latest_repo_release_tag() {
  local tag
  local repo
  repo="$(hosted::tandem_release_repo)"

  if command -v gh >/dev/null 2>&1; then
    tag="$(
      gh api "repos/${repo}/releases/latest" --jq '.tag_name // empty' 2>/dev/null || true
    )"
    if [[ -n "${tag}" ]]; then
      printf '%s\n' "${tag}"
      return 0
    fi
  fi

  if command -v git >/dev/null 2>&1; then
    tag="$(
      git ls-remote --tags --refs "https://github.com/${repo}.git" 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null \
        | awk -F/ '{print $3}' \
        | sort -V \
        | tail -n1
    )"
    if [[ -n "${tag}" ]]; then
      printf '%s\n' "${tag}"
      return 0
    fi
  fi

  return 1
}

hosted::latest_repo_release_version() {
  local tag
  tag="$(hosted::latest_repo_release_tag 2>/dev/null || true)"
  if [[ -n "${tag}" ]]; then
    printf '%s\n' "${tag#v}"
    return 0
  fi
  return 1
}

hosted::release_version() {
  local requested="${HOSTED_RELEASE_VERSION:-}"
  local file_path
  file_path="$(hosted::release_version_file)"

  if [[ -n "${requested}" ]]; then
    requested="$(hosted::trim "${requested}")"
    hosted::validate_release_version "${requested}"
    printf '%s\n' "${requested}"
    return 0
  fi

  if requested="$(hosted::latest_repo_release_version 2>/dev/null || true)" && [[ -n "${requested}" ]]; then
    hosted::validate_release_version "${requested}"
    printf '%s\n' "${requested}"
    return 0
  fi

  if [[ -f "${file_path}" ]]; then
    requested="$(hosted::trim "$(cat "${file_path}")")"
    hosted::validate_release_version "${requested}"
    printf '%s\n' "${requested}"
    return 0
  fi

  printf '%s\n' "0.1.0"
}

hosted::write_release_version() {
  local version="${1:-}"
  local file_path
  file_path="$(hosted::release_version_file)"
  hosted::validate_release_version "${version}"
  printf '%s\n' "${version}" > "${file_path}"
}

hosted::bump_release_version() {
  local part="${1:-patch}"
  local current
  local major
  local minor
  local patch
  local next

  current="$(hosted::release_version)"
  IFS=. read -r major minor patch <<< "${current}"

  case "${part}" in
    patch)
      patch=$((patch + 1))
      ;;
    minor)
      minor=$((minor + 1))
      patch=0
      ;;
    major)
      major=$((major + 1))
      minor=0
      patch=0
      ;;
    current)
      ;;
    *)
      hosted::die "release bump part must be one of: current, patch, minor, major"
      ;;
  esac

  next="${major}.${minor}.${patch}"
  hosted::write_release_version "${next}"
  printf '%s\n' "${next}"
}

hosted::release_tag() {
  if [[ -n "${HOSTED_RELEASE_TAG:-}" ]]; then
    printf '%s\n' "$(hosted::trim "${HOSTED_RELEASE_TAG}")"
    return 0
  fi
  if local_tag="$(hosted::latest_repo_release_tag 2>/dev/null || true)"; [[ -n "${local_tag:-}" ]]; then
    printf '%s\n' "${local_tag}"
    return 0
  fi
  printf 'v%s\n' "$(hosted::release_version)"
}

hosted::repo_slug_from_remote() {
  local remote
  remote="$(git -C "${HOSTED_REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
  if [[ "$remote" =~ github\.com[:/]+([^/]+)/([^/.]+)(\.git)?$ ]]; then
    printf '%s/%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  printf '%s/%s\n' "local" "$(basename "${HOSTED_REPO_ROOT}")"
}

hosted::image_namespace() {
  if [[ -n "${HOSTED_IMAGE_NAMESPACE:-}" ]]; then
    printf '%s\n' "$(hosted::trim "${HOSTED_IMAGE_NAMESPACE}")"
    return 0
  fi
  if [[ -n "${HOSTED_IMAGE_OWNER:-}" && -n "${HOSTED_IMAGE_REPOSITORY:-}" ]]; then
    printf 'ghcr.io/%s/%s\n' "$(hosted::trim "${HOSTED_IMAGE_OWNER}")" "$(hosted::trim "${HOSTED_IMAGE_REPOSITORY}")"
    return 0
  fi
  if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
    printf 'ghcr.io/%s\n' "$(hosted::trim "${GITHUB_REPOSITORY}")"
    return 0
  fi
  local slug
  slug="$(hosted::repo_slug_from_remote)"
  printf 'ghcr.io/%s\n' "$slug"
}

hosted::image_name() {
  local service="$1"
  printf '%s/%s\n' "$(hosted::image_namespace)" "$service"
}

hosted::image_ref() {
  local service="$1"
  local tag="${2:-$(hosted::release_tag)}"
  printf '%s:%s\n' "$(hosted::image_name "$service")" "$tag"
}

hosted::npm_release_version() {
  local package_name="$1"
  local requested="$2"
  if [[ -n "${requested}" && "${requested}" != "latest" ]]; then
    printf '%s\n' "$(hosted::trim "${requested}")"
    return 0
  fi
  hosted::require npm
  npm view "${package_name}" version
}

hosted::tandem_engine_release_version() {
  local requested="${HOSTED_TANDEM_ENGINE_RELEASE_VERSION:-${HOSTED_TANDEM_RELEASE_VERSION:-${TANDEM_ENGINE_RELEASE_VERSION:-${TANDEM_RELEASE_VERSION:-}}}}"
  if [[ -z "${requested}" || "${requested}" == "latest" ]]; then
    if [[ "${HOSTED_STRICT_RELEASE:-false}" == "true" ]]; then
      hosted::die "HOSTED_TANDEM_ENGINE_RELEASE_VERSION must be pinned when HOSTED_STRICT_RELEASE=true"
    fi
  fi
  hosted::npm_release_version "@frumu/tandem" "${requested}"
}

hosted::tandem_control_panel_release_version() {
  local requested="${HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION:-${HOSTED_TANDEM_RELEASE_VERSION:-${TANDEM_CONTROL_PANEL_RELEASE_VERSION:-${TANDEM_RELEASE_VERSION:-}}}}"
  if [[ -z "${requested}" || "${requested}" == "latest" ]]; then
    if [[ "${HOSTED_STRICT_RELEASE:-false}" == "true" ]]; then
      hosted::die "HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION must be pinned when HOSTED_STRICT_RELEASE=true"
    fi
  fi
  hosted::npm_release_version "@frumu/tandem-panel" "${requested}"
}

hosted::tandem_release_version() {
  hosted::tandem_engine_release_version
}

hosted::compose_project_name() {
  local raw="${HOSTED_COMPOSE_PROJECT_NAME:-${HOSTED_DEPLOYMENT_SLUG:-tandem-hosted}}"
  raw="$(hosted::trim "$raw")"
  raw="${raw,,}"
  raw="${raw//[^a-z0-9_.-]/-}"
  printf '%s\n' "$raw"
}

hosted::deployment_slug() {
  if [[ -n "${HOSTED_DEPLOYMENT_SLUG:-}" ]]; then
    printf '%s\n' "$(hosted::trim "${HOSTED_DEPLOYMENT_SLUG}")"
    return 0
  fi
  hosted::die "HOSTED_DEPLOYMENT_SLUG is required"
}

hosted::install_root() {
  if [[ -n "${HOSTED_INSTALL_ROOT:-}" ]]; then
    printf '%s\n' "$(hosted::trim "${HOSTED_INSTALL_ROOT}")"
    return 0
  fi
  printf '/srv/tandem/%s\n' "$(hosted::deployment_slug)"
}

hosted::bundle_dir() {
  if [[ -n "${HOSTED_BUNDLE_DIR:-}" ]]; then
    printf '%s\n' "$(hosted::trim "${HOSTED_BUNDLE_DIR}")"
    return 0
  fi
  local release
  release="$(hosted::release_tag)"
  if [[ -n "${HOSTED_DEPLOYMENT_SLUG:-}" ]]; then
    printf '%s/out/hosted/%s/%s\n' "${HOSTED_REPO_ROOT}" "$release" "$(hosted::deployment_slug)"
    return 0
  fi
  printf '%s/out/hosted/%s\n' "${HOSTED_REPO_ROOT}" "$release"
}

hosted::compose_file() {
  printf '%s/docker-compose.hosted.yml\n' "$(hosted::bundle_dir)"
}

hosted::runtime_env_file() {
  printf '%s/hosted.env\n' "$(hosted::bundle_dir)"
}

hosted::control_panel_config_file() {
  printf '%s/control-panel-config.json\n' "$(hosted::bundle_dir)"
}

hosted::cloud_init_file() {
  printf '%s/cloud-init.user-data.sh\n' "$(hosted::bundle_dir)"
}

hosted::release_manifest_file() {
  printf '%s/release-manifest.env\n' "$(hosted::bundle_dir)"
}

hosted::release_json_file() {
  printf '%s/release-manifest.json\n' "$(hosted::bundle_dir)"
}

hosted::bundle_archive_file() {
  printf '%s.tar.gz\n' "$(hosted::bundle_dir)"
}

hosted::deployment_data_root() {
  printf '%s/tandem-data\n' "$(hosted::install_root)"
}

hosted::deployment_engine_state_root() {
  printf '%s/tandem-engine-state\n' "$(hosted::install_root)"
}

hosted::deployment_panel_state_root() {
  printf '%s/tandem-panel-state\n' "$(hosted::install_root)"
}

hosted::deployment_runs_root() {
  printf '%s/runs\n' "$(hosted::install_root)"
}

hosted::deployment_repos_root() {
  printf '%s/repos\n' "$(hosted::install_root)"
}

hosted::deployment_secrets_root() {
  printf '%s/secrets\n' "$(hosted::install_root)"
}

hosted::deployment_proxy_root() {
  printf '%s/proxy\n' "$(hosted::install_root)"
}

hosted::deployment_proxy_caddyfile() {
  printf '%s/Caddyfile\n' "$(hosted::deployment_proxy_root)"
}

hosted::deployment_proxy_data_root() {
  printf '%s/proxy/data\n' "$(hosted::install_root)"
}

hosted::deployment_proxy_config_root() {
  printf '%s/proxy/config\n' "$(hosted::install_root)"
}
