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
python3 - <<'PY'
import json
import os
from urllib.parse import urlparse

def q(value: str) -> str:
    return json.dumps(str(value))

project_name = os.environ.get("HOSTED_COMPOSE_PROJECT_NAME", "tandem-hosted")
install_root = os.environ.get("HOSTED_INSTALL_ROOT", "/srv/tandem/hosted")
engine_image = os.environ["HOSTED_ENGINE_IMAGE"]
aca_image = os.environ["HOSTED_ACA_IMAGE"]
control_panel_image = os.environ["HOSTED_CONTROL_PANEL_IMAGE"]
proxy_image = os.environ["HOSTED_PROXY_IMAGE"]
kb_image = os.environ["HOSTED_KB_IMAGE"]
engine_port = os.environ.get("HOSTED_ENGINE_PORT", "39733")
aca_port = os.environ.get("HOSTED_ACA_PORT", "39735")
control_panel_port = os.environ.get("HOSTED_CONTROL_PANEL_PORT", "39734")
kb_port = os.environ.get("HOSTED_KB_PORT", "39736")
http_port = os.environ.get("HOSTED_HTTP_PORT", "80")
https_port = os.environ.get("HOSTED_HTTPS_PORT", "443")
engine_state_root = os.environ.get("HOSTED_ENGINE_STATE_ROOT", f"{install_root}/tandem-engine-state")
panel_state_root = os.environ.get("HOSTED_PANEL_STATE_ROOT", f"{install_root}/tandem-panel-state")
data_root = os.environ.get("HOSTED_DATA_ROOT", f"{install_root}/tandem-data")
repos_root = os.environ.get("HOSTED_REPOS_ROOT", f"{install_root}/repos")
runs_root = os.environ.get("HOSTED_RUNS_ROOT", f"{install_root}/runs")
secrets_root = os.environ.get("HOSTED_SECRETS_ROOT", f"{install_root}/secrets")
proxy_data_root = os.environ.get("HOSTED_PROXY_DATA_ROOT", f"{install_root}/proxy/data")
proxy_config_root = os.environ.get("HOSTED_PROXY_CONFIG_ROOT", f"{install_root}/proxy/config")
proxy_caddyfile = os.environ.get("HOSTED_CADDYFILE", f"{install_root}/proxy/Caddyfile")
kb_docs_root = os.environ.get("HOSTED_KB_DOCS_ROOT", f"{install_root}/kb-docs")
kb_index_root = os.environ.get("HOSTED_KB_INDEX_ROOT", f"{install_root}/kb-index")
public_url = os.environ.get("HOSTED_CONTROL_PANEL_PUBLIC_URL", os.environ.get("HOSTED_PUBLIC_URL", "")).strip()
parsed_public_url = urlparse(public_url) if public_url else None
scheme = (parsed_public_url.scheme or "").lower() if parsed_public_url else ""
host = (parsed_public_url.hostname or "").strip() if parsed_public_url else ""
local_hosts = {"localhost", "127.0.0.1", "::1"}
local_mode = scheme == "http" or host in local_hosts
listen_port = str(parsed_public_url.port or http_port) if parsed_public_url else http_port
enable_github_mcp = os.environ.get("HOSTED_ENABLE_GITHUB_MCP", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
github_mcp_url = os.environ.get("HOSTED_GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
github_mcp_toolsets = os.environ.get("HOSTED_GITHUB_MCP_TOOLSETS", "default,projects")
github_mcp_scope = os.environ.get("HOSTED_GITHUB_MCP_SCOPE", "intake_finalize")
github_remote_sync = os.environ.get("HOSTED_GITHUB_REMOTE_SYNC", "status_comment")
runtime_auth_mode = os.environ.get("HOSTED_RUNTIME_AUTH_MODE", os.environ.get("TANDEM_RUNTIME_AUTH_MODE", "")).strip()
context_public_keys = os.environ.get(
    "HOSTED_CONTEXT_ASSERTION_PUBLIC_KEYS",
    os.environ.get("TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS", ""),
).strip()
context_assertion_env = ""
if runtime_auth_mode:
    context_assertion_env += f"      TANDEM_RUNTIME_AUTH_MODE: {q(runtime_auth_mode)}\n"
if context_public_keys:
    context_assertion_env += f"      TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS: {q(context_public_keys)}\n"

control_panel_healthcheck = f"""\
      test:
        [
          "CMD-SHELL",
          "curl -fsS http://127.0.0.1:{control_panel_port}/api/system/health >/dev/null",
        ]
      interval: 10s
      timeout: 3s
      retries: 20
      start_period: 15s
"""

engine_healthcheck = f"""\
      test:
        - CMD-SHELL
        - >-
          TOKEN_FILE="$$TANDEM_API_TOKEN_FILE";
          if [ -z "$$TOKEN_FILE" ]; then TOKEN_FILE="/run/secrets/tandem_api_token"; fi;
          TOKEN="";
          if [ -r "$$TOKEN_FILE" ]; then TOKEN="$$(tr -d '\\r\\n' < "$$TOKEN_FILE")"; fi;
          if [ -z "$$TOKEN" ]; then TOKEN="$$TANDEM_API_TOKEN"; fi;
          if [ -z "$$TOKEN" ]; then TOKEN="$$TANDEM_TOKEN"; fi;
          URL="http://127.0.0.1:$$TANDEM_PORT";
          if [ -n "$$TOKEN" ]; then
            curl -fsS -H "Authorization: Bearer $$TOKEN" "$$URL/global/health" >/dev/null ||
            curl -fsS -H "Authorization: Bearer $$TOKEN" "$$URL/health" >/dev/null
          else
            curl -fsS "$$URL/global/health" >/dev/null ||
            curl -fsS "$$URL/health" >/dev/null
          fi
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s
"""

aca_healthcheck = f"""\
      test:
        - CMD-SHELL
        - >-
          TOKEN_FILE="$$ACA_API_TOKEN_FILE";
          if [ -z "$$TOKEN_FILE" ]; then TOKEN_FILE="/run/secrets/aca_api_token"; fi;
          TOKEN="";
          if [ -r "$$TOKEN_FILE" ]; then TOKEN="$$(tr -d '\\r\\n' < "$$TOKEN_FILE")"; fi;
          if [ -z "$$TOKEN" ]; then TOKEN="$$ACA_API_TOKEN"; fi;
          URL="http://127.0.0.1:$$ACA_API_PORT";
          if [ -n "$$TOKEN" ]; then
            curl -fsS -H "Authorization: Bearer $$TOKEN" "$$URL/ready" >/dev/null
          else
            curl -fsS "$$URL/health" >/dev/null
          fi
      interval: 10s
      timeout: 4s
      retries: 30
      start_period: 20s
"""

proxy_healthcheck = f"""\
      test:
        - CMD-SHELL
        - caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
      interval: 10s
      timeout: 5s
      retries: 20
      start_period: 15s
"""

kb_healthcheck = f"""\
      test:
        [
          "CMD-SHELL",
          "curl -fsS http://127.0.0.1:{kb_port}/health >/dev/null",
        ]
      interval: 10s
      timeout: 3s
      retries: 20
      start_period: 10s
"""

if local_mode:
    proxy_ports = [
        f"      - {q(f'127.0.0.1:{listen_port}:{listen_port}')}",
    ]
else:
    proxy_ports = [
        f"      - {q(f'{http_port}:80')}",
        f"      - {q(f'{https_port}:443')}",
    ]

engine_service = f"""\
  tandem-engine:
    image: {q(engine_image)}
    pull_policy: always
    environment:
      TANDEM_PORT: {q(engine_port)}
      TANDEM_ENGINE_PORT: {q(engine_port)}
      TANDEM_BASE_URL: ""
      TANDEM_STATE_DIR: /home/node/.local/share/tandem/data
      TANDEM_API_TOKEN_FILE: /run/secrets/tandem_api_token
      TANDEM_API_TOKEN: ""
      TANDEM_TOKEN: ""
      TANDEM_ENGINE_COMMAND: scripts/tandem-engine-serve.sh
      TANDEM_OUTPUT_ROOT: /workspace/tandem-data
      TANDEM_CONTROL_PANEL_CONFIG_FILE: /workspace/tandem-data/control-panel-config.json
      CODEX_HOME: /workspace/tandem-data/codex
      NPM_CONFIG_PREFIX: /home/node/npm
      GITHUB_PERSONAL_ACCESS_TOKEN: ""
      GITHUB_TOKEN: ""
      GITHUB_PERSONAL_ACCESS_TOKEN_FILE: /run/secrets/github_token
      GITHUB_TOKEN_FILE: /run/secrets/github_token
      ACA_GITHUB_MCP_ENABLED: {q("true" if enable_github_mcp else "false")}
      ACA_GITHUB_MCP_URL: {q(github_mcp_url)}
      ACA_GITHUB_MCP_TOOLSETS: {q(github_mcp_toolsets)}
      TANDEM_BROWSER_EXECUTABLE: /usr/bin/chromium
      TANDEM_BROWSER_NO_SANDBOX: "true"
{context_assertion_env.rstrip()}
      ACA_BROWSER_ENABLED: "true"
      ACA_BROWSER_HEADLESS: "true"
    volumes:
      - {engine_state_root}:/home/node/.local/share/tandem
      - {data_root}:/workspace/tandem-data
      - {repos_root}:/workspace/repos
      - {runs_root}:/workspace/tandem-agents/runs
      - {secrets_root}:/run/secrets
    depends_on:
      tandem-kb-mcp:
        condition: service_healthy
    healthcheck:
{engine_healthcheck.rstrip()}
    restart: unless-stopped
"""

control_panel_service = f"""\
  tandem-control-panel:
    image: {q(control_panel_image)}
    pull_policy: always
    environment:
      TANDEM_CONTROL_PANEL_HOST: 0.0.0.0
      TANDEM_CONTROL_PANEL_PORT: {q(control_panel_port)}
      TANDEM_ENGINE_URL: http://tandem-engine:{engine_port}
      TANDEM_CONTROL_PANEL_AUTO_START_ENGINE: "0"
      TANDEM_STATE_DIR: /var/lib/tandem/panel
      TANDEM_CONTROL_PANEL_STATE_DIR: /var/lib/tandem/panel/control-panel
      TANDEM_CONTROL_PANEL_CONFIG_FILE: /workspace/tandem-data/control-panel-config.json
      TANDEM_CONTROL_PANEL_MODE: auto
      TANDEM_API_TOKEN_FILE: /run/secrets/tandem_api_token
      ACA_BASE_URL: http://aca:{aca_port}
      ACA_API_TOKEN_FILE: /run/secrets/aca_api_token
      TANDEM_KB_ADMIN_URL: http://tandem-kb-mcp:{kb_port}
      TANDEM_KB_ADMIN_API_KEY_FILE: /run/secrets/kb_admin_api_key
      TANDEM_KB_DEFAULT_COLLECTION_ID: {q(os.environ.get("HOSTED_KB_DEFAULT_COLLECTION_ID", os.environ.get("HOSTED_DEPLOYMENT_SLUG", "")))}
      TANDEM_CONTROL_PANEL_WORKSPACE_ROOT: /workspace/repos
    volumes:
      - {panel_state_root}:/var/lib/tandem/panel
      - {data_root}:/workspace/tandem-data:ro
      - {repos_root}:/workspace/repos
      - {secrets_root}:/run/secrets:ro
    ports:
      - {q(f"127.0.0.1:{control_panel_port}:{control_panel_port}")}
    depends_on:
      tandem-engine:
        condition: service_healthy
      tandem-kb-mcp:
        condition: service_healthy
      aca:
        condition: service_healthy
    healthcheck:
{control_panel_healthcheck.rstrip()}
    restart: unless-stopped
"""

proxy_service = f"""\
  tandem-proxy:
    image: {q(proxy_image)}
    pull_policy: always
    volumes:
      - {proxy_caddyfile}:/etc/caddy/Caddyfile:ro
      - {proxy_data_root}:/data
      - {proxy_config_root}:/config
    ports:
{chr(10).join(proxy_ports)}
    depends_on:
      tandem-control-panel:
        condition: service_healthy
    healthcheck:
{proxy_healthcheck.rstrip()}
    restart: unless-stopped
"""

kb_service = f"""\
  tandem-kb-mcp:
    image: {q(kb_image)}
    pull_policy: always
    environment:
      KB_PORT: {q(kb_port)}
      KB_PUBLIC_BASE_URL: {q(f"http://127.0.0.1:{kb_port}/mcp")}
      KB_DOCS_ROOT: /workspace/kb-data/docs
      KB_INDEX_ROOT: /workspace/kb-data/index
      KB_ADMIN_API_KEY_FILE: /run/secrets/kb_admin_api_key
      KB_SERVER_NAME: ac.tandem/kb-mcp
      KB_SERVER_TITLE: Tandem Knowledgebase MCP
      KB_SERVER_DESCRIPTION: Local MCP server for private business knowledgebase retrieval and admin uploads.
      KB_SERVER_VERSION: 0.1.0
    volumes:
      - {kb_docs_root}:/workspace/kb-data/docs
      - {kb_index_root}:/workspace/kb-data/index
      - {secrets_root}:/run/secrets
    ports:
      - {q(f"127.0.0.1:{kb_port}:{kb_port}")}
    healthcheck:
{kb_healthcheck.rstrip()}
    restart: unless-stopped
"""

aca_service = f"""\
  aca:
    image: {q(aca_image)}
    pull_policy: always
    environment:
      ACA_ROOT: /workspace/tandem-agents
      TANDEM_BASE_URL: http://tandem-engine:{engine_port}
      TANDEM_ENGINE_PORT: {q(engine_port)}
      TANDEM_API_TOKEN_FILE: /run/secrets/tandem_api_token
      TANDEM_API_TOKEN: ""
      TANDEM_TOKEN: ""
      TANDEM_ENGINE_COMMAND: scripts/tandem-engine-serve.sh
      TANDEM_ENGINE_STARTUP_MODE: reuse_only
      TANDEM_OUTPUT_ROOT: /workspace/tandem-data
      AUTOCODER_WORKTREE_ROOT: /workspace/repos
      AUTOCODER_OUTPUT_ROOT: /workspace/tandem-agents/runs
      AUTOCODER_REPO_PATH: ""
      AUTOCODER_REPO_SLUG: ""
      AUTOCODER_REPO_URL: ""
      AUTOCODER_DEFAULT_BRANCH: main
      AUTOCODER_REMOTE_NAME: origin
      ACA_MODE: api
      ACA_API_PORT: {q(aca_port)}
      ACA_API_TOKEN_FILE: /run/secrets/aca_api_token
      TANDEM_CONTROL_PANEL_CONFIG_FILE: /workspace/tandem-data/control-panel-config.json
      TANDEM_CONTROL_PANEL_MODE: auto
      ACA_COORDINATION_POSTGRES_URL: ""
      ACA_STORAGE_PROFILE: local
      ACA_GITHUB_MCP_ENABLED: {q("true" if enable_github_mcp else "false")}
      ACA_GITHUB_MCP_URL: {q(github_mcp_url)}
      ACA_GITHUB_MCP_TOOLSETS: {q(github_mcp_toolsets)}
      ACA_GITHUB_MCP_SCOPE: {q(github_mcp_scope)}
      ACA_GITHUB_REMOTE_SYNC: {q(github_remote_sync)}
    volumes:
      - {repos_root}:/workspace/repos
      - {runs_root}:/workspace/tandem-agents/runs
      - {data_root}:/workspace/tandem-data
      - {secrets_root}:/run/secrets:ro
    expose:
      - {q(aca_port)}
    depends_on:
      tandem-engine:
        condition: service_healthy
    healthcheck:
{aca_healthcheck.rstrip()}
    restart: unless-stopped
"""

print(f"name: {project_name}")
print("services:")
print(engine_service.rstrip())
print()
print(control_panel_service.rstrip())
print()
print(proxy_service.rstrip())
print()
print(kb_service.rstrip())
print()
print(aca_service.rstrip())
PY
)"

if [[ -n "$output" ]]; then
  mkdir -p "$(dirname "$output")"
  printf '%s\n' "$compose" > "$output"
else
  printf '%s\n' "$compose"
fi
