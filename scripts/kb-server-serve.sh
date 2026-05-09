#!/usr/bin/env bash
set -euo pipefail

HOST="${KB_HOST:-0.0.0.0}"
PORT="${KB_PORT:-39736}"
ADMIN_KEY_FILE="${KB_ADMIN_API_KEY_FILE:-/run/secrets/kb_admin_api_key}"
KB_RUNTIME_USER="${KB_RUNTIME_USER:-tandem}"

write_key_file() {
  local file="$1"
  local token="$2"
  mkdir -p "$(dirname "$file")"
  umask 077
  printf '%s\n' "$token" > "$file"
  chmod 600 "$file" 2>/dev/null || true
}

generate_key() {
  python3 - <<'PY'
from secrets import token_urlsafe
print(token_urlsafe(48))
PY
}

if [[ ! -r "$ADMIN_KEY_FILE" ]]; then
  token="$(generate_key)"
  write_key_file "$ADMIN_KEY_FILE" "$token"
  echo "KB admin key generated at $ADMIN_KEY_FILE" >&2
fi

export KB_ADMIN_API_KEY_FILE="$ADMIN_KEY_FILE"
mkdir -p "${KB_DOCS_ROOT:-./kb-data/docs}" "${KB_INDEX_ROOT:-./kb-data/index}"

if [[ -r "$ADMIN_KEY_FILE" && -z "${KB_ADMIN_API_KEY:-}" ]]; then
  KB_ADMIN_API_KEY="$(tr -d '\r\n' < "$ADMIN_KEY_FILE")"
  export KB_ADMIN_API_KEY
fi

if [[ "$(id -u)" -eq 0 ]]; then
  if id "$KB_RUNTIME_USER" >/dev/null 2>&1; then
    chown -R "$KB_RUNTIME_USER:$KB_RUNTIME_USER" "${KB_DOCS_ROOT:-./kb-data/docs}" "${KB_INDEX_ROOT:-./kb-data/index}" 2>/dev/null || true
  fi
fi

exec_as_runtime_user() {
  if [[ "$(id -u)" -ne 0 ]]; then
    exec "$@"
  fi

  if ! id "$KB_RUNTIME_USER" >/dev/null 2>&1; then
    exec "$@"
  fi

  KB_RUNTIME_USER="$KB_RUNTIME_USER" python3 - "$@" <<'PY'
import os
import pwd
import sys

user = os.environ.get("KB_RUNTIME_USER", "tandem")
pw = pwd.getpwnam(user)
os.initgroups(pw.pw_name, pw.pw_gid)
os.setgid(pw.pw_gid)
os.setuid(pw.pw_uid)
os.execvp(sys.argv[1], sys.argv[1:])
PY
}

exec_as_runtime_user uvicorn src.aca.kb.main:app --host "$HOST" --port "$PORT" --proxy-headers --forwarded-allow-ips '*' 2>&1
