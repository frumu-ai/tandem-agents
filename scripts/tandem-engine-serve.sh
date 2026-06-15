#!/usr/bin/env bash
set -euo pipefail

HOST="${TANDEM_ENGINE_HOST:-0.0.0.0}"
PORT="${TANDEM_ENGINE_PORT:-${TANDEM_PORT:-39733}}"
TOKEN_FILE="${TANDEM_API_TOKEN_FILE:-/run/secrets/tandem_api_token}"
CONTROL_PANEL_CONFIG_FILE="${TANDEM_CONTROL_PANEL_CONFIG_FILE:-/workspace/tandem-data/control-panel-config.json}"
MCP_REGISTRY_FILE="${TANDEM_STATE_DIR:-/home/node/.local/share/tandem/data}/mcp/mcp_servers.json"
MCP_BOOTSTRAP_SCRIPT="${TANDEM_MCP_BOOTSTRAP_SCRIPT:-/usr/local/lib/tandem-mcp-bootstrap.js}"

read_token_file() {
  local file="$1"
  tr -d '\r\n' < "$file"
}

write_token_file() {
  local file="$1"
  local token="$2"
  local dir
  dir="$(dirname "$file")"
  mkdir -p "$dir"
  umask 077
  printf '%s\n' "$token" > "$file"
  chmod 600 "$file" 2>/dev/null || true
}

generate_token() {
  tandem-engine token generate | tr -d '\r\n'
}

apply_mcp_bootstrap() {
  local file="${MCP_REGISTRY_FILE%/}"
  if [[ -z "$file" ]]; then
    return 0
  fi

  mkdir -p "$(dirname "$file")"

  if [[ ! -f "$MCP_BOOTSTRAP_SCRIPT" ]]; then
    echo "ACA MCP bootstrap: missing bootstrap script at $MCP_BOOTSTRAP_SCRIPT; skipping" >&2
    return 0
  fi

  if ! CONTROL_PANEL_CONFIG_FILE="$CONTROL_PANEL_CONFIG_FILE" \
    MCP_SOURCE_FILE="$CONTROL_PANEL_CONFIG_FILE" \
    MCP_REGISTRY_FILE="$file" \
    GITHUB_PERSONAL_ACCESS_TOKEN_FILE="${GITHUB_PERSONAL_ACCESS_TOKEN_FILE:-/run/secrets/github_token}" \
    GITHUB_TOKEN_FILE="${GITHUB_TOKEN_FILE:-/run/secrets/github_token}" \
    node "$MCP_BOOTSTRAP_SCRIPT" >/dev/null
  then
    echo "ACA MCP bootstrap: failed to seed registry; continuing without MCP bootstrap" >&2
  fi
}

disable_builtin_github_bootstrap() {
  if [[ "${ACA_GITHUB_MCP_ENABLED:-false}" != "true" ]]; then
    return 0
  fi

  unset GITHUB_PERSONAL_ACCESS_TOKEN
  unset GITHUB_TOKEN
  echo "ACA GitHub MCP bootstrap: disabled Tandem built-in GitHub token bootstrap for this engine start" >&2
}

apply_browser_config() {
  if [[ "${ACA_BROWSER_ENABLED:-true}" != "true" ]]; then
    echo "ACA browser config: disabled by ACA_BROWSER_ENABLED=${ACA_BROWSER_ENABLED}" >&2
    return 0
  fi

  local state_dir cfg_file
  state_dir="${TANDEM_STATE_DIR:-/home/node/.local/share/tandem/data}"
  cfg_file="${state_dir%/}/config.json"

  echo "ACA browser config: state_dir=$state_dir cfg_file=$cfg_file TANDEM_BROWSER_EXECUTABLE=${TANDEM_BROWSER_EXECUTABLE:-UNSET}" >&2

  if [[ ! -f "$cfg_file" ]]; then
    echo "ACA browser config: no engine config at $cfg_file; skipping browser patch" >&2
    return 0
  fi

  local browser_executable="${TANDEM_BROWSER_EXECUTABLE:-/usr/bin/chromium}"
  local headless="${ACA_BROWSER_HEADLESS:-true}"

  echo "ACA browser config: patching with executable=$browser_executable headless=$headless" >&2

  local patch
  patch=$(\
    CFG_FILE="$cfg_file" \
    BROWSER_EXECUTABLE="$browser_executable" \
    BROWSER_HEADLESS="$headless" \
    node <<'NODE'
const fs = require("fs");
const file = process.env.CFG_FILE;
const executable = process.env.BROWSER_EXECUTABLE || "/usr/bin/chromium";
const headless = process.env.BROWSER_HEADLESS !== "false";
let obj;
try {
  obj = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (error) {
  console.error("ACA browser config: could not read " + file + ": " + error.message);
  process.exit(0);
}
obj.browser = { enabled: true, headless: headless, executable: executable, allow_no_sandbox: true };
fs.writeFileSync(file, JSON.stringify(obj, null, 2));
console.error("ACA browser config: patched browser={enabled:true,headless:" + headless + ",executable:" + executable + "}");
NODE
)

  echo "$patch" >&2
}

token=""
token_source=""

if [[ -n "${TANDEM_API_TOKEN_FILE:-}" && -r "${TANDEM_API_TOKEN_FILE}" ]]; then
  token="$(read_token_file "${TANDEM_API_TOKEN_FILE}")"
  token_source="file"
elif [[ -r "$TOKEN_FILE" ]]; then
  token="$(read_token_file "$TOKEN_FILE")"
  token_source="file"
elif [[ -n "${TANDEM_API_TOKEN:-}" ]]; then
  token="${TANDEM_API_TOKEN}"
  token_source="env"
elif [[ -n "${TANDEM_TOKEN:-}" ]]; then
  token="${TANDEM_TOKEN}"
  token_source="legacy_env"
fi

if [[ -z "$token" ]]; then
  if ! command -v tandem-engine >/dev/null 2>&1; then
    echo "ACA Tandem engine requires tandem-engine to bootstrap a token. Set TANDEM_API_TOKEN_FILE or TANDEM_API_TOKEN before starting the engine." >&2
    exit 1
  fi
  token="$(generate_token)"
  if [[ -z "$token" ]]; then
    echo "ACA Tandem engine token generation returned an empty token." >&2
    exit 1
  fi
  if write_token_file "$TOKEN_FILE" "$token"; then
    token="$(read_token_file "$TOKEN_FILE")"
    token_source="generated"
  else
    echo "ACA Tandem engine could not write a token file at $TOKEN_FILE. Set TANDEM_API_TOKEN_FILE or TANDEM_API_TOKEN before starting the engine." >&2
    exit 1
  fi
fi

TANDEM_STATE_DIR="${TANDEM_STATE_DIR:-/home/node/.local/share/tandem/data}"
export TANDEM_STATE_DIR

export TANDEM_API_TOKEN="$token"

echo "ACA Tandem engine starting with token source=${token_source}" >&2
apply_mcp_bootstrap
disable_builtin_github_bootstrap
apply_browser_config

exec /home/node/npm/bin/tandem-engine serve --hostname "${HOST}" --port "${PORT}" 2>&1
