"""Authorized host installation for the shared policy timer."""
import json
import os
from pathlib import Path
import subprocess

from .policy_contract import service_files
from .prepare import _directory, _plain_path, _write
from .policy_sync import _operator_file


def install_policy_service(bundle, management_dir, token_file, *, activate=False):
    if os.name != "posix" or os.geteuid() != 0:
        raise ValueError("policy service installation requires the Linux host operator")
    management = _directory(management_dir, 0, 0)
    token = _operator_file(token_file, 4096)
    retained_token = management / "agent-token"
    _write(retained_token, token, 0, 0)
    module = _directory(management / "tandem_runtime_bundle", 0, 0)
    for name in ("__init__.py", "contract.py", "prepare.py", "policy_contract.py", "policy_sync.py", "policy_service.py"):
        _write(module / name, Path(__file__).with_name(name).read_bytes(), 0, 0)
    files = service_files(bundle, management, retained_token)
    _write(Path(files["config_path"]), json.dumps(files["config"]).encode(), 0, 0)
    for suffix in ("service", "timer"):
        _write(management / f"{files['unit']}.{suffix}", files[suffix].encode(), 0, 0)
    if activate:
        # Fixed host-owned systemd directory; no install-root-controlled target.
        unit_dir = _plain_path("/etc/systemd/system")
        for suffix in ("service", "timer"):
            _write(unit_dir / f"{files['unit']}.{suffix}", files[suffix].encode(), 0, 0)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", f"{files['unit']}.timer"], check=True)
    return files
