#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

secrets_root="${HOSTED_SECRETS_ROOT:-}"
install_root="${HOSTED_INSTALL_ROOT:-}"
tandem_token_override="${HOSTED_TANDEM_API_TOKEN:-}"
aca_token_override="${HOSTED_ACA_API_TOKEN:-}"
kb_token_override="${HOSTED_KB_ADMIN_API_KEY:-}"
github_pat_override="${HOSTED_GITHUB_PERSONAL_ACCESS_TOKEN:-${HOSTED_GITHUB_TOKEN:-}}"
host_uid="${HOSTED_HOST_UID:-1000}"
host_gid="${HOSTED_HOST_GID:-1000}"
force=false

usage() {
  cat <<'EOF'
Usage:
  generate-secrets.sh [--secrets-root PATH] [--install-root PATH] [--force]

Generate deployment-local Tandem, ACA, KB admin, and optional GitHub PAT secrets.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --secrets-root)
      secrets_root="$2"
      shift 2
      ;;
    --install-root)
      install_root="$2"
      shift 2
      ;;
    --force)
      force=true
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

if [[ -z "$secrets_root" ]]; then
  if [[ -n "$install_root" ]]; then
    secrets_root="${install_root%/}/secrets"
  else
    hosted::die "HOSTED_SECRETS_ROOT or HOSTED_INSTALL_ROOT is required"
  fi
fi

generate_token() {
  python3 - <<'PY'
from secrets import token_urlsafe
print(token_urlsafe(48))
PY
}

write_secret() {
  local path="$1"
  local value="$2"
  mkdir -p "$(dirname "$path")"
  umask 077
  printf '%s\n' "$value" > "$path"
  chmod 600 "$path" 2>/dev/null || true
}

ensure_secret() {
  local path="$1"
  local override="$2"
  local label="$3"
  local current=""

  if [[ -f "$path" && "$force" == false ]]; then
    current="$(tr -d '\r\n' < "$path")"
  fi

  local token
  token="${override:-$current}"
  if [[ -z "$token" ]]; then
    token="$(generate_token)"
  fi

  write_secret "$path" "$token"
  hosted::log "ensured ${label} secret at ${path}"
}

ensure_optional_secret() {
  local path="$1"
  local override="$2"
  local label="$3"

  if [[ -n "$override" ]]; then
    write_secret "$path" "$override"
    hosted::log "ensured ${label} secret at ${path}"
    return 0
  fi

  if [[ -f "$path" ]]; then
    hosted::log "kept existing ${label} secret at ${path}"
  else
    hosted::log "skipped ${label} secret; no override provided"
  fi
}

ensure_secret "${secrets_root%/}/tandem_api_token" "$tandem_token_override" "tandem"
ensure_secret "${secrets_root%/}/aca_api_token" "$aca_token_override" "aca"
ensure_secret "${secrets_root%/}/kb_admin_api_key" "$kb_token_override" "kb-admin"
chmod 600 "${secrets_root%/}/kb_admin_api_key" 2>/dev/null || true
chown "${host_uid}:${host_gid}" "${secrets_root%/}/kb_admin_api_key" 2>/dev/null || true
ensure_optional_secret "${secrets_root%/}/github_token" "$github_pat_override" "github-pat"
