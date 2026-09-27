"""Exact-source non-root memory crypto with v3 KMS and a synthetic hosted tenant.

The policy endpoint and KMS are disposable fixtures; live hosting and two-user
privacy need separate deployment evidence.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION
from tandem_runtime_bundle.policy_service import install_policy_service
from tandem_runtime_bundle.prepare import prepare_security
from engine_integration import Engine
from fixtures import DEPLOYMENT, ORGANIZATION, inputs, keyring, synthetic_v3_provenance
from policy_engine_integration import TOKEN, assertion, policy_document, wait_for
from policy_tls_fixture import tls_endpoint


def contains(path, needle):
    carry = b""
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if needle in carry + block:
                return True
            carry = block[-len(needle) + 1:] if len(needle) > 1 else b""
    return False


def memory_write_warning(log_path, secret):
    """Expose only a bounded storage warning, never the memory test payload."""
    try:
        with log_path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            tail = log.read(8192).decode("utf-8", errors="replace")
    except OSError:
        return "unavailable"
    warnings = [line[line.index("global memory store write failed"):]
                .replace(secret, "[redacted]")[:400]
                for line in tail.splitlines()
                if "global memory store write failed" in line]
    return " | ".join(warnings[-3:]) or "none"


def provision_kms_commands(secrets):
    secrets.mkdir(mode=0o750, parents=True)
    os.chown(secrets, 0, 1000)
    fixture = Path(__file__).with_name("memory_kms_command_fixture.py").read_text()
    for name in ("memory-kms-encrypt", "memory-kms-decrypt"):
        command = secrets / name
        command.write_text(f"#!{sys.executable}\n" + fixture)
        command.chmod(0o550)
        os.chown(command, 0, 1000)
    key = secrets / "memory-kms-key"
    original = os.urandom(32)
    key.write_bytes(original)
    key.chmod(0o440)
    os.chown(key, 0, 1000)
    return key, original


def write_kms_key(path, material):
    if path.exists():
        path.chmod(0o600)
    if material is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(material)
        os.chown(path, 0, 1000)
        path.chmod(0o440)


def memory_request(engine, assertion_token, method, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{engine.port}{path}", method=method,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + engine.token,
            "x-tandem-context-assertion": assertion_token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def memory_policy_document():
    policy = json.loads(policy_document(1, ["alice"]))
    policy["users"][0].update(role="admin", capabilities=["hosted.use", "hosted.admin"])
    return json.dumps(policy).encode()


class MemoryEncryptionEngineTests(unittest.TestCase):
    def test_encrypted_write_cold_restart_and_missing_or_wrong_key(self):
        # GitHub Actions may set TMPDIR under a runner-owned workspace. The
        # production KMS guard correctly rejects that writable ancestor, so
        # stage this root-only fixture beneath the sticky, root-owned /tmp.
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary, \
                tls_endpoint(memory_policy_document, required_token=TOKEN) as (url, tls_context, seen):
            root = Path(temporary)
            root.chmod(0o755)
            home, install = root / "runtime-home", root / "install"
            for directory in (home, install):
                directory.mkdir(mode=0o700)
                os.chown(directory, 1000, 1000)
            with synthetic_v3_provenance():
                values = inputs(install, root / "independent-anchors")
                # The install root belongs to the non-root engine. KMS command
                # ancestors must remain root-owned so that user cannot replace
                # the executable before a later bind mount.
                values.update(HOSTED_RUNTIME_SECURITY_VERSION="3",
                              HOSTED_TANDEM_ENGINE_SOURCE_REVISION=MEMORY_ENGINE_REVISION,
                              HOSTED_MEMORY_KMS_COMMAND_ROOT=str(root / "memory-kms-commands"),
                              HOSTED_CONTROL_PLANE_URL=url)
                bundle = build_security_bundle(values)
                for location in (bundle["host_paths"]["state"], bundle["ordinary_paths"]["DATA"]):
                    directory = Path(location)
                    directory.mkdir(mode=0o755, parents=True)
                    os.chown(directory, 1000, 1000)
                commands = Path(bundle["host_paths"]["memory_kms_commands"])
                key_file, original_key = provision_kms_commands(commands)
                source = root / "operator-token"
                source.write_text(TOKEN)
                source.chmod(0o600)
                signer = Ed25519PrivateKey.generate()
                prepare_security(bundle, keyring(signer.public_key().public_bytes_raw()), source)
                policy = install_policy_service(bundle, root / "operator-agent", source)
                ca = root / "synthetic-ca.pem"
                ca.write_text(ssl.DER_cert_to_PEM_cert(tls_context.get_ca_certs(binary_form=True)[0]))
                engine = Engine(home, bundle)
                executable = root / "tandem-engine"
                shutil.copyfile(engine.binary, executable)
                executable.chmod(0o755)
                engine.binary = str(executable)
                os.chown(bundle["host_paths"]["state"], 1000, 1000)
                os.chown(engine.env["TANDEM_STATE_DIR"], 1000, 1000)
                engine.process_options = {"user": 1000, "group": 1000, "extra_groups": []}
                assertion_token = assertion(signer, "alice", 1, "alice-encrypted-memory", role="admin")
                partition = {"org_id": ORGANIZATION, "workspace_id": DEPLOYMENT,
                             "project_id": "encrypted-memory", "tier": "session"}
                resource = {"organization_id": ORGANIZATION, "workspace_id": DEPLOYMENT,
                            "project_id": "encrypted-memory", "resource_kind": "memory_space",
                            "resource_id": "encrypted-memory"}
                secret = uuid.uuid4().hex
                marker = "recovery lantern " + secret
                payload = {"run_id": "encrypted-memory-acceptance", "partition": partition,
                           "kind": "note", "content": marker, "classification": "internal",
                           "private": True,
                           "metadata": {"knowledge_scope_registry": {
                               "registry_id": "encrypted-memory-acceptance",
                               "resource_ref": resource, "data_class": "internal",
                               "allowed_write_tiers": ["session"]}}}

                def start():
                    engine.start(wait_ready=False)
                    result = subprocess.run([sys.executable, "-s", "-m", "tandem_runtime_bundle.policy_sync",
                        "--config", policy["config_path"]], cwd=root / "operator-agent",
                        env={"PATH": os.environ["PATH"], "SSL_CERT_FILE": str(ca)},
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn(TOKEN, result.stdout + result.stderr)
                    wait_for(lambda: json.loads(engine.request("/global/health")[1])["ready"])

                def recall():
                    return memory_request(engine, assertion_token, "POST", "/memory/search",
                        {"run_id": "encrypted-memory-acceptance", "partition": partition,
                         "read_scopes": ["session"], "query": "recovery lantern", "limit": 10})

                try:
                    start()
                    grant = {"grant_id": "encrypted-memory-read", "unit_id": "eng",
                        "taxonomy_id": "hosted-control-plane", "resource_kind": "memory_space",
                        "resource_id": "encrypted-memory", "project_id": "encrypted-memory",
                        "permissions": ["read"], "data_classes": ["internal"]}
                    grant_status, grant_body = memory_request(engine, assertion_token, "POST",
                        "/enterprise/org-unit-access-grants", grant)
                    self.assertEqual(grant_status, 200, grant_body)
                    status, body = memory_request(engine, assertion_token, "POST", "/memory/put", payload)
                    self.assertEqual(status, 200,
                                     f"{body}; write_warning={memory_write_warning(home / 'engine.log', secret)}")
                    memory_db = Path(bundle["host_paths"]["state"]) / "data/memory.sqlite"
                    with sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True) as connection:
                        stored_rows = connection.execute(
                            "SELECT tenant_org_id, tenant_workspace_id, tenant_deployment_id "
                            "FROM memory_records WHERE run_id = ?",
                            ("encrypted-memory-acceptance",)).fetchall()
                    self.assertEqual(stored_rows, [(ORGANIZATION, DEPLOYMENT, DEPLOYMENT)],
                                     "memory/put returned 200 without persisting its record; "
                                     f"write_warning={memory_write_warning(home / 'engine.log', secret)}")
                    self.assertEqual(recall()[0], 200)
                    self.assertIn(marker, recall()[1], f"persisted_rows={len(stored_rows)}")
                    engine.stop()
                    persisted = [path for mount in (bundle["host_paths"]["state"],
                        bundle["ordinary_paths"]["DATA"]) for path in Path(mount).rglob("*")
                        if path.is_file()]
                    self.assertTrue(persisted, "memory must persist outside the process")
                    files_on_disk = [path for path in root.rglob("*")
                                     if path.is_file() and path != executable]
                    plaintext_paths = [str(path.relative_to(root)) for path in files_on_disk
                                       if contains(path, secret.encode())]
                    self.assertEqual(plaintext_paths, [],
                                     "memory must not persist plaintext in: " + ", ".join(plaintext_paths))
                    self.assertTrue(memory_db.is_file())
                    with sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True) as connection:
                        stored_contents = connection.execute(
                            "SELECT content FROM memory_records WHERE run_id = ?",
                            ("encrypted-memory-acceptance",)).fetchall()
                    self.assertTrue(stored_contents)
                    self.assertTrue(all(content.startswith("tce1:") for (content,) in stored_contents),
                                    "global-memory record content must be an encrypted envelope")
                    for material in (os.urandom(32), None):
                        write_kms_key(key_file, material)
                        engine.start(wait_ready=False)
                        wait_for(lambda: "ENGINE_STARTUP_FAILED" in
                            (home / "engine.log").read_text(errors="replace"), seconds=20)
                        self.assertFalse(json.loads(engine.request("/global/health")[1])["ready"])
                        status, body = recall()
                        self.assertNotIn(marker, body, (status, body))
                        engine.stop()
                    write_kms_key(key_file, original_key)
                    start()
                    status, body = recall()
                    self.assertEqual(status, 200, body)
                    self.assertIn(marker, body, "cold-cache restart must decrypt persisted memory")
                finally:
                    engine.stop()
                self.assertTrue(seen)

if __name__ == "__main__":
    if os.name != "posix" or os.geteuid() != 0 or not os.environ.get("TANDEM_TEST_ENGINE"):
        raise SystemExit("Disposable Linux root and an exact-source TANDEM_TEST_ENGINE are required")
    unittest.main(verbosity=2)
