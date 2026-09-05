"""Run the source-pinned panel image under the rendered security restrictions."""
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from fixtures import keyring, provisioned_paths
from tandem_runtime_bundle.prepare import prepare_security


with tempfile.TemporaryDirectory() as temp:
    values, bundle, source = provisioned_paths(temp)
    prepare_security(bundle, keyring(), source)
    state = Path(temp) / "panel-state"
    state.mkdir(mode=0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options = bundle["container_security"]
    command = ["docker", "run", "--detach", "--rm", "--user", options["user"],
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--tmpfs", options["tmpfs"][0], "--pids-limit", str(options["pids_limit"]),
               "--publish", f"127.0.0.1:{port}:39734",
               "--mount", f"type=bind,src={state},dst=/var/lib/tandem/panel",
               "--env", "TANDEM_ENGINE_URL=http://127.0.0.1:39733",
               "--env", "TANDEM_CONTROL_PANEL_CONFIG_FILE=/run/tandem-panel-auth/control-panel-config.json"]
    for mount in bundle["panel_mounts"]:
        command.extend(["--mount", f"type=bind,src={mount['source']},dst={mount['target']},readonly"])
    command.append("tandem-panel-security-test")
    container = subprocess.check_output(command, text=True).strip()
    try:
        for attempt in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system/health", timeout=3) as response:
                    assert json.load(response)["ok"] is True
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        else:
            raise AssertionError("source-pinned panel did not start under container restrictions")
        # A workload cannot rewrite the trusted authentication destination.
        probe = subprocess.run(["docker", "exec", container, "node", "-e",
                                "require('fs').writeFileSync('/run/tandem-panel-auth/control-panel-config.json', '{}')"],
                               capture_output=True, text=True)
        assert probe.returncode != 0 and "EROFS" in probe.stderr
        print("Pinned panel container started non-root/read-only; trusted configuration write denied.")
    except Exception:
        subprocess.run(["docker", "logs", "--tail", "40", container], check=False)
        raise
    finally:
        subprocess.run(["docker", "stop", container], capture_output=True, check=False)
