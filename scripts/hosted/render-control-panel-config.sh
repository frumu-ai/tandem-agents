#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

output=""
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"
deployment_name="${HOSTED_DEPLOYMENT_NAME:-${HOSTED_DEPLOYMENT_SLUG:-hosted}}"

usage() {
  cat <<'EOF'
Usage:
  render-control-panel-config.sh [--output FILE] [--public-url URL]

Render the hosted control-panel JSON config.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    --public-url)
      public_url="$2"
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

source <("${SCRIPT_DIR}/release-manifest.sh")

content="$(python3 - "$deployment_name" "$public_url" <<'PY'
import json
import os
import sys

deployment_name = sys.argv[1]
public_url = sys.argv[2]

def join_mcp_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/mcp"

github_mcp_url = os.environ.get("HOSTED_GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
github_mcp_toolsets = os.environ.get("HOSTED_GITHUB_MCP_TOOLSETS", "default,projects")
github_mcp_scope = os.environ.get("HOSTED_GITHUB_MCP_SCOPE", "intake_finalize")
github_remote_sync = os.environ.get("HOSTED_GITHUB_REMOTE_SYNC", "status_comment")
github_enabled = os.environ.get("HOSTED_ENABLE_GITHUB_MCP", "").strip().lower()
kb_admin_url = os.environ.get("HOSTED_KB_ADMIN_URL", "http://tandem-kb-mcp:39736")
kb_mcp_url = os.environ.get("HOSTED_KB_MCP_URL", join_mcp_url(kb_admin_url))
hosted_managed = os.environ.get("HOSTED_MANAGED", "true").strip().lower() in {"1", "true", "yes", "y", "on"}

config = {
    "version": 1,
    "control_panel": {
        "mode": "auto",
        "aca_compact_nav": True,
    },
    "agent": {
        "name": "ACA",
        "dry_run": False,
    },
    "tandem": {
        "base_url": "http://tandem-engine:39733",
        "token_env": "TANDEM_API_TOKEN",
        "token_file": "/run/secrets/tandem_api_token",
        "required_version": "",
        "startup_mode": "reuse_only",
        "update_policy": "notify",
        "engine_command": "scripts/tandem-engine-serve.sh",
    },
    "task_source": {
        "type": "manual",
        "prompt": f"Hosted deployment bootstrap for {deployment_name}",
        "source_name": "hosted",
        "payload": {},
    },
    "repository": {
        "path": "",
        "slug": "",
        "clone_url": "",
        "default_branch": "main",
        "worktree_root": "/workspace/repos",
        "remote_name": "origin",
    },
    "provider": {
        "id": os.environ.get("HOSTED_DEFAULT_PROVIDER", "openai"),
        "model": os.environ.get("HOSTED_DEFAULT_MODEL", "gpt-4.1-mini"),
        "base_url": "",
        "fallback_provider": "",
        "fallback_model": "",
    },
    "execution": {
        "backend": "auto",
    },
    "swarm": {
        "enabled": False,
        "shared_model": False,
        "max_workers": 1,
        "max_retries": 1,
        "manager": {"provider": "", "model": ""},
        "worker": {"provider": "", "model": ""},
        "reviewer": {"provider": "", "model": ""},
        "tester": {"provider": "", "model": ""},
    },
    "storage": {
        "profile": os.environ.get("HOSTED_STORAGE_PROFILE", "local"),
    },
    "hosted": {
        "managed": hosted_managed,
        "provider": os.environ.get("HOSTED_PROVIDER", ""),
        "deployment_id": os.environ.get("HOSTED_DEPLOYMENT_ID", ""),
        "deployment_slug": os.environ.get("HOSTED_DEPLOYMENT_SLUG", ""),
        "hostname": os.environ.get("HOSTED_HOSTNAME", ""),
        "public_url": public_url,
        "control_plane_url": os.environ.get("HOSTED_CONTROL_PLANE_URL", public_url),
        "release_version": os.environ.get("HOSTED_RELEASE_VERSION", ""),
        "release_channel": os.environ.get("HOSTED_RELEASE_CHANNEL", ""),
        "engine_image": os.environ.get("HOSTED_ENGINE_IMAGE", ""),
        "aca_image": os.environ.get("HOSTED_ACA_IMAGE", ""),
        "control_panel_image": os.environ.get("HOSTED_CONTROL_PANEL_IMAGE", ""),
        "proxy_image": os.environ.get("HOSTED_PROXY_IMAGE", ""),
        "update_policy": os.environ.get("HOSTED_UPDATE_POLICY", "manual"),
    },
    "output": {
        "root": "runs",
    },
    "mcp_servers": {
        "github": {
            "transport": github_mcp_url,
            "headers": {"X-MCP-Toolsets": github_mcp_toolsets} if github_mcp_toolsets else {},
            "auth": {
                "token_envs": ["GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"],
                "token_file_envs": ["GITHUB_PERSONAL_ACCESS_TOKEN_FILE", "GITHUB_TOKEN_FILE"],
            },
            "auto_connect": True,
            "auto_enable_with_credentials": True,
            "scope": github_mcp_scope,
            "remote_sync": github_remote_sync,
        },
        "kb": {
            "enabled": True,
            "transport": kb_mcp_url,
            "auto_connect": True,
        },
    },
    "github_mcp": {
        "url": github_mcp_url,
        "toolsets": github_mcp_toolsets,
        "scope": github_mcp_scope,
        "remote_sync": github_remote_sync,
    },
}

if github_enabled in {"1", "true", "yes", "y", "on"}:
    config["mcp_servers"]["github"]["enabled"] = True
    config["github_mcp"]["enabled"] = True
elif github_enabled in {"0", "false", "no", "n", "off"}:
    config["mcp_servers"]["github"]["enabled"] = False
    config["github_mcp"]["enabled"] = False

if public_url:
    config["control_panel"]["public_url"] = public_url

json.dump(config, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
)"

if [[ -n "$output" ]]; then
  mkdir -p "$(dirname "$output")"
  printf '%s\n' "$content" > "$output"
else
  printf '%s\n' "$content"
fi
