"""Run with a checksum-verified 0.7.2 engine on Linux; no live providers/accounts."""
import base64
import copy
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tandem_runtime_bundle.prepare import prepare_security
from fixtures import DEPLOYMENT, ORGANIZATION, keyring, provisioned_paths


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def assertion(private_key, assertion_id, role="workspace:user"):
    now = int(time.time() * 1000)
    principal = {"actor_id": "synthetic-user", "source": "tandem-web"}
    claims = {
        "version": "v1", "issuer": "tandem-web", "audience": "tandem-runtime",
        "issued_at_ms": now, "expires_at_ms": now + 240_000, "assertion_id": assertion_id,
        "tenant_context": {"org_id": ORGANIZATION, "workspace_id": DEPLOYMENT,
                           "deployment_id": DEPLOYMENT, "actor_id": "synthetic-user", "source": "explicit"},
        "human_actor": {"actor_id": "synthetic-user", "provider": "tandem"},
        "authority_chain": {"initiated_by": principal, "executed_as": {"kind": "request", **principal}},
        "roles": [role],
    }
    header = {"alg": "EdDSA", "typ": "tandem-tenant-context+jws", "kid": "test-key"}
    content = ".".join(encode(json.dumps(value, separators=(",", ":")).encode()) for value in (header, claims))
    return content + "." + encode(private_key.sign(content.encode()))


class Engine:
    def __init__(self, root, bundle):
        self.root = Path(root)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.token = "synthetic-engine-token-" + "x" * 40
        self.env = {"PATH": os.environ["PATH"], "HOME": str(root), "RUST_LOG": "warn",
                    "TANDEM_API_TOKEN": self.token, "HF_HUB_OFFLINE": "1"}
        paths = bundle["host_paths"]
        translations = {"/run/tandem-security": paths["security"],
                        "/var/lib/tandem-replay": paths["replay"],
                        "/var/lib/tandem-audit": paths["anchor"],
                        "/home/node/.local/share/tandem": paths["state"]}
        for name, value in bundle["engine_environment"].items():
            for container, host in translations.items():
                value = value.replace(container, host)
            self.env[name] = value
        Path(self.env["TANDEM_STATE_DIR"]).mkdir(parents=True, exist_ok=True)
        self.process = None

    def start(self, failure=False):
        self.log = open(self.root / "engine.log", "w+")
        self.process = subprocess.Popen(
            [os.environ["TANDEM_TEST_ENGINE"], "serve", "--hostname", "127.0.0.1", "--port", str(self.port)],
            env=self.env, cwd=self.root, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                detail = self.log.read()
                if failure and self.process.returncode != 0:
                    return detail
                raise AssertionError("Engine exited unexpectedly: " + detail[-5000:])
            try:
                status, body = self.request("/global/health")
                if status == 200 and json.loads(body).get("ready") is True:
                    if failure:
                        raise AssertionError("Invalid hosted configuration became healthy")
                    return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.1)
        self.log.seek(0)
        raise AssertionError("Engine did not resolve startup within 60 seconds: " + self.log.read()[-5000:])

    def request(self, path="/session", token=None):
        headers = {"Authorization": "Bearer " + self.token}
        if token:
            headers["x-tandem-context-assertion"] = token
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if hasattr(self, "log"):
            self.log.close()


class CurrentEngineTests(unittest.TestCase):
    def setup_runtime(self, root):
        values, bundle, source = provisioned_paths(root)
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        prepare_security(bundle, keyring(public_key), source, values["HOSTED_SECRETS_ROOT"])
        return bundle, private_key, Engine(root, bundle)

    def test_signed_context_and_bound_replay_survive_process_restart(self):
        with tempfile.TemporaryDirectory() as root:
            bundle, private_key, engine = self.setup_runtime(root)
            original = assertion(private_key, "restart-test")
            altered = assertion(private_key, "restart-test", "workspace:admin")
            try:
                engine.start()
                self.assertEqual(engine.request()[0], 403, "bearer-only must not provide tenant identity")
                self.assertEqual(engine.request(token=original)[0], 200)
                self.assertEqual(engine.request(token=original)[0], 200, "bound retries remain supported")
                self.assertEqual(engine.request(token=altered)[0], 403)
                engine.stop()
                replay = Path(bundle["host_paths"]["replay"]) / "assertions.sqlite3"
                self.assertTrue(replay.read_bytes().startswith(b"SQLite format 3"))
                self.assertEqual(replay.stat().st_mode & 0o777, 0o600)
                engine.start()
                self.assertEqual(engine.request(token=original)[0], 200)
                self.assertEqual(engine.request(token=altered)[0], 403)
                self.assertTrue(any(Path(bundle["host_paths"]["anchor"]).iterdir()))
            finally:
                engine.stop()

    def test_each_missing_prerequisite_and_unsafe_storage_prevents_startup(self):
        cases = ["keyring", "replay", "audit", "anchor", "bearer", "replay-off", "anchor-in-state", "keyring-permissions", "audit-permissions"]
        missing = {"keyring": "TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS_FILE",
                   "replay": "TANDEM_CONTEXT_ASSERTION_REPLAY_STORE_FILE",
                   "audit": "TANDEM_AUDIT_HMAC_KEY_FILE", "anchor": "TANDEM_AUDIT_ANCHOR_DIR",
                   "bearer": "TANDEM_API_TOKEN"}
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                _, _, engine = self.setup_runtime(root)
                if case in missing:
                    engine.env.pop(missing[case])
                elif case == "replay-off":
                    engine.env["TANDEM_CONTEXT_ASSERTION_REPLAY_MODE"] = "off"
                elif case == "anchor-in-state":
                    engine.env["TANDEM_AUDIT_ANCHOR_DIR"] = engine.env["TANDEM_STATE_DIR"] + "/anchors"
                elif case == "keyring-permissions":
                    Path(engine.env["TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS_FILE"]).chmod(0o644)
                else:
                    Path(engine.env["TANDEM_AUDIT_HMAC_KEY_FILE"]).chmod(0o644)
                try:
                    message = engine.start(failure=True)
                    self.assertTrue(message.strip(), "failure must include an actionable diagnostic")
                finally:
                    engine.stop()


if __name__ == "__main__":
    if not os.environ.get("TANDEM_TEST_ENGINE") or os.name != "posix":
        raise SystemExit("A verified TANDEM_TEST_ENGINE binary and Linux are required")
    unittest.main(verbosity=2)
