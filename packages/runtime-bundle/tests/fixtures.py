import base64
import os
from pathlib import Path

from tandem_runtime_bundle import build_security_bundle

DEPLOYMENT = "b3a60fb8-2b41-43b3-9c9e-88bccfc37c15"
ORGANIZATION = "9faebcf0-2b32-482f-82b8-df032d154abc"


def inputs(root="/srv/tandem/test", anchor="/var/lib/tandem-audit/test"):
    values = {
        "HOSTED_RUNTIME_SECURITY_VERSION": "1", "HOSTED_DEPLOYMENT_ID": DEPLOYMENT,
        "HOSTED_ORGANIZATION_ID": ORGANIZATION, "HOSTED_INSTALL_ROOT": str(root),
        "HOSTED_AUDIT_ANCHOR_ROOT": str(anchor),
        "HOSTED_CONTROL_PANEL_PUBLIC_URL": "https://panel.example.com",
        "HOSTED_CONTROL_PLANE_URL": "https://identity.example.com",
        "HOSTED_TANDEM_ENGINE_RELEASE_VERSION": "0.7.2",
        "HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION": "0.7.2",
        "HOSTED_RELEASE_TAG": "v0.7.2", "HOSTED_RELEASE_VERSION": "0.7.2",
        "HOSTED_DEPLOYMENT_SLUG": "test", "HOSTED_STORAGE_PROFILE": "local",
        "HOSTED_HOST_UID": str(os.getuid()) if os.name == "posix" and os.getuid() else "1000",
        "HOSTED_HOST_GID": str(os.getgid()) if os.name == "posix" and os.getgid() else "1000",
    }
    values.update({f"HOSTED_{name}_IMAGE": f"ghcr.io/example/{name.lower()}@sha256:{'a' * 64}"
                   for name in ("ENGINE", "CONTROL_PANEL", "ACA", "PROXY", "KB")})
    return values


def keyring(public_key=None):
    public_key = public_key or bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    return {"test-key": {
        "public_key": base64.b64encode(public_key).decode(), "purpose": "context_assertion",
        "deployment_id": DEPLOYMENT, "organization_id": ORGANIZATION,
        "allowed_audiences": ["tandem-runtime"], "status": "active",
    }}


def provisioned_paths(root):
    root = Path(root)
    values = inputs(root / "install", root / "independent-anchors")
    values["HOSTED_SECRETS_ROOT"] = str(root / "install" / "secrets")
    bundle = build_security_bundle(values)
    source = root / "host-agent-source"
    source.write_text("synthetic-host-agent-token-" + "a" * 32)
    source.chmod(0o600)
    return values, bundle, source
