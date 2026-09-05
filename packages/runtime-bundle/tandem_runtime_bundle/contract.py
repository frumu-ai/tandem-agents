"""Pure rendering: no credentials, filesystem reads, network, or auth authority."""
import base64
import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit

CONTRACT_VERSION = 1
ENGINE_VERSION = "0.7.2"
PANEL_REVISION = "3ee2d83d76497565680538ef00f1616f55650524"
PROFILE = "hosted-single-node-v1"
STATE_DIR = "/home/node/.local/share/tandem/data"
SECURITY_DIR = "/run/tandem-security"
REPLAY_DIR = "/var/lib/tandem-replay"
ANCHOR_DIR = "/var/lib/tandem-audit"
PANEL_AUTH_DIR = "/run/tandem-panel-auth"


def _required(values, name):
    value = values.get(name, "")
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} is required")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _path(value, name):
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts or value == "/":
        raise ValueError(f"{name} must be a normalized absolute Linux path")
    if any(char in value for char in "$:\\"):
        raise ValueError(f"{name} contains unsupported path characters")
    return path


def _url(value, name):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (not host or not parsed.netloc.isascii() or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or "\\" in value or any(char.isspace() for char in value)
                or not re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]+)?", parsed.netloc)):
            raise ValueError()
        loopback = host in {"127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError()
        if port == 0:
            raise ValueError()
    except ValueError:
        raise ValueError(f"{name} requires HTTPS or explicit HTTP loopback") from None
    return value.rstrip("/")


def validate_keyring(keyring, deployment_id, organization_id, audience="tandem-runtime"):
    """Enforce deployment scope that the general engine metadata parser permits omitting."""
    if not isinstance(keyring, dict) or not keyring:
        raise ValueError("context keyring must be a nonempty metadata object")
    active = False
    for kid, entry in keyring.items():
        if not isinstance(kid, str) or not kid or not isinstance(entry, dict):
            raise ValueError("context keyring requires named metadata entries")
        if (entry.get("purpose") != "context_assertion"
                or entry.get("deployment_id") != deployment_id
                or entry.get("organization_id") != organization_id
                or entry.get("allowed_audiences") != [audience]
                or entry.get("status") not in ("active", "retired", "revoked")):
            raise ValueError("context keyring has an invalid purpose, status or deployment scope")
        try:
            public_key = entry["public_key"]
            raw = base64.b64decode(public_key + "=" * (-len(public_key) % 4), altchars=b"-_", validate=True)
            if len(raw) != 32:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError("context keyring requires a 32-byte Ed25519 public key") from None
        active = active or entry["status"] == "active"
    if not active:
        raise ValueError("context keyring requires an active key")


def validate_release(values):
    """Validate immutable image inputs before advertising contract compatibility."""
    version = str(values.get("HOSTED_RUNTIME_SECURITY_VERSION", CONTRACT_VERSION))
    if version not in ("1", "2"):
        raise ValueError("unsupported runtime security contract version")
    if version == "2":
        from .policy_contract import POLICY_ENGINE_REVISION
        if values.get("HOSTED_TANDEM_ENGINE_SOURCE_REVISION") != POLICY_ENGINE_REVISION:
            raise ValueError("runtime security v2 requires the tested policy-capable engine source revision")
    if values.get("HOSTED_PLATFORM", "linux/amd64") != "linux/amd64":
        raise ValueError("runtime security v1 supports linux/amd64 only")
    if values.get("HOSTED_TANDEM_CONTROL_PANEL_SOURCE_REVISION") != PANEL_REVISION:
        raise ValueError("runtime security v1 requires the reviewed panel source revision; npm 0.7.2 is insufficient")
    for key in ("HOSTED_TANDEM_ENGINE_RELEASE_VERSION", "HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION"):
        if _required(values, key) != ENGINE_VERSION:
            raise ValueError(f"{key} must be the tested version {ENGINE_VERSION}")
    images = {}
    for name in ("ENGINE", "CONTROL_PANEL", "PROXY", "ACA", "KB"):
        value = values.get(f"HOSTED_{name}_IMAGE")
        if not value and name in {"ACA", "KB"}:
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}", value):
            raise ValueError(f"HOSTED_{name}_IMAGE must be pinned by sha256 digest")
        images[name.lower()] = value
    return images


def build_security_bundle(values):
    """Render the versioned contract shared by Python and shell callers."""
    images = validate_release(values)
    if values.get("HOSTED_RUNTIME_AUTH_MODE", values.get("TANDEM_RUNTIME_AUTH_MODE", "hosted_single_tenant")) != "hosted_single_tenant":
        raise ValueError("this profile requires hosted_single_tenant auth")
    if values.get("HOSTED_STORAGE_PROFILE", "local") != "local":
        raise ValueError("runtime security v1 supports local storage only")
    if str(values.get("HOSTED_ENABLE_OUTBOX", "false")).lower() != "false":
        raise ValueError("runtime security v1 does not support a separate outbox")
    deployment_id = str(uuid.UUID(_required(values, "HOSTED_DEPLOYMENT_ID")))
    organization_id = str(uuid.UUID(_required(values, "HOSTED_ORGANIZATION_ID")))
    root = _path(_required(values, "HOSTED_INSTALL_ROOT"), "HOSTED_INSTALL_ROOT")
    state = _path(values.get("HOSTED_ENGINE_STATE_ROOT", f"{root}/tandem-engine-state"), "engine state")
    security = _path(values.get("HOSTED_SECURITY_ROOT", f"{root}/runtime-security"), "security root")
    replay = _path(values.get("HOSTED_REPLAY_ROOT", f"{root}/context-replay"), "replay root")
    panel_auth = _path(values.get("HOSTED_PANEL_AUTH_ROOT", f"{root}/panel-auth"), "panel auth root")
    anchor = _path(_required(values, "HOSTED_AUDIT_ANCHOR_ROOT"), "audit anchor root")
    roots = [state, security, replay, anchor, panel_auth]
    ordinary_paths = {
        name: str(_path(values.get(f"HOSTED_{name}_ROOT", f"{root}/{suffix}"), name))
        for name, suffix in {
            "DATA": "tandem-data", "REPOS": "repos", "RUNS": "runs", "SECRETS": "secrets",
            "PANEL_STATE": "tandem-panel-state", "KB_DOCS": "kb-docs", "KB_INDEX": "kb-index",
            "PROXY_DATA": "proxy/data", "PROXY_CONFIG": "proxy/config",
        }.items()
    }
    for path in roots:
        for ordinary in map(PurePosixPath, ordinary_paths.values()):
            if path == ordinary or path in ordinary.parents or ordinary in path.parents:
                raise ValueError("security storage must be independent of ordinary workload mounts")
    for index, path in enumerate(roots):
        for other in roots[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError("state, security, replay and anchor roots must be independent")
    if anchor == root or root in anchor.parents or anchor in root.parents:
        raise ValueError("audit anchor root must be independent of the installation tree")
    uid = int(values.get("HOSTED_HOST_UID", "1000"))
    gid = int(values.get("HOSTED_HOST_GID", "1000"))
    if uid <= 0 or gid <= 0:
        raise ValueError("hosted runtime requires a non-root UID/GID")
    control_plane = _url(_required(values, "HOSTED_CONTROL_PLANE_URL"), "control plane URL")
    login_base = _url(values.get("HOSTED_LOGIN_BASE_URL", control_plane), "login base URL")
    public_url = _url(_required(values, "HOSTED_CONTROL_PANEL_PUBLIC_URL"), "panel public URL")
    bundle = {
        "schema_version": CONTRACT_VERSION, "profile": PROFILE, "platform": "linux/amd64",
        "engine_version": ENGINE_VERSION, "control_panel_version": ENGINE_VERSION,
        "control_panel_source_revision": PANEL_REVISION,
        "deployment_id": deployment_id, "organization_id": organization_id,
        "images": images, "uid": uid, "gid": gid,
        "ordinary_paths": ordinary_paths,
        "host_paths": {"state": str(state), "security": str(security), "replay": str(replay),
                       "anchor": str(anchor), "panel_auth": str(panel_auth)},
        "engine_environment": {
            "TANDEM_RUNTIME_AUTH_MODE": "hosted_single_tenant",
            "TANDEM_STATE_DIR": STATE_DIR,
            "TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS_FILE": f"{SECURITY_DIR}/context-keyring.json",
            "TANDEM_CONTEXT_ASSERTION_ISSUER": "tandem-web",
            "TANDEM_CONTEXT_ASSERTION_AUDIENCE": "tandem-runtime",
            "TANDEM_CONTEXT_ASSERTION_REPLAY_MODE": "bound",
            "TANDEM_CONTEXT_ASSERTION_REPLAY_STORE_FILE": f"{REPLAY_DIR}/assertions.sqlite3",
            "TANDEM_AUDIT_HMAC_KEY_FILE": f"{SECURITY_DIR}/audit-hmac-key",
            "TANDEM_AUDIT_HMAC_KEY_ID": "hosted-audit-v1",
            "TANDEM_AUDIT_ANCHOR_DIR": ANCHOR_DIR,
        },
        "engine_mounts": [
            {"type": "bind", "source": str(security), "target": SECURITY_DIR, "read_only": True, "bind": {"create_host_path": False}},
            {"type": "bind", "source": str(replay), "target": REPLAY_DIR, "bind": {"create_host_path": False}},
            {"type": "bind", "source": str(anchor), "target": ANCHOR_DIR, "bind": {"create_host_path": False}},
        ],
        "panel_mounts": [
            {"type": "bind", "source": str(panel_auth), "target": PANEL_AUTH_DIR,
             "read_only": True, "bind": {"create_host_path": False}},
        ],
        "container_security": {
            "user": f"{uid}:{gid}", "read_only": True,
            "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"],
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=256m"], "pids_limit": 512,
        },
        "panel_hosted": {
            "managed": True, "access_mode": "managed",
            "deployment_id": deployment_id, "organization_id": organization_id,
            "public_url": public_url, "control_plane_url": control_plane,
            "auth": {
                "mode": "hosted", "control_plane_url": control_plane,
                "panel_login_url": f"{login_base}/hosted/panel/authorize",
                "panel_exchange_url": f"{control_plane}/api/v1/hosted/deployments/{deployment_id}/panel/exchange",
                "panel_refresh_url": f"{control_plane}/api/v1/hosted/deployments/{deployment_id}/panel/refresh",
                "host_agent_token_file": f"{PANEL_AUTH_DIR}/host-agent-token",
            },
        },
    }
    if str(values.get("HOSTED_RUNTIME_SECURITY_VERSION", CONTRACT_VERSION)) == "2":
        from .policy_contract import apply_policy_profile
        return apply_policy_profile(bundle, values)
    return bundle
