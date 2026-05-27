#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

output=""
bundle_url="${HOSTED_BUNDLE_URL:-}"
bundle_sha256="${HOSTED_BUNDLE_SHA256:-}"
deployment_slug="${HOSTED_DEPLOYMENT_SLUG:-}"
public_url="${HOSTED_CONTROL_PANEL_PUBLIC_URL:-${HOSTED_PUBLIC_URL:-}}"

usage() {
  cat <<'EOF'
Usage:
  render-cloud-init.sh --bundle-url URL [--bundle-sha256 SHA256] [--output FILE]

Render a shell-script cloud-init payload that downloads a hosted bundle and boots it.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle-url)
      bundle_url="$2"
      shift 2
      ;;
    --bundle-sha256)
      bundle_sha256="$2"
      shift 2
      ;;
    --deployment-slug)
      deployment_slug="$2"
      shift 2
      ;;
    --public-url)
      public_url="$2"
      shift 2
      ;;
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

if [[ -z "$bundle_url" ]]; then
  hosted::die "--bundle-url is required"
fi

bootstrap_bundle_name="$(basename "${bundle_url%%\?*}")"
deployment_slug="${deployment_slug:-hosted}"

payload="$(
cat <<EOF
#!/usr/bin/env bash
set -euo pipefail

BUNDLE_URL=$(printf '%q' "$bundle_url")
BUNDLE_SHA256=$(printf '%q' "$bundle_sha256")
DEPLOYMENT_SLUG=$(printf '%q' "$deployment_slug")
PUBLIC_URL=$(printf '%q' "$public_url")
WORK_ROOT=/var/lib/tandem-hosted/bootstrap

mkdir -p "\$WORK_ROOT"
cd "\$WORK_ROOT"

if ! command -v curl >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends curl ca-certificates
fi

archive="\$WORK_ROOT/${bootstrap_bundle_name}"
curl -fsSL "\$BUNDLE_URL" -o "\$archive"

if [[ -n "\$BUNDLE_SHA256" ]]; then
  echo "\$BUNDLE_SHA256  \$archive" | sha256sum -c -
fi

rm -rf "\$WORK_ROOT/extracted"
mkdir -p "\$WORK_ROOT/extracted"
tar -xzf "\$archive" -C "\$WORK_ROOT/extracted"

exec "\$WORK_ROOT/extracted/bootstrap-server.sh"
EOF
)"

if [[ -n "$output" ]]; then
  mkdir -p "$(dirname "$output")"
  printf '%s\n' "$payload" > "$output"
else
  printf '%s\n' "$payload"
fi
