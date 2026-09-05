"""Real v2 engine process and copied root policy agent, using synthetic TLS only."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import POLICY_ENGINE_REVISION
from tandem_runtime_bundle.policy_service import install_policy_service
from tandem_runtime_bundle.prepare import prepare_security
from engine_integration import Engine, encode
from fixtures import DEPLOYMENT, ORGANIZATION, inputs, keyring
from policy_tls_fixture import tls_endpoint

TOKEN = "synthetic-policy-agent-token-" + "p" * 48


def assertion(key, actor, version, assertion_id):
    now = int(time.time() * 1000)
    principal = {"actor_id": actor, "source": "tandem-web"}
    claims = {"version": "v1", "issuer": "tandem-web", "audience": "tandem-runtime",
        "issued_at_ms": now, "expires_at_ms": now + 240_000, "assertion_id": assertion_id,
        "tenant_context": {"org_id": ORGANIZATION, "workspace_id": DEPLOYMENT,
            "deployment_id": DEPLOYMENT, "actor_id": actor, "source": "explicit"},
        "human_actor": {"actor_id": actor, "provider": "tandem"},
        "authority_chain": {"initiated_by": principal, "executed_as": {"kind": "request", **principal}},
        "roles": ["hosted:role:member"], "capabilities": ["hosted.use"], "policy_version": version}
    header = {"alg": "EdDSA", "typ": "tandem-tenant-context+jws", "kid": "test-key"}
    content = ".".join(encode(json.dumps(value, separators=(",", ":")).encode()) for value in (header, claims))
    return content + "." + encode(key.sign(content.encode()))


def policy_document(version, actors):
    return json.dumps({"schema_version": 1, "policy_version": version,
        "organization_id": ORGANIZATION, "deployment_id": DEPLOYMENT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "users": [{"id": actor, "email": None, "username": None, "role": "member",
            "capabilities": ["hosted.use"], "is_active": True, "email_verified": True} for actor in actors],
        "org_units": [], "org_unit_memberships": [], "deployment_grants": []}).encode()


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Expected runtime policy transition did not occur before deadline")


def session_request(engine, token, method, path, body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{engine.port}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + engine.token, "x-tandem-context-assertion": token,
            "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class PolicyEngineTests(unittest.TestCase):
    def test_authenticated_projection_revocation_restart_and_real_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            installation, home = root / "install", root / "runtime-home"
            for directory in (installation, home):
                directory.mkdir(mode=0o700)
                os.chown(directory, 1000, 1000)
            state = {"version": 1, "actors": ["alice", "bob"], "status": 200}
            with tls_endpoint(lambda: policy_document(state["version"], state["actors"]),
                    status=lambda: state["status"], required_token=TOKEN) as (url, context, seen):
                values = inputs(installation, root / "independent-anchors")
                values.update({"HOSTED_RUNTIME_SECURITY_VERSION": "2",
                    "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": POLICY_ENGINE_REVISION,
                    "HOSTED_CONTROL_PLANE_URL": url})
                bundle = build_security_bundle(values)
                source = root / "operator-token"
                source.write_text(TOKEN)
                source.chmod(0o600)
                key = Ed25519PrivateKey.generate()
                prepare_security(bundle, keyring(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)), source)
                files = install_policy_service(bundle, root / "operator-agent", source)
                ca = root / "synthetic-ca.pem"
                ca.write_text(ssl.DER_cert_to_PEM_cert(context.get_ca_certs(binary_form=True)[0]))
                engine = Engine(home, bundle)
                # The runner's checkout ancestors may be private to its UID.
                # Install the exact built binary into the disposable host.
                executable = root / "tandem-engine"
                shutil.copyfile(engine.binary, executable)
                executable.chmod(0o755)
                engine.binary = str(executable)
                os.chown(bundle["host_paths"]["state"], 1000, 1000)
                os.chown(engine.env["TANDEM_STATE_DIR"], 1000, 1000)
                engine.process_options = {"user": 1000, "group": 1000, "extra_groups": []}
                def fetch(success=True):
                    result = subprocess.run([sys.executable, "-s", "-m", "tandem_runtime_bundle.policy_sync",
                        "--config", files["config_path"]], cwd=root / "operator-agent",
                        env={"PATH": os.environ["PATH"], "SSL_CERT_FILE": str(ca)},
                        capture_output=True, text=True, timeout=20)
                    self.assertNotIn(TOKEN, result.stdout + result.stderr)
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                ready = lambda: json.loads(engine.request("/global/health")[1])["ready"]
                try:
                    engine.start(wait_ready=False)
                    self.assertFalse(ready())
                    fetch()
                    wait_for(ready)
                    alice = assertion(key, "alice", 1, "alice-1")
                    bob = assertion(key, "bob", 1, "bob-1")
                    status, body = session_request(engine, alice, "POST", "/session", {"title": "Alice private note"})
                    self.assertEqual(status, 200, body)
                    session = json.loads(body)
                    session_id = session.get("id") or session.get("session", {}).get("id")
                    self.assertTrue(session_id, body)
                    self.assertEqual(session_request(engine, bob, "GET", f"/session/{session_id}")[0], 404)
                    self.assertNotIn("Alice private note", engine.request(token=bob)[1])
                    state.update(version=2, actors=["bob"])
                    fetch()
                    wait_for(lambda: engine.request(token=alice)[0] == 403)
                    self.assertEqual(engine.request(token=bob)[0], 403)
                    bob_fresh = assertion(key, "bob", 2, "bob-2")
                    self.assertEqual(engine.request(token=bob_fresh)[0], 200)
                    engine.stop()
                    time.sleep(0.01)
                    engine.start(wait_ready=False)
                    self.assertFalse(ready(), "restart must require a new control-plane fetch")
                    fetch()
                    wait_for(ready)
                    self.assertEqual(engine.request(token=bob_fresh)[0], 200)
                    self.assertEqual(engine.request(token=alice)[0], 403)
                    output = Path(bundle["policy_sync"]["output_file"])
                    retained = output.read_bytes()
                    state["status"] = 503
                    fetch(success=False)
                    self.assertEqual(output.read_bytes(), retained)
                    self.assertEqual(output.stat().st_uid, 1000)
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    # Real wall-clock expiry: no modified clock or fabricated
                    # old source timestamp. A failed fetch cannot extend trust.
                    wait_for(lambda: not ready(), seconds=125)
                    self.assertNotEqual(engine.request(token=bob_fresh)[0], 200)
                    state["status"] = 200
                    fetch()
                    wait_for(ready)
                    self.assertEqual(engine.request(token=assertion(key, "bob", 2, "bob-recovered"))[0], 200)
                    self.assertTrue(seen)
                    self.assertTrue(all(path == f"/api/v1/hosted/agent/deployments/{DEPLOYMENT}/policy-bundle"
                        and auth == f"Bearer {TOKEN}" for path, auth in seen))
                    self.assertNotIn(TOKEN, json.dumps(engine.env))
                finally:
                    engine.stop()


if __name__ == "__main__":
    if os.name != "posix" or os.geteuid() != 0 or not os.environ.get("TANDEM_TEST_ENGINE"):
        raise SystemExit("Disposable Linux root and a source-verified TANDEM_TEST_ENGINE are required")
    unittest.main(verbosity=2)
