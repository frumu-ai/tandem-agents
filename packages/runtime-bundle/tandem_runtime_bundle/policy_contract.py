"""Version 2 policy synchronization overlay and independent service definition."""
import json
from pathlib import PurePosixPath

# Candidate source must pass the exact-source engine integration before release.
# Version 0.7.2 alone is insufficient: its released binary predates policy sync.
POLICY_ENGINE_REVISION = "b50414df0fef4cbb23b0efdb63e650ff894e674e"
POLICY_CONTAINER_DIR = "/run/tandem-hosted-policy"


def apply_policy_profile(bundle, values):
    from .contract import _path

    root = _path(values["HOSTED_INSTALL_ROOT"], "installation root")
    policy = _path(values.get("HOSTED_POLICY_ROOT", f"{root}/hosted-policy"), "hosted policy root")
    for other in map(PurePosixPath, [*bundle["host_paths"].values(), *bundle["ordinary_paths"].values()]):
        if policy == other or policy in other.parents or other in policy.parents:
            raise ValueError("hosted policy storage must be independent of other mounts")
    control_plane = bundle["panel_hosted"]["control_plane_url"]
    if not control_plane.startswith("https://"):
        raise ValueError("runtime policy synchronization requires HTTPS")
    bundle.update(schema_version=2, profile="hosted-single-node-v2", engine_source_revision=POLICY_ENGINE_REVISION)
    bundle["host_paths"]["policy"] = str(policy)
    bundle["engine_environment"].update({
        "TANDEM_HOSTED_ORGANIZATION_ID": bundle["organization_id"],
        "TANDEM_HOSTED_DEPLOYMENT_ID": bundle["deployment_id"],
        "TANDEM_HOSTED_POLICY_FILE": f"{POLICY_CONTAINER_DIR}/current.json",
    })
    bundle["engine_mounts"].append({"type": "bind", "source": str(policy), "target": POLICY_CONTAINER_DIR,
        "read_only": True, "bind": {"create_host_path": False}})
    bundle["policy_sync"] = {
        "schema_version": 1, "control_plane_url": control_plane,
        "organization_id": bundle["organization_id"], "deployment_id": bundle["deployment_id"],
        "output_file": f"{policy}/current.json", "uid": bundle["uid"], "gid": bundle["gid"],
        "poll_interval_seconds": 30, "fetch_service_timeout_seconds": 15,
    }
    return bundle


def service_files(bundle, management_dir, token_file):
    """Render root-owned host files. No credentials are embedded in any unit."""
    if bundle.get("schema_version") != 2 or bundle.get("profile") != "hosted-single-node-v2":
        raise ValueError("policy service requires runtime security v2")
    from .contract import _path

    management = _path(str(management_dir), "management directory")
    token = _path(str(token_file), "host token file")
    config_path = management / "policy-sync.json"
    config = {**bundle["policy_sync"], "token_file": str(token)}

    def quoted(value):
        value = str(value)
        if any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("systemd paths require printable ASCII")
        # systemd expands percent specifiers even inside quoted arguments.
        return json.dumps(value.replace("%", "%%"))

    # Each installation has its own unit, allowing multiple isolated hosts.
    unit = f"tandem-policy-sync-{bundle['deployment_id']}"
    service = f"""[Unit]
Description=Tandem hosted policy synchronization
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={str(management).replace('%', '%%')}
ExecStart=/usr/bin/python3 -s -m tandem_runtime_bundle.policy_sync --config {quoted(config_path)}
TimeoutStartSec=15
TimeoutStopSec=5
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={quoted(bundle['host_paths']['policy'])}
"""
    timer = f"""[Unit]
Description=Refresh Tandem hosted policy independently of release updates

[Timer]
OnBootSec=1s
OnUnitInactiveSec=30s
AccuracySec=1s
Unit={unit}.service

[Install]
WantedBy=timers.target
"""
    return {"unit": unit, "config_path": str(config_path), "config": config,
            "service": service, "timer": timer}
