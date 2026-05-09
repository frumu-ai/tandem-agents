#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   chmod +x test_github_mcp.sh
#   GITHUB_PAT=ghp_xxx ./test_github_mcp.sh
#
# Optional:
#   GITHUB_MCP_URL="https://api.githubcopilot.com/mcp/" GITHUB_PAT=ghp_xxx ./test_github_mcp.sh
#   GITHUB_TOOLSETS="default,projects" GITHUB_PAT=ghp_xxx ./test_github_mcp.sh

MCP_URL="${GITHUB_MCP_URL:-https://api.githubcopilot.com/mcp/}"
TOOLSETS="${GITHUB_TOOLSETS:-default,projects}"

if [[ -z "${GITHUB_PAT:-}" ]]; then
  echo "Error: GITHUB_PAT is not set"
  echo "Example:"
  echo '  GITHUB_PAT=ghp_xxx ./test_github_mcp.sh'
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

INIT_HEADERS="$TMP_DIR/init_headers.txt"
INIT_BODY="$TMP_DIR/init_body.txt"
NOTIFY_HEADERS="$TMP_DIR/notify_headers.txt"
NOTIFY_BODY="$TMP_DIR/notify_body.txt"
TOOLS_HEADERS="$TMP_DIR/tools_headers.txt"
TOOLS_BODY="$TMP_DIR/tools_body.txt"

pretty_print_body() {
  local body_file="$1"
  if ! command -v jq >/dev/null 2>&1; then
    cat "$body_file"
    return 0
  fi

  if jq . "$body_file" >/dev/null 2>&1; then
    jq . "$body_file"
    return 0
  fi

  if grep -q '^data: ' "$body_file"; then
    awk '/^data: /{sub(/^data: /, ""); print}' "$body_file" \
      | jq -R 'fromjson? | select(.)' || cat "$body_file"
    return 0
  fi

  cat "$body_file"
}

extract_json_payload() {
  local body_file="$1"
  if jq . "$body_file" >/dev/null 2>&1; then
    cat "$body_file"
    return 0
  fi

  if grep -q '^data: ' "$body_file"; then
    awk '/^data: /{sub(/^data: /, ""); print}' "$body_file" \
      | jq -Rs '
          split("\n")
          | map(select(length > 0) | fromjson?)
          | map(select(. != null))
          | last // empty
        '
    return 0
  fi

  return 1
}

echo "==> MCP URL: $MCP_URL"
echo "==> Toolsets: $TOOLSETS"
echo

echo "==> Step 1: initialize"
curl -sS -D "$INIT_HEADERS" -o "$INIT_BODY" \
  -X POST "$MCP_URL" \
  -H "Authorization: Bearer $GITHUB_PAT" \
  -H "X-MCP-Toolsets: $TOOLSETS" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {
        "name": "manual-linux-test",
        "version": "1.0.0"
      }
    }
  }'

echo "-- Response headers --"
cat "$INIT_HEADERS"
echo
echo "-- Response body --"
pretty_print_body "$INIT_BODY"
echo
echo

SESSION_ID="$(grep -i '^Mcp-Session-Id:' "$INIT_HEADERS" | sed 's/\r$//' | awk -F': ' '{print $2}' || true)"

if [[ -n "$SESSION_ID" ]]; then
  echo "==> Found Mcp-Session-Id: $SESSION_ID"
else
  echo "==> No Mcp-Session-Id found in initialize response headers"
fi
echo

echo "==> Step 2: notifications/initialized"
NOTIFY_ARGS=(
  -sS -D "$NOTIFY_HEADERS" -o "$NOTIFY_BODY"
  -X POST "$MCP_URL"
  -H "Authorization: Bearer $GITHUB_PAT"
  -H "X-MCP-Toolsets: $TOOLSETS"
  -H "Content-Type: application/json"
  -H "Accept: application/json, text/event-stream"
)

if [[ -n "$SESSION_ID" ]]; then
  NOTIFY_ARGS+=(-H "Mcp-Session-Id: $SESSION_ID")
fi

curl "${NOTIFY_ARGS[@]}" \
  --data '{
    "jsonrpc": "2.0",
    "method": "notifications/initialized"
  }'

echo "-- Response headers --"
cat "$NOTIFY_HEADERS"
echo
echo "-- Response body --"
pretty_print_body "$NOTIFY_BODY"
echo
echo

echo "==> Step 3: tools/list"
TOOLS_ARGS=(
  -sS -D "$TOOLS_HEADERS" -o "$TOOLS_BODY"
  -X POST "$MCP_URL"
  -H "Authorization: Bearer $GITHUB_PAT"
  -H "X-MCP-Toolsets: $TOOLSETS"
  -H "Content-Type: application/json"
  -H "Accept: application/json, text/event-stream"
)

if [[ -n "$SESSION_ID" ]]; then
  TOOLS_ARGS+=(-H "Mcp-Session-Id: $SESSION_ID")
fi

curl "${TOOLS_ARGS[@]}" \
  --data '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'

echo "-- Response headers --"
cat "$TOOLS_HEADERS"
echo
echo "-- Response body --"
pretty_print_body "$TOOLS_BODY"
echo
echo

if command -v jq >/dev/null 2>&1; then
  TOOLS_JSON="$(extract_json_payload "$TOOLS_BODY" || true)"

  echo "==> Parsed tool names"
  if [[ -n "${TOOLS_JSON:-}" ]]; then
    printf '%s\n' "$TOOLS_JSON" | jq -r '
      if .result and .result.tools then
        .result.tools[]?.name
      else
        empty
      end
    ' || true
  else
    echo "No JSON tool payload detected"
  fi
  echo

  echo "==> Project-related tools only"
  if [[ -n "${TOOLS_JSON:-}" ]]; then
    printf '%s\n' "$TOOLS_JSON" | jq -r '
      if .result and .result.tools then
        .result.tools[]?.name
      else
        empty
      end
    ' | grep -i 'project' || true
  else
    echo "No JSON tool payload detected"
  fi
else
  echo "jq is not installed, skipping parsed output"
fi

echo
echo "==> Done"
echo "If the raw body is SSE formatted like:"
echo '  event: message'
echo '  data: {...json...}'
echo "then copy the data JSON part and inspect .result.tools manually."
