"""One authenticated fetch; a separate bounded systemd timer owns scheduling."""
import argparse
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import ssl
import stat
import time
from urllib.parse import urlsplit
import uuid

from .contract import _url
from .prepare import _directory, _plain_path, _write

MAX_BYTES = 4 * 1024 * 1024
MAX_AGE_SECONDS = 120
FETCH_TIMEOUT_SECONDS = 8


class PolicySyncError(ValueError):
    """Only fixed diagnostic codes are logged; never credentials or responses."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicySyncError("duplicate_policy_field")
        result[key] = value
    return result


def decode_policy(raw, organization_id, deployment_id, *, now=None):
    if len(raw) > MAX_BYTES:
        raise PolicySyncError("policy_too_large")
    try:
        policy = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(policy, dict) or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
            raise ValueError()
        revision = policy.get("policy_version")
        if type(revision) is not int or revision < 1:
            raise ValueError()
        if policy.get("organization_id") != organization_id or policy.get("deployment_id") != deployment_id:
            raise PolicySyncError("policy_scope_mismatch")
        generated = datetime.fromisoformat(policy["generated_at"].replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError()
        age = (now or datetime.now(timezone.utc)).timestamp() - generated.timestamp()
        if age < -5 or age >= MAX_AGE_SECONDS:
            raise PolicySyncError("policy_not_fresh")
        for name in ("users", "org_units", "org_unit_memberships", "deployment_grants"):
            if not isinstance(policy[name], list):
                raise ValueError()
    except PolicySyncError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        raise PolicySyncError("invalid_policy_document") from None
    return policy


def fetch_policy(control_plane, organization_id, deployment_id, token, *, tls_context=None):
    """TLS-verifying, direct transport: no redirects, proxies or credential forwarding."""
    try:
        url = urlsplit(_url(control_plane, "control plane URL"))
        if url.scheme != "https":
            raise ValueError()
        if str(uuid.UUID(organization_id)) != organization_id or str(uuid.UUID(deployment_id)) != deployment_id:
            raise ValueError()
        if not 32 <= len(token) <= 4096 or any(ord(char) <= 32 or ord(char) >= 127 for char in token):
            raise ValueError()
    except (TypeError, ValueError):
        raise PolicySyncError("invalid_policy_fetch_configuration") from None
    connection = http.client.HTTPSConnection(url.hostname, url.port or 443,
        timeout=FETCH_TIMEOUT_SECONDS, context=tls_context or ssl.create_default_context())
    started = time.monotonic()
    try:
        connection.request("GET", f"{url.path.rstrip('/')}/api/v1/hosted/agent/deployments/{deployment_id}/policy-bundle",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Cache-Control": "no-cache"})
        response = connection.getresponse()
        if response.status != 200:
            raise PolicySyncError("policy_control_plane_http_error")
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise PolicySyncError("unsupported_policy_encoding")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise PolicySyncError("invalid_policy_content_type")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
            raise PolicySyncError("policy_too_large")
        raw = response.read(MAX_BYTES + 1)
        if length is not None and len(raw) != int(length):
            raise PolicySyncError("incomplete_policy_response")
        if time.monotonic() - started > FETCH_TIMEOUT_SECONDS:
            raise PolicySyncError("policy_fetch_timeout")
        decode_policy(raw, organization_id, deployment_id)
        return raw
    except PolicySyncError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise PolicySyncError("policy_transport_failed") from None
    finally:
        connection.close()


def _operator_file(path, limit):
    path = _plain_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise PolicySyncError("policy_agent_input_permissions")
        raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise PolicySyncError("policy_agent_input_too_large")
        return raw


def sync_once(config_path):
    if os.name != "posix" or os.geteuid() != 0:
        raise PolicySyncError("policy_agent_requires_linux_operator")
    config = json.loads(_operator_file(config_path, 16384), object_pairs_hook=_unique_object)
    token = _operator_file(config["token_file"], 4096).decode("ascii").strip()
    output = _plain_path(config["output_file"])
    _directory(output.parent, config["uid"], config["gid"])
    raw = fetch_policy(config["control_plane_url"], config["organization_id"], config["deployment_id"], token)
    # Reuse secure provisioning's 0600, single-link, fsync + atomic replacement.
    # Never replace the previous snapshot on an incomplete or failed fetch.
    _write(output, raw, config["uid"], config["gid"])
    descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        sync_once(Path(args.config))
    except PolicySyncError as error:
        parser.exit(1, f"Policy synchronization failed: {error}\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "Policy synchronization failed: invalid_local_configuration\n")


if __name__ == "__main__":
    main()
