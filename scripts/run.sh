#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ROOT_DIR}/.env"
  set +a
fi

usage() {
  cat <<'EOF'
Usage:
  run.sh [--setup] [--validate] [--check-engine] [--print-config] [--init-board] [--dry-run]
  run.sh [worker|outbox-dispatcher|scheduler-plan|scheduler-dispatch|coordination-status|operator-status|workspace]
  run.sh [dogfood-linear-graph|repo-graph-eval]
  run.sh [run]

If no arguments are provided, the portable runner starts a run.
EOF
}

if [[ $# -eq 0 ]]; then
  exec python3 -m src.tandem_agents.cli run
fi

case "$1" in
  --setup)
    exec "${ROOT_DIR}/scripts/setup.sh"
    ;;
  --validate)
    exec python3 -m src.tandem_agents.cli validate
    ;;
  --check-engine)
    exec python3 -m src.tandem_agents.cli check-engine
    ;;
  --print-config|--show-config)
    exec python3 -m src.tandem_agents.cli print-config
    ;;
  --init-board)
    exec python3 -m src.tandem_agents.cli init-board
    ;;
  --dry-run)
    exec python3 -m src.tandem_agents.cli run --dry-run
    ;;
  -h|--help)
    usage
    ;;
  run|worker|outbox-dispatcher|validate|check-engine|print-config|init-board|monitor|workspace|coordination-status|coordination-workers|operator-status|scheduler-plan|scheduler-dispatch|lease|blackboard|dogfood-linear-graph|repo-graph-eval)
    exec python3 -m src.tandem_agents.cli "$@"
    ;;
  *)
    exec python3 -m src.tandem_agents.cli run "$@"
    ;;
esac
