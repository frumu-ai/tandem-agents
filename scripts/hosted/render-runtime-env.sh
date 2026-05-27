#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

output=""
deployment_slug="${HOSTED_DEPLOYMENT_SLUG:-}"
install_root="${HOSTED_INSTALL_ROOT:-}"
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"
runtime_auth_mode="${HOSTED_RUNTIME_AUTH_MODE:-${TANDEM_RUNTIME_AUTH_MODE:-}}"
context_assertion_public_keys="${HOSTED_CONTEXT_ASSERTION_PUBLIC_KEYS:-${TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS:-}}"

usage() {
  cat <<'EOF'
Usage:
  render-runtime-env.sh [--output FILE] [--deployment-slug SLUG] [--install-root PATH]

Render the hosted deployment env file that the bundle and bootstrap script use.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
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

if [[ -z "$deployment_slug" && -z "$install_root" ]]; then
  hosted::die "either HOSTED_DEPLOYMENT_SLUG or HOSTED_INSTALL_ROOT is required"
fi

source <("${SCRIPT_DIR}/release-manifest.sh")

if [[ -z "$install_root" ]]; then
  install_root="/srv/tandem/${deployment_slug}"
fi

bundle_dir="${HOSTED_BUNDLE_DIR:-$(hosted::bundle_dir)}"

data_root="${install_root}/tandem-data"
engine_state_root="${install_root}/tandem-engine-state"
panel_state_root="${install_root}/tandem-panel-state"
runs_root="${install_root}/runs"
repos_root="${install_root}/repos"
secrets_root="${install_root}/secrets"
proxy_root="${install_root}/proxy"
proxy_data_root="${install_root}/proxy/data"
proxy_config_root="${install_root}/proxy/config"
proxy_caddyfile="${proxy_root}/Caddyfile"
control_panel_upstream="${HOSTED_CONTROL_PANEL_UPSTREAM:-http://tandem-control-panel:${HOSTED_CONTROL_PANEL_PORT:-39734}}"
compose_file="${install_root}/docker-compose.hosted.yml"
control_panel_config_file="${data_root}/control-panel-config.json"
bootstrap_script="${install_root}/bootstrap-server.sh"
install_prereqs_script="${install_root}/install-prereqs.sh"
host_hardening_script="${install_root}/host-hardening.sh"
generate_secrets_script="${install_root}/generate-secrets.sh"
release_manifest_env="${install_root}/release-manifest.env"
release_manifest_json="${install_root}/release-manifest.json"
bundle_archive="${bundle_dir}.tar.gz"
bundle_readme="${install_root}/README.md"

emit() {
  printf '%s=%q\n' "$1" "$2"
}

content="$(
  {
    emit HOSTED_DEPLOYMENT_SLUG "${deployment_slug}"
    emit HOSTED_INSTALL_ROOT "${install_root}"
    emit HOSTED_BUNDLE_DIR "${bundle_dir}"
    emit HOSTED_BUNDLE_ARCHIVE "${bundle_archive}"
    emit HOSTED_DATA_ROOT "${data_root}"
    emit HOSTED_ENGINE_STATE_ROOT "${engine_state_root}"
    emit HOSTED_PANEL_STATE_ROOT "${panel_state_root}"
    emit HOSTED_RUNS_ROOT "${runs_root}"
    emit HOSTED_REPOS_ROOT "${repos_root}"
    emit HOSTED_SECRETS_ROOT "${secrets_root}"
    emit HOSTED_PROXY_ROOT "${proxy_root}"
    emit HOSTED_PROXY_DATA_ROOT "${proxy_data_root}"
    emit HOSTED_PROXY_CONFIG_ROOT "${proxy_config_root}"
    emit HOSTED_CADDYFILE "${proxy_caddyfile}"
    emit HOSTED_CONTROL_PANEL_UPSTREAM "${control_panel_upstream}"
    emit HOSTED_KB_ADMIN_URL "${HOSTED_KB_ADMIN_URL}"
    emit HOSTED_KB_MCP_URL "${HOSTED_KB_MCP_URL}"
    emit HOSTED_TANDEM_TOKEN_FILE "${secrets_root}/tandem_api_token"
    emit HOSTED_ACA_TOKEN_FILE "${secrets_root}/aca_api_token"
    emit HOSTED_PUBLIC_URL "${public_url}"
    emit HOSTED_CONTROL_PANEL_PUBLIC_URL "${public_url}"
    emit HOSTED_COMPOSE_PROJECT_NAME "$(hosted::compose_project_name)"
    emit HOSTED_COMPOSE_FILE "${compose_file}"
    emit HOSTED_CONTROL_PANEL_CONFIG_FILE "${control_panel_config_file}"
    emit HOSTED_BOOTSTRAP_SCRIPT "${bootstrap_script}"
    emit HOSTED_INSTALL_PREREQS_SCRIPT "${install_prereqs_script}"
    emit HOSTED_HOST_HARDENING_SCRIPT "${host_hardening_script}"
    emit HOSTED_GENERATE_SECRETS_SCRIPT "${generate_secrets_script}"
    emit HOSTED_RELEASE_MANIFEST_ENV "${release_manifest_env}"
    emit HOSTED_RELEASE_MANIFEST_JSON "${release_manifest_json}"
    emit HOSTED_BUNDLE_README "${bundle_readme}"
    emit HOSTED_RELEASE_TAG "${HOSTED_RELEASE_TAG}"
    emit HOSTED_GIT_SHA "${HOSTED_GIT_SHA}"
    emit HOSTED_GIT_SHORT_SHA "${HOSTED_GIT_SHORT_SHA}"
    emit HOSTED_GIT_REF "${HOSTED_GIT_REF}"
    emit HOSTED_BUILD_DATE "${HOSTED_BUILD_DATE}"
    emit HOSTED_TANDEM_RELEASE_VERSION "${HOSTED_TANDEM_RELEASE_VERSION}"
    emit HOSTED_TANDEM_ENGINE_RELEASE_VERSION "${HOSTED_TANDEM_ENGINE_RELEASE_VERSION}"
    emit HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION "${HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION}"
    emit HOSTED_IMAGE_NAMESPACE "${HOSTED_IMAGE_NAMESPACE}"
    emit HOSTED_ENGINE_IMAGE_REPOSITORY "${HOSTED_ENGINE_IMAGE_REPOSITORY}"
    emit HOSTED_ACA_IMAGE_REPOSITORY "${HOSTED_ACA_IMAGE_REPOSITORY}"
    emit HOSTED_CONTROL_PANEL_IMAGE_REPOSITORY "${HOSTED_CONTROL_PANEL_IMAGE_REPOSITORY}"
    emit HOSTED_PROXY_IMAGE_REPOSITORY "${HOSTED_PROXY_IMAGE_REPOSITORY}"
    emit HOSTED_KB_IMAGE_REPOSITORY "${HOSTED_KB_IMAGE_REPOSITORY}"
    emit HOSTED_ENGINE_IMAGE "${HOSTED_ENGINE_IMAGE}"
    emit HOSTED_ACA_IMAGE "${HOSTED_ACA_IMAGE}"
    emit HOSTED_CONTROL_PANEL_IMAGE "${HOSTED_CONTROL_PANEL_IMAGE}"
    emit HOSTED_PROXY_IMAGE "${HOSTED_PROXY_IMAGE}"
    emit HOSTED_KB_IMAGE "${HOSTED_KB_IMAGE}"
    emit HOSTED_DEFAULT_PROVIDER "${HOSTED_DEFAULT_PROVIDER}"
    emit HOSTED_DEFAULT_MODEL "${HOSTED_DEFAULT_MODEL}"
    emit HOSTED_HOST_UID "${HOSTED_HOST_UID}"
    emit HOSTED_HOST_GID "${HOSTED_HOST_GID}"
    emit HOSTED_ENGINE_PORT "${HOSTED_ENGINE_PORT}"
    emit HOSTED_ACA_PORT "${HOSTED_ACA_PORT}"
    emit HOSTED_CONTROL_PANEL_PORT "${HOSTED_CONTROL_PANEL_PORT}"
    emit HOSTED_HTTP_PORT "${HOSTED_HTTP_PORT}"
    emit HOSTED_HTTPS_PORT "${HOSTED_HTTPS_PORT}"
    emit HOSTED_STORAGE_PROFILE "${HOSTED_STORAGE_PROFILE}"
    emit HOSTED_KB_PORT "${HOSTED_KB_PORT}"
    emit HOSTED_KB_DOCS_ROOT "${HOSTED_KB_DOCS_ROOT}"
    emit HOSTED_KB_INDEX_ROOT "${HOSTED_KB_INDEX_ROOT}"
    emit HOSTED_KB_ADMIN_API_KEY_FILE "${HOSTED_KB_ADMIN_API_KEY_FILE}"
    emit HOSTED_KB_DEFAULT_COLLECTION_ID "${HOSTED_KB_DEFAULT_COLLECTION_ID}"
    emit HOSTED_GITHUB_PERSONAL_ACCESS_TOKEN_FILE "${HOSTED_GITHUB_PERSONAL_ACCESS_TOKEN_FILE}"
    emit HOSTED_GITHUB_TOKEN_FILE "${HOSTED_GITHUB_TOKEN_FILE}"
    emit HOSTED_ENABLE_GITHUB_MCP "${HOSTED_ENABLE_GITHUB_MCP}"
    emit HOSTED_GITHUB_MCP_URL "${HOSTED_GITHUB_MCP_URL}"
    emit HOSTED_GITHUB_MCP_TOOLSETS "${HOSTED_GITHUB_MCP_TOOLSETS}"
    emit HOSTED_GITHUB_MCP_SCOPE "${HOSTED_GITHUB_MCP_SCOPE}"
    emit HOSTED_GITHUB_REMOTE_SYNC "${HOSTED_GITHUB_REMOTE_SYNC}"
    if [[ -n "${runtime_auth_mode}" ]]; then
      emit HOSTED_RUNTIME_AUTH_MODE "${runtime_auth_mode}"
    fi
    if [[ -n "${context_assertion_public_keys}" ]]; then
      emit HOSTED_CONTEXT_ASSERTION_PUBLIC_KEYS "${context_assertion_public_keys}"
    fi
    emit HOSTED_ENABLE_OUTBOX "${HOSTED_ENABLE_OUTBOX}"
  } | sed '/^$/d'
)"

if [[ -n "$output" ]]; then
  mkdir -p "$(dirname "$output")"
  printf '%s\n' "$content" > "$output"
else
  printf '%s\n' "$content"
fi
