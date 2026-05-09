#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ACA_ROOT="${ACA_ROOT:-${ROOT_DIR}}"

echo "ACA container entrypoint"
echo "  ROOT_DIR=${ROOT_DIR}"
echo "  ACA_ROOT=${ACA_ROOT}"
echo "  legacy ACA_ROOT=${ACA_ROOT}"
echo "  ACA_REPO_PATH=${ACA_REPO_PATH:-${AUTOCODER_REPO_PATH:-}}"
echo "  ACA_REPO_SLUG=${ACA_REPO_SLUG:-${AUTOCODER_REPO_SLUG:-}}"
echo "  ACA_REPO_URL=${ACA_REPO_URL:-${AUTOCODER_REPO_URL:-}}"
echo "  TANDEM_BASE_URL=${TANDEM_BASE_URL:-http://127.0.0.1:39733}"
echo "  TANDEM_API_TOKEN_FILE=${TANDEM_API_TOKEN_FILE:-/run/secrets/tandem_api_token}"
echo "  TANDEM_ENGINE_STARTUP_MODE=${TANDEM_ENGINE_STARTUP_MODE:-reuse_or_start}"
echo "  TANDEM_OUTPUT_ROOT=${TANDEM_OUTPUT_ROOT:-}"
echo "  ACA_MODE=${ACA_MODE:-}"
echo "  ACA_AUTORUN=${ACA_AUTORUN:-false}"
echo

if [ "${ACA_MODE:-}" = "api" ] || [ "${ACA_MODE:-}" = "coordinator" ]; then
    echo "ACA started in coordinator mode — listening on port ${ACA_API_PORT:-39735}"
    exec python3 -m src.aca.api.main
elif [ "${ACA_MODE:-}" = "worker" ]; then
    echo "ACA started in worker mode — running a single worker execution"
    exec python3 -m src.aca.cli worker
elif [ "${ACA_MODE:-}" = "outbox-dispatcher" ] || [ "${ACA_MODE:-}" = "outbox" ]; then
    echo "ACA started in outbox-dispatcher mode — draining GitHub sync outbox"
    exec python3 -m src.aca.cli outbox-dispatcher
elif [ "${ACA_AUTORUN:-false}" = "true" ]; then
    echo "ACA_AUTORUN is set — starting explicit execution mode"
    exec python3 -m src.aca.cli run
else
    echo "ACA started in passive mode — waiting for explicit run command"
    echo "To trigger a run inside the container, use:"
    echo "  python3 -m src.aca.cli run"
    echo ""
    echo "For next-task preview, use:"
    echo "  python3 -m src.aca.cli next-task"
    echo ""
    echo "Container staying alive. Exec in with: docker compose exec aca bash"
    exec sleep infinity
fi
