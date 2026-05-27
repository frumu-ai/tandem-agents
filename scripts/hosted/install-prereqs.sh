#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

install_source="${HOSTED_DOCKER_INSTALL_SOURCE:-distro}"

usage() {
  cat <<'EOF'
Usage:
  install-prereqs.sh

Install Docker and the Compose plugin on a fresh Debian or Ubuntu host.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

have_docker=false
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  have_docker=true
fi

if [[ ! -r /etc/os-release ]]; then
  hosted::die "unsupported host: /etc/os-release is missing"
fi

# shellcheck disable=SC1091
source /etc/os-release
os_id="${ID:-}"
codename="${VERSION_CODENAME:-}"

if [[ -z "$os_id" || -z "$codename" ]]; then
  hosted::die "could not determine host distribution from /etc/os-release"
fi

apt_install() {
  hosted::as_root env DEBIAN_FRONTEND=noninteractive apt-get update
  hosted::as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

case "$os_id" in
  ubuntu|debian)
    hosted::log "Installing Docker prereqs on ${os_id} ${codename} using ${install_source} packages."
    apt_install ca-certificates curl gnupg lsb-release python3 fail2ban unattended-upgrades

    if [[ "$have_docker" == false ]]; then
      if [[ "$install_source" == "official" ]]; then
        hosted::as_root install -d -m 0755 /etc/apt/keyrings
        if [[ "$os_id" == "ubuntu" ]]; then
          docker_repo_os="ubuntu"
        else
          docker_repo_os="debian"
        fi
        hosted::as_root sh -c "curl -fsSL https://download.docker.com/linux/${docker_repo_os}/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg"
        hosted::as_root chmod a+r /etc/apt/keyrings/docker.gpg
        hosted::as_root sh -c "echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${docker_repo_os} ${codename} stable\" > /etc/apt/sources.list.d/docker.list"
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      else
        apt_install docker.io docker-compose-plugin
      fi
    fi
    ;;
  *)
    hosted::die "unsupported distribution: ${os_id}"
    ;;
esac

if [[ "$have_docker" == true ]]; then
  hosted::log "Docker and Compose are already installed."
fi

if command -v systemctl >/dev/null 2>&1; then
  hosted::as_root systemctl enable --now docker >/dev/null 2>&1 || true
fi

host_hardening_script="${SCRIPT_DIR}/host-hardening.sh"
if [[ -f "$host_hardening_script" ]]; then
  bash "$host_hardening_script"
else
  hosted::die "missing host hardening script: ${host_hardening_script}"
fi

if ! hosted::as_root docker version >/dev/null 2>&1; then
  hosted::die "docker daemon is not available after installation"
fi

if ! hosted::as_root docker compose version >/dev/null 2>&1; then
  hosted::die "docker compose plugin is not available after installation"
fi

hosted::log "Docker and Compose installation complete."
