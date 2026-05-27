#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

output=""
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"
control_panel_upstream="${HOSTED_CONTROL_PANEL_UPSTREAM:-http://tandem-control-panel:${HOSTED_CONTROL_PANEL_PORT:-39734}}"
http_port="${HOSTED_HTTP_PORT:-80}"

usage() {
  cat <<'EOF'
Usage:
  render-caddyfile.sh [--output FILE] [--public-url URL] [--upstream URL]

Render the hosted Caddy front-door config for a single deployment.
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
    --upstream)
      control_panel_upstream="$2"
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

if [[ -z "${public_url:-}" ]]; then
  hosted::die "--public-url or HOSTED_CONTROL_PANEL_PUBLIC_URL is required"
fi

content="$(
python3 - "$public_url" "$control_panel_upstream" "$http_port" <<'PY'
import sys
from urllib.parse import urlparse

public_url = sys.argv[1].strip()
upstream = sys.argv[2].strip()
http_port = sys.argv[3].strip() or "80"

parsed = urlparse(public_url)
scheme = (parsed.scheme or "").lower()
host = (parsed.hostname or "").strip()
listen_port = str(parsed.port or http_port)

if scheme not in {"http", "https"}:
    raise SystemExit(f"Unsupported public URL scheme for Caddy: {public_url}")

local_hosts = {"localhost", "127.0.0.1", "::1"}
local_mode = scheme == "http" or host in local_hosts
site_address = f":{listen_port}" if local_mode else host
if not site_address:
    raise SystemExit(f"Could not determine Caddy site address from {public_url}")

forward_proto = "http" if local_mode else "https"
forward_port = listen_port if local_mode else "443"

print(f"{site_address} {{")
print("  encode zstd gzip")
print(f"  reverse_proxy {upstream} {{")
print(f"    header_up X-Forwarded-Host {host or 'localhost'}")
print(f"    header_up X-Forwarded-Proto {forward_proto}")
print(f"    header_up X-Forwarded-Port {forward_port}")
print("  }")
print("}")
PY
)"

if [[ -n "$output" ]]; then
  mkdir -p "$(dirname "$output")"
  printf '%s\n' "$content" > "$output"
else
  printf '%s\n' "$content"
fi
