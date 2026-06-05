#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

channel="stable"
published="true"
release_notes=""
web_root="${TANDEM_WEB_ROOT:-}"
publish_base_url="${HOSTED_RELEASE_PUBLISH_INTERNAL_BASE_URL:-}"
publish_token="${HOSTED_RELEASE_PUBLISH_TOKEN:-}"

usage() {
  cat <<'EOF'
Usage:
  publish-release.sh [--channel stable] [--published true|false] [--release-notes "notes"] [--web-root /path/to/tandem-web]

Build the Hosted release payload from the current manifest and publish it to the
tandem-web control plane using the internal Hosted release publish token.
EOF
}

load_env_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 1
  python3 - "$file" "$key" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() != key:
        continue
    print(value.strip().strip('"').strip("'"))
    raise SystemExit(0)
raise SystemExit(1)
PY
}

resolve_web_root() {
  if [[ -n "$web_root" && -d "$web_root" ]]; then
    printf '%s\n' "$(cd "$web_root" && pwd)"
    return 0
  fi

  local candidate
  candidate="${HOSTED_REPO_ROOT}/../tandem-web"
  if [[ -d "$candidate" ]]; then
    printf '%s\n' "$(cd "$candidate" && pwd)"
    return 0
  fi

  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)
      channel="${2:?--channel requires a value}"
      shift 2
      ;;
    --published)
      published="${2:?--published requires true or false}"
      shift 2
      ;;
    --release-notes)
      release_notes="${2:?--release-notes requires a value}"
      shift 2
      ;;
    --web-root)
      web_root="${2:?--web-root requires a path}"
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

payload_args=(--channel "${channel}" --published "${published}")
if [[ -n "${release_notes}" ]]; then
  payload_args+=(--release-notes "${release_notes}")
fi

payload="$("${SCRIPT_DIR}/release-payload.sh" "${payload_args[@]}")"
payload_file="$(mktemp)"
response_file="$(mktemp)"
trap 'rm -f "${payload_file}" "${response_file}"' EXIT
printf '%s\n' "${payload}" > "${payload_file}"

if [[ "${HOSTED_SKIP_IMAGE_VALIDATION:-false}" != "true" ]]; then
  mapfile -t image_refs < <(
    python3 - "${payload_file}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
keys = [
    "engine_image_ref",
    "aca_image_ref",
    "control_panel_image_ref",
    "proxy_image_ref",
    "kb_image_ref",
]
manifest = payload.get("manifest_json") or {}
keys_from_manifest = [
    ("public_engine_image_ref", manifest),
    ("public_aca_image_ref", manifest),
]
seen = set()
for key in keys:
    value = str(payload.get(key) or "").strip()
    if value and value not in seen:
        seen.add(value)
        print(value)
for key, source in keys_from_manifest:
    value = str(source.get(key) or "").strip()
    if value and value not in seen:
        seen.add(value)
        print(value)
PY
  )
  missing=()
  for image_ref in "${image_refs[@]}"; do
    hosted::log "checking image ${image_ref}"
    if ! docker buildx imagetools inspect "${image_ref}" >/dev/null 2>&1; then
      missing+=("${image_ref}")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    printf 'Missing hosted release image(s):\n' >&2
    printf '  - %s\n' "${missing[@]}" >&2
    hosted::die "refusing to publish hosted release with missing image refs"
  fi
fi

if [[ -z "${publish_base_url}" || -z "${publish_token}" ]]; then
  local_web_root="$(resolve_web_root 2>/dev/null || true)"
  if [[ -n "${local_web_root:-}" ]]; then
    local_env="${local_web_root}/.env"
    if [[ -z "${publish_base_url}" ]]; then
      publish_base_url="$(load_env_value "${local_env}" HOSTED_RELEASE_PUBLISH_INTERNAL_BASE_URL 2>/dev/null || true)"
    fi
    if [[ -z "${publish_token}" ]]; then
      publish_token="$(load_env_value "${local_env}" HOSTED_RELEASE_PUBLISH_TOKEN 2>/dev/null || true)"
    fi
  fi
fi

publish_base_url="${publish_base_url:-https://tandem.ac/api/v1}"
publish_url="${publish_base_url%/}/internal/hosted/releases"

if [[ -z "${publish_token}" ]]; then
  hosted::die "hosted release publish token not found. Set HOSTED_RELEASE_PUBLISH_TOKEN or add it to tandem-web/.env."
fi

hosted::log "publishing hosted release to tandem-web: ${publish_url}"
http_code="$(
  curl -sS \
    -X POST \
    -H "Authorization: Bearer ${publish_token}" \
    -H "Content-Type: application/json" \
    --data-binary @"${payload_file}" \
    -o "${response_file}" \
    -w '%{http_code}' \
    "${publish_url}"
)"

case "${http_code}" in
  200|201)
    cat "${response_file}"
    ;;
  409)
    hosted::log "hosted release already exists in tandem-web; nothing to publish."
    cat "${response_file}" >&2
    ;;
  *)
    cat "${response_file}" >&2 || true
    hosted::die "failed to publish hosted release (HTTP ${http_code})"
    ;;
esac
