"""Exact-source non-root memory crypto with v3 KMS inputs and a disposable KMS.

The process uses synthetic local authorization to isolate crypto acceptance;
hosted policy, grants, and two-user privacy need separate deployment evidence.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION
from tandem_runtime_bundle.prepare import prepare_security
from engine_integration import Engine
from fixtures import DEPLOYMENT, ORGANIZATION, inputs, keyring, synthetic_v3_provenance
from policy_engine_integration import TOKEN, wait_for


def contains(path, needle):
    carry = b""
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if needle in carry + block:
                return True
            carry = block[-len(needle) + 1:] if len(needle) > 1 else b""
    return False


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
    path.chmod(0o600)
    if material is None:
        path.unlink()
    else:
        path.write_bytes(material)
        path.chmod(0o440)


def memory_request(engine, method, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{engine.port}{path}", method=method,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + engine.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class MemoryEncryptionEngineTests(unittest.TestCase):
    def test_encrypted_write_cold_restart_and_missing_or_wrong_key(self):
        # GitHub Actions may set TMPDIR under a runner-owned workspace. The
        # production KMS guard correctly rejects that writable ancestor, so
        # stage this root-only fixture beneath the sticky, root-owned /tmp.
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            home, install = root / "runtime-home", root / "install"
            for directory in (home, install):
                directory.mkdir(mode=0o700)
                os.chown(directory, 1000, 1000)
            # The v3 contract is hosted. This process keeps its v3 memory KMS
            # inputs but uses local authorization so the test isolates crypto.
            with synthetic_v3_provenance():
                values = inputs(install, root / "independent-anchors")
                values.update(HOSTED_RUNTIME_SECURITY_VERSION="3",
                              HOSTED_TANDEM_ENGINE_SOURCE_REVISION=MEMORY_ENGINE_REVISION)
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
                engine = Engine(home, bundle)
                executable = root / "tandem-engine"
                shutil.copyfile(engine.binary, executable)
                executable.chmod(0o755)
                engine.binary = str(executable)
                os.chown(bundle["host_paths"]["state"], 1000, 1000)
                os.chown(engine.env["TANDEM_STATE_DIR"], 1000, 1000)
                engine.process_options = {"user": 1000, "group": 1000, "extra_groups": []}
                engine.env["TANDEM_RUNTIME_AUTH_MODE"] = "local_single_tenant"
                for name in ("TANDEM_HOSTED_POLICY_FILE", "TANDEM_HOSTED_ORGANIZATION_ID",
                             "TANDEM_HOSTED_DEPLOYMENT_ID",
                             "TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS_FILE"):
                    engine.env.pop(name, None)
                partition = {"org_id": ORGANIZATION, "workspace_id": DEPLOYMENT,
                             "project_id": "encrypted-memory", "tier": "session"}
                secret = uuid.uuid4().hex
                marker = "recovery lantern " + secret
                payload = {"run_id": "encrypted-memory-acceptance", "partition": partition,
                           "kind": "note", "content": marker, "classification": "internal",
                           "private": True}

                def start():
                    engine.start()

                def recall():
                    return memory_request(engine, "POST", "/memory/search",
                        {"run_id": "encrypted-memory-acceptance", "partition": partition,
                         "read_scopes": ["session"], "query": "recovery lantern", "limit": 10})

                try:
                    start()
                    status, body = memory_request(engine, "POST", "/memory/put", payload)
                    self.assertEqual(status, 200, body)
                    self.assertEqual(recall()[0], 200)
                    self.assertIn(marker, recall()[1])
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
                    memory_db = Path(bundle["host_paths"]["state"]) / "data/memory.sqlite"
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

if __name__ == "__main__":
    if os.name != "posix" or os.geteuid() != 0 or not os.environ.get("TANDEM_TEST_ENGINE"):
        raise SystemExit("Disposable Linux root and an exact-source TANDEM_TEST_ENGINE are required")
    unittest.main(verbosity=2)
