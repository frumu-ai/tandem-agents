"""Version 2/3 policy overlay; version 3 requires hosted memory encryption."""
import json
import re
from pathlib import PurePosixPath

# Candidate source must pass the exact-source engine integration before release.
# Version 0.7.2 alone is insufficient: its released binary predates policy sync.
POLICY_ENGINE_REVISION = "364d20926455e3154a6bed623945b8d9e3d464e5"
# Combined hosted-grant and encrypted-global-memory source candidate for v3,
# independent of the v2 source pin. Exact-source integration and a verified
# published image remain required before release.
MEMORY_ENGINE_REVISION = "57f21af64766a7bbe3c2f018ddc1a3c3e9fce638"
# Add an entry only after independently verifying the published image was built
# from MEMORY_ENGINE_REVISION. Each entry binds source, in-image binary checksum
# and the reviewed build observation's canonical SHA-256. Empty fails closed.
VERIFIED_MEMORY_ENGINE_IMAGES = {}
POLICY_CONTAINER_DIR = "/run/tandem-hosted-policy"
MEMORY_COMMAND_DIR = PurePosixPath("/run/tandem-memory-kms")


def verify_memory_engine_image(image, revision, binary_sha256=None, attestation_sha256=None):
    approved = VERIFIED_MEMORY_ENGINE_IMAGES.get(image) if isinstance(image, str) else None
    if (revision != MEMORY_ENGINE_REVISION or not isinstance(approved, dict)
            or approved.get("source_revision") != revision
            or not isinstance(binary_sha256, str)
            or approved.get("binary_sha256") != binary_sha256
            or not isinstance(attestation_sha256, str)
            or approved.get("attestation_sha256") != attestation_sha256):
        raise ValueError("runtime security v3 requires a verified exact-source engine image digest and attestation")


def _memory_encryption(bundle, values):
    """Require the complete, non-secret KMS references used by hosted memory."""
    from .contract import _path, _required

    if _required(values, "HOSTED_MEMORY_ENCRYPTION_REQUIRED") != "true":
        raise ValueError("runtime security v3 requires hosted memory encryption")
    provider = _required(values, "HOSTED_MEMORY_KMS_PROVIDER")
    if provider != "google_cloud_kms":
        raise ValueError("runtime security v3 requires the supported Google Cloud KMS provider")
    principal = _required(values, "HOSTED_MEMORY_KMS_RUNTIME_PRINCIPAL_ID")
    if (not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", principal)
            or not principal.endswith(":" + bundle["deployment_id"])):
        raise ValueError("hosted memory principal must be bound to this deployment")
    commands = {}
    for operation in ("ENCRYPT", "DECRYPT"):
        name = f"HOSTED_MEMORY_KMS_{operation}_COMMAND"
        command = _path(_required(values, name), name)
        if (command.parent != MEMORY_COMMAND_DIR
                or not re.fullmatch(r"/run/tandem-memory-kms/[A-Za-z0-9._-]+", str(command))):
            raise ValueError(f"{name} must use the dedicated read-only memory KMS mount")
        commands[operation.lower() + "_command"] = str(command)
    kek_id = _required(values, "HOSTED_MEMORY_KEK_ID")
    if not re.fullmatch(
            r"projects/[A-Za-z0-9._-]+/locations/[A-Za-z0-9._-]+/"
            r"keyRings/[A-Za-z0-9._-]+/cryptoKeys/[A-Za-z0-9._-]+", kek_id):
        raise ValueError("hosted memory KEK id must be a complete Google KMS key resource")
    version = _required(values, "HOSTED_MEMORY_KEK_VERSION")
    version_prefix = kek_id + "/cryptoKeyVersions/"
    short_version = version.removeprefix(version_prefix)
    if not re.fullmatch(r"[1-9][0-9]*", short_version):
        raise ValueError("hosted memory KEK version must belong to the configured key")
    epoch = _required(values, "HOSTED_MEMORY_KEK_ROTATION_EPOCH")
    if not re.fullmatch(r"0|[1-9][0-9]*", epoch) or int(epoch) >= 2**64:
        raise ValueError("hosted memory KEK rotation epoch must be an unsigned integer")
    return {"schema_version": 1, "provider": provider, "runtime_principal_id": principal,
            **commands, "kek_id": kek_id, "kek_version": version,
            "rotation_epoch": int(epoch)}


def apply_policy_profile(bundle, values, version=2):
    from .contract import _path

    root = _path(values["HOSTED_INSTALL_ROOT"], "installation root")
    policy = _path(values.get("HOSTED_POLICY_ROOT", f"{root}/hosted-policy"), "hosted policy root")
    for other in map(PurePosixPath, [*bundle["host_paths"].values(), *bundle["ordinary_paths"].values()]):
        if policy == other or policy in other.parents or other in policy.parents:
            raise ValueError("hosted policy storage must be independent of other mounts")
    control_plane = bundle["panel_hosted"]["control_plane_url"]
    if not control_plane.startswith("https://"):
        raise ValueError("runtime policy synchronization requires HTTPS")
    if version not in (2, 3):
        raise ValueError("unsupported policy profile")
    revision = MEMORY_ENGINE_REVISION if version == 3 else POLICY_ENGINE_REVISION
    bundle.update(schema_version=version, profile=f"hosted-single-node-v{version}",
                  engine_source_revision=revision)
    bundle["host_paths"]["policy"] = str(policy)
    bundle["engine_environment"].update({
        "TANDEM_HOSTED_ORGANIZATION_ID": bundle["organization_id"],
        "TANDEM_HOSTED_DEPLOYMENT_ID": bundle["deployment_id"],
        "TANDEM_HOSTED_POLICY_FILE": f"{POLICY_CONTAINER_DIR}/current.json",
    })
    if version == 3:
        command_root = _path(values.get("HOSTED_MEMORY_KMS_COMMAND_ROOT",
            f"{root}/memory-kms-commands"), "hosted memory KMS command root")
        for other in map(PurePosixPath, [*bundle["host_paths"].values(),
                                         *bundle["ordinary_paths"].values()]):
            if command_root == other or command_root in other.parents or other in command_root.parents:
                raise ValueError("memory KMS command storage must be independent of other mounts")
        bundle["host_paths"]["memory_kms_commands"] = str(command_root)
        memory = _memory_encryption(bundle, values)
        bundle["memory_encryption"] = memory
        bundle["engine_provenance"] = {
            "source_revision": revision,
            "binary_sha256": values["HOSTED_ENGINE_BINARY_SHA256"],
            "attestation_sha256": values["HOSTED_ENGINE_ATTESTATION_SHA256"],
        }
        bundle["engine_environment"].update({
            "TANDEM_MEMORY_ENCRYPTION_REQUIRED": "true",
            "TANDEM_MEMORY_DECRYPT_PROVIDER": memory["provider"],
            "TANDEM_MEMORY_DECRYPT_PRINCIPAL_ID": memory["runtime_principal_id"],
            "TANDEM_MEMORY_GOOGLE_KMS_ENCRYPT_COMMAND": memory["encrypt_command"],
            "TANDEM_MEMORY_GOOGLE_KMS_DECRYPT_COMMAND": memory["decrypt_command"],
            "TANDEM_MEMORY_KEK_ID": memory["kek_id"],
            "TANDEM_MEMORY_KEK_VERSION": memory["kek_version"],
            "TANDEM_MEMORY_KEK_ROTATION_EPOCH": str(memory["rotation_epoch"]),
        })
        bundle["engine_mounts"].append({"type": "bind", "source": str(command_root),
            "target": str(MEMORY_COMMAND_DIR), "read_only": True,
            "bind": {"create_host_path": False}})
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
    version = bundle.get("schema_version")
    if version not in (2, 3) or bundle.get("profile") != f"hosted-single-node-v{version}":
        raise ValueError("policy service requires runtime security v2 or v3")
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
