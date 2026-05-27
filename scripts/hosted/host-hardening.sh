#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

usage() {
  cat <<'EOF'
Usage:
  host-hardening.sh

Apply the minimal customer-VM hardening baseline for hosted Tandem.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

hosted::log "applying host hardening baseline"

hosted::as_root python3 - <<'PY'
from pathlib import Path

files = {
    "/etc/ssh/sshd_config.d/99-tandem-hosted.conf": """\
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
X11Forwarding no
""",
    "/etc/fail2ban/jail.d/99-tandem-hosted.conf": """\
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
mode = aggressive
""",
    "/etc/apt/apt.conf.d/20auto-upgrades": """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
""",
}

for path, content in files.items():
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
PY

if command -v sshd >/dev/null 2>&1; then
  hosted::as_root sshd -t
fi

if command -v systemctl >/dev/null 2>&1; then
  hosted::as_root systemctl enable --now fail2ban >/dev/null 2>&1 || true
  hosted::as_root systemctl reload ssh >/dev/null 2>&1 || hosted::as_root systemctl reload sshd >/dev/null 2>&1 || true
elif command -v service >/dev/null 2>&1; then
  hosted::as_root service fail2ban restart >/dev/null 2>&1 || true
fi

if command -v fail2ban-client >/dev/null 2>&1; then
  hosted::as_root fail2ban-client ping >/dev/null 2>&1 || true
fi

hosted::log "host hardening baseline applied."
