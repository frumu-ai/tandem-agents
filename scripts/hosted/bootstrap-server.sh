#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

bundle_dir="${HOSTED_BUNDLE_DIR:-$SCRIPT_DIR}"
install_root="${HOSTED_INSTALL_ROOT:-}"
skip_prereqs=false
skip_healthcheck=false
wait_seconds="${HOSTED_WAIT_SECONDS:-300}"

usage() {
  cat <<'EOF'
Usage:
  bootstrap-server.sh [--bundle-dir PATH] [--skip-prereqs] [--skip-healthcheck] [--wait-seconds N]

Bootstrap a fresh customer VM from a packaged hosted bundle.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle-dir)
      bundle_dir="$2"
      shift 2
      ;;
    --skip-prereqs)
      skip_prereqs=true
      shift
      ;;
    --skip-healthcheck)
      skip_healthcheck=true
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

bundle_dir="$(cd "$bundle_dir" && pwd)"
hosted_env_file="${bundle_dir}/hosted.env"
compose_src="${bundle_dir}/docker-compose.hosted.yml"
control_panel_src="${bundle_dir}/control-panel-config.json"
release_env_src="${bundle_dir}/release-manifest.env"
release_json_src="${bundle_dir}/release-manifest.json"
bootstrap_copy="${bundle_dir}/bootstrap-server.sh"
prereqs_copy="${bundle_dir}/install-prereqs.sh"
secrets_copy="${bundle_dir}/generate-secrets.sh"
runtime_env_copy="${bundle_dir}/render-runtime-env.sh"
control_panel_config_copy="${bundle_dir}/render-control-panel-config.sh"
compose_copy="${bundle_dir}/render-compose.sh"
caddyfile_copy="${bundle_dir}/render-caddyfile.sh"
hardening_copy="${bundle_dir}/host-hardening.sh"
manifest_copy="${bundle_dir}/release-manifest.sh"
lib_copy="${bundle_dir}/lib.sh"

[[ -f "$hosted_env_file" ]] || hosted::die "missing bundle env file: ${hosted_env_file}"
[[ -f "$compose_src" ]] || hosted::die "missing bundle compose file: ${compose_src}"
[[ -f "$control_panel_src" ]] || hosted::die "missing control-panel config: ${control_panel_src}"
[[ -f "$bootstrap_copy" ]] || hosted::die "missing bootstrap script: ${bootstrap_copy}"
[[ -f "$prereqs_copy" ]] || hosted::die "missing prereqs script: ${prereqs_copy}"
[[ -f "$secrets_copy" ]] || hosted::die "missing secrets generator: ${secrets_copy}"
[[ -f "${bundle_dir}/Caddyfile" ]] || hosted::die "missing bundle caddyfile: ${bundle_dir}/Caddyfile"
[[ -f "$runtime_env_copy" ]] || hosted::die "missing runtime env renderer: ${runtime_env_copy}"
[[ -f "$control_panel_config_copy" ]] || hosted::die "missing control panel config renderer: ${control_panel_config_copy}"
[[ -f "$compose_copy" ]] || hosted::die "missing compose renderer: ${compose_copy}"
[[ -f "$caddyfile_copy" ]] || hosted::die "missing caddyfile renderer: ${caddyfile_copy}"
[[ -f "$hardening_copy" ]] || hosted::die "missing host hardening script: ${hardening_copy}"
[[ -f "$manifest_copy" ]] || hosted::die "missing release manifest helper: ${manifest_copy}"
[[ -f "$lib_copy" ]] || hosted::die "missing helper library: ${lib_copy}"

set -a
# shellcheck disable=SC1090
source "$hosted_env_file"
set +a

if [[ -z "${HOSTED_INSTALL_ROOT:-}" ]]; then
  hosted::die "HOSTED_INSTALL_ROOT is missing from the bundle environment"
fi

install_root="${HOSTED_INSTALL_ROOT}"
data_root="${HOSTED_DATA_ROOT:-${install_root}/tandem-data}"
engine_state_root="${HOSTED_ENGINE_STATE_ROOT:-${install_root}/tandem-engine-state}"
panel_state_root="${HOSTED_PANEL_STATE_ROOT:-${install_root}/tandem-panel-state}"
runs_root="${HOSTED_RUNS_ROOT:-${install_root}/runs}"
repos_root="${HOSTED_REPOS_ROOT:-${install_root}/repos}"
secrets_root="${HOSTED_SECRETS_ROOT:-${install_root}/secrets}"
proxy_root="${HOSTED_PROXY_ROOT:-${install_root}/proxy}"
proxy_data_root="${HOSTED_PROXY_DATA_ROOT:-${install_root}/proxy/data}"
proxy_config_root="${HOSTED_PROXY_CONFIG_ROOT:-${install_root}/proxy/config}"
kb_docs_root="${HOSTED_KB_DOCS_ROOT:-${install_root}/kb-docs}"
kb_index_root="${HOSTED_KB_INDEX_ROOT:-${install_root}/kb-index}"
compose_file="${HOSTED_COMPOSE_FILE:-${install_root}/docker-compose.hosted.yml}"
control_panel_file="${HOSTED_CONTROL_PANEL_CONFIG_FILE:-${data_root}/control-panel-config.json}"
proxy_caddyfile="${HOSTED_CADDYFILE:-${proxy_root}/Caddyfile}"
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"
host_uid="${HOSTED_HOST_UID:-1000}"
host_gid="${HOSTED_HOST_GID:-1000}"

hosted::as_root install -d -m 0755 "${install_root}" "${data_root}" "${engine_state_root}" "${panel_state_root}" "${runs_root}" "${repos_root}" "${secrets_root}" "${proxy_root}" "${proxy_data_root}" "${proxy_config_root}" "${kb_docs_root}" "${kb_index_root}"
hosted::as_root chown -R "${host_uid}:${host_gid}" "${data_root}" "${engine_state_root}" "${panel_state_root}" "${runs_root}" "${repos_root}" "${kb_docs_root}" "${kb_index_root}"

stage_file() {
  local source_path="$1"
  local target_path="$2"
  if [[ -f "$source_path" ]]; then
    hosted::as_root install -m 0644 "$source_path" "$target_path"
  fi
}

stage_executable() {
  local source_path="$1"
  local target_path="$2"
  if [[ -f "$source_path" ]]; then
    hosted::as_root install -m 0755 "$source_path" "$target_path"
  fi
}

stage_file "$hosted_env_file" "${install_root}/hosted.env"
stage_file "$compose_src" "${compose_file}"
stage_file "$control_panel_src" "$control_panel_file"
stage_file "$release_env_src" "${install_root}/release-manifest.env"
stage_file "$release_json_src" "${install_root}/release-manifest.json"
stage_file "${bundle_dir}/Caddyfile" "$proxy_caddyfile"
stage_file "${bundle_dir}/README.md" "${install_root}/README.md"
stage_executable "$runtime_env_copy" "${install_root}/render-runtime-env.sh"
stage_executable "$control_panel_config_copy" "${install_root}/render-control-panel-config.sh"
stage_executable "$compose_copy" "${install_root}/render-compose.sh"
stage_executable "$caddyfile_copy" "${install_root}/render-caddyfile.sh"
stage_executable "$hardening_copy" "${install_root}/host-hardening.sh"
stage_executable "$manifest_copy" "${install_root}/release-manifest.sh"
stage_executable "$bootstrap_copy" "${install_root}/bootstrap-server.sh"
stage_executable "$prereqs_copy" "${install_root}/install-prereqs.sh"
stage_executable "$secrets_copy" "${install_root}/generate-secrets.sh"
stage_file "$lib_copy" "${install_root}/lib.sh"

if [[ "$skip_prereqs" == false ]]; then
  "${install_root}/install-prereqs.sh"
fi

"${install_root}/generate-secrets.sh" --secrets-root "$secrets_root"

if [[ -n "${HOSTED_REGISTRY_USERNAME:-}" && -n "${HOSTED_REGISTRY_TOKEN:-}" ]]; then
  hosted::log "logging in to ghcr.io as ${HOSTED_REGISTRY_USERNAME}"
  printf '%s\n' "${HOSTED_REGISTRY_TOKEN}" | hosted::as_root docker login ghcr.io -u "${HOSTED_REGISTRY_USERNAME}" --password-stdin >/dev/null
fi

hosted::as_root docker compose -f "$compose_file" config >/dev/null

skip_pull="${HOSTED_SKIP_PULL:-false}"
if [[ "$skip_pull" == true ]]; then
  hosted::log "skipping image pull because HOSTED_SKIP_PULL=true"
else
  hosted::as_root docker compose -f "$compose_file" pull
fi

up_args=(-d --remove-orphans)
if [[ "$skip_pull" == true ]]; then
  up_args+=(--pull never)
fi
hosted::as_root docker compose -f "$compose_file" up "${up_args[@]}"

if [[ "$skip_healthcheck" == true ]]; then
  hosted::log "bootstrap completed without health checks."
  exit 0
fi

control_panel_port="${HOSTED_CONTROL_PANEL_PORT:-39734}"
aca_port="${HOSTED_ACA_PORT:-39735}"
kb_port="${HOSTED_KB_PORT:-39736}"
deadline="$((SECONDS + wait_seconds))"

wait_for() {
  local label="$1"
  shift

  while (( SECONDS < deadline )); do
    if "$@" >/dev/null 2>&1; then
      hosted::log "${label} is healthy."
      return 0
    fi
    sleep 3
  done

  return 1
}

check_control_panel() {
  curl -fsS "http://127.0.0.1:${control_panel_port}/api/system/health" >/dev/null
}

check_aca_ready() {
  hosted::as_root docker compose -f "$compose_file" exec -T aca sh -lc '
TOKEN_FILE="${ACA_API_TOKEN_FILE:-/run/secrets/aca_api_token}"
TOKEN=""
if [ -r "$TOKEN_FILE" ]; then TOKEN="$(tr -d "\r\n" < "$TOKEN_FILE")"; fi
if [ -z "$TOKEN" ]; then TOKEN="${ACA_API_TOKEN:-}"; fi
curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:${ACA_API_PORT}/ready" >/dev/null
'
}

check_kb_ready() {
  curl -fsS "http://127.0.0.1:${kb_port}/health" >/dev/null
}

check_proxy_config() {
  hosted::as_root docker compose -f "$compose_file" exec -T tandem-proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
}

check_proxy_route() {
  curl -fsS "${public_url%/}/api/system/health" >/dev/null
}

if ! wait_for "control panel" check_control_panel; then
  hosted::die "control panel did not become healthy within ${wait_seconds}s"
fi

if ! wait_for "ACA runtime" check_aca_ready; then
  hosted::die "ACA runtime did not become healthy within ${wait_seconds}s"
fi

if ! wait_for "KB MCP" check_kb_ready; then
  hosted::die "KB MCP did not become healthy within ${wait_seconds}s"
fi

if ! wait_for "proxy config" check_proxy_config; then
  hosted::die "proxy did not validate within ${wait_seconds}s"
fi

case "${public_url}" in
  http://localhost*|http://127.0.0.1*)
    if ! wait_for "proxy route" check_proxy_route; then
      hosted::die "proxy route did not become reachable within ${wait_seconds}s"
    fi
    ;;
esac

hosted::log "hosted deployment bootstrapped successfully."
hosted::log "  install root: ${install_root}"
hosted::log "  compose file: ${compose_file}"
hosted::log "  proxy caddyfile: ${proxy_caddyfile}"
hosted::log "  public url: ${public_url}"
hosted::log "  control panel: http://127.0.0.1:${control_panel_port}"
hosted::log "  ACA API: internal http://aca:${aca_port}"
hosted::log "  KB MCP: http://127.0.0.1:${kb_port}"
hosted::log "  tandem token file: ${secrets_root}/tandem_api_token"
hosted::log "  aca token file: ${secrets_root}/aca_api_token"
