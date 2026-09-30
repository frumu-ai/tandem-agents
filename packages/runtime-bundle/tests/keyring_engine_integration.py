"""Scoped key rotation through the actual non-root engine and protected reload API."""
import copy
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.keyring_lifecycle import fingerprint, install_keyring
from tandem_runtime_bundle.policy_contract import POLICY_ENGINE_REVISION
from tandem_runtime_bundle.policy_service import install_policy_service
from tandem_runtime_bundle.prepare import prepare_security, _write
from engine_integration import Engine, encode
from fixtures import DEPLOYMENT, ORGANIZATION, inputs, keyring
from policy_engine_integration import TOKEN, policy_document, wait_for, session_request
from policy_tls_fixture import tls_endpoint


def signed(key, kid, actor="alice", assertion_id=None):
    now = int(time.time() * 1000)
    principal = {"actor_id": actor, "source": "tandem-web"}
    claims = {"version": "v1", "issuer": "tandem-web", "audience": "tandem-runtime",
        "issued_at_ms": now, "expires_at_ms": now + 240_000,
        "assertion_id": assertion_id or f"{kid}-{actor}-{time.time_ns()}",
        "tenant_context": {"org_id": ORGANIZATION, "workspace_id": DEPLOYMENT,
            "deployment_id": DEPLOYMENT, "actor_id": actor, "source": "explicit"},
        "human_actor": {"actor_id": actor, "provider": "tandem"},
        "authority_chain": {"initiated_by": principal, "executed_as": {"kind": "request", **principal}},
        "roles": ["hosted:role:admin" if actor == "alice" else "hosted:role:member"],
        "capabilities": ["hosted.use", "hosted.admin"] if actor == "alice" else ["hosted.use"],
        "policy_version": 1, "org_units": ["eng" if actor == "alice" else "ops"]}
    header = {"alg": "EdDSA", "typ": "tandem-tenant-context+jws", "kid": kid}
    content = ".".join(encode(json.dumps(value, separators=(",", ":")).encode()) for value in (header, claims))
    return content + "." + encode(key.sign(content.encode()))


def admin_policy():
    document = json.loads(policy_document(1, ["alice", "bob"]))
    document["users"][0].update(role="admin", capabilities=["hosted.use", "hosted.admin"])
    return json.dumps(document).encode()


class KeyringEngineTests(unittest.TestCase):
    def test_overlap_retire_reload_failures_and_restart_keep_identity_and_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            home, installation = root / "runtime-home", root / "install"
            for directory in (home, installation):
                directory.mkdir(mode=0o700)
                os.chown(directory, 1000, 1000)
            with tls_endpoint(admin_policy, required_token=TOKEN) as (url, context, _):
                values = inputs(installation, root / "independent-anchors")
                values.update(HOSTED_RUNTIME_SECURITY_VERSION="2", HOSTED_TANDEM_ENGINE_SOURCE_REVISION=POLICY_ENGINE_REVISION,
                              HOSTED_CONTROL_PLANE_URL=url)
                bundle = build_security_bundle(values)
                source = root / "operator-token"
                source.write_text(TOKEN)
                source.chmod(0o600)
                old_key, new_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
                original = keyring(old_key.public_key().public_bytes_raw())
                proposed = copy.deepcopy(original)
                proposed["next-key"] = keyring(new_key.public_key().public_bytes_raw())["test-key"]
                prepare_security(bundle, original, source)
                files = install_policy_service(bundle, root / "operator-agent", source)
                ca = root / "synthetic-ca.pem"
                ca.write_text(ssl.DER_cert_to_PEM_cert(context.get_ca_certs(binary_form=True)[0]))
                def fetch():
                    subprocess.run([sys.executable, "-s", "-m", "tandem_runtime_bundle.policy_sync", "--config", files["config_path"]],
                        cwd=root / "operator-agent", env={"PATH": os.environ["PATH"], "SSL_CERT_FILE": str(ca)},
                        capture_output=True, text=True, check=True, timeout=20)
                engine = Engine(home, bundle)
                executable = root / "tandem-engine"
                shutil.copyfile(engine.binary, executable)
                executable.chmod(0o755)
                engine.binary = str(executable)
                for path in (bundle["host_paths"]["state"], engine.env["TANDEM_STATE_DIR"]):
                    os.chown(path, 1000, 1000)
                engine.process_options = {"user": 1000, "group": 1000, "extra_groups": []}
                ready = lambda: json.loads(engine.request("/global/health")[1])["ready"]
                key_file = Path(engine.env["TANDEM_CONTEXT_ASSERTION_PUBLIC_KEYS_FILE"])
                reload = lambda token: session_request(engine, token, "POST", "/admin/context-assertions/reload")
                try:
                    engine.start(wait_ready=False)
                    fetch()
                    wait_for(ready)
                    old_token = signed(old_key, "test-key")
                    new_token = signed(new_key, "next-key")
                    self.assertEqual(engine.request(token=old_token)[0], 200)
                    self.assertNotEqual(engine.request(token=new_token)[0], 200)
                    # The CLI previews and stages a root-owned proposed document.
                    bundle_file, proposed_file = root / "bundle.json", root / "proposed.json"
                    for path, document in ((bundle_file, bundle), (proposed_file, proposed)):
                        path.write_text(json.dumps(document)); path.chmod(0o600)
                    command = [sys.executable, "-m", "tandem_runtime_bundle.keyring_lifecycle", "--bundle", str(bundle_file),
                               "--keyring", str(proposed_file), "--expected", fingerprint(original)]
                    for apply in (False, True):
                        result = subprocess.run(command + (["--apply"] if apply else []), capture_output=True, text=True, check=True)
                        self.assertEqual(json.loads(result.stdout)["status"], "staged" if apply else "preview")
                    self.assertNotEqual(engine.request(token=new_token)[0], 200, "staging does not publish verifier authority")
                    self.assertEqual(reload(signed(old_key, "test-key", "bob"))[0], 403)
                    self.assertEqual(session_request(engine, old_token, "POST", "/admin/reload-config")[0], 403)
                    # A real filesystem denial at the independent anchor must
                    # prevent publication; restore access before probing again.
                    anchor = Path(bundle["host_paths"]["anchor"])
                    anchor.chmod(0o500)
                    try:
                        self.assertNotEqual(reload(old_token)[0], 200)
                    finally:
                        anchor.chmod(0o700)
                    self.assertNotEqual(engine.request(token=new_token)[0], 200)
                    status, receipt = reload(old_token)
                    self.assertEqual(status, 200, receipt)
                    self.assertEqual(json.loads(receipt)["verifier"]["key_count"], 2)
                    self.assertEqual(engine.request(token=old_token)[0], 200)
                    self.assertEqual(engine.request(token=new_token)[0], 200)
                    self.assertEqual(engine.request(token=signed(new_key, "next-key", "bob"))[0], 200)
                    # Malformed operator file and wrong scope retain the live snapshot.
                    _write(key_file, b"{invalid", 1000, 1000)
                    self.assertEqual(reload(new_token)[0], 400)
                    self.assertEqual(engine.request(token=new_token)[0], 200)
                    foreign = copy.deepcopy(proposed)
                    foreign["next-key"]["organization_id"] = "foreign"
                    _write(key_file, json.dumps(foreign).encode(), 1000, 1000)
                    self.assertEqual(reload(new_token)[0], 400)
                    _write(key_file, json.dumps(proposed).encode(), 1000, 1000)
                    # Retirement is explicit and cannot be undone by bootstrap or reload.
                    retired = copy.deepcopy(proposed)
                    retired["test-key"]["status"] = "retired"
                    install_keyring(bundle, retired, expected_fingerprint=fingerprint(proposed))
                    self.assertEqual(reload(new_token)[0], 200)
                    self.assertNotEqual(engine.request(token=signed(old_key, "test-key"))[0], 200)
                    self.assertEqual(engine.request(token=signed(new_key, "next-key", "bob"))[0], 200)
                    with self.assertRaises(ValueError):
                        prepare_security(bundle, original, source)
                    _write(key_file, json.dumps(proposed).encode(), 1000, 1000)
                    self.assertEqual(reload(new_token)[0], 400)
                    _write(key_file, json.dumps(retired).encode(), 1000, 1000)
                    self.assertEqual(reload(new_token)[0], 200)
                    # Same assertion ID with different claims is rejected after restart.
                    bound = signed(new_key, "next-key", assertion_id="persistent-binding")
                    self.assertEqual(engine.request(token=bound)[0], 200)
                    engine.stop()
                    engine.start(wait_ready=False)
                    fetch()
                    wait_for(ready)
                    self.assertEqual(engine.request(token=bound)[0], 200)
                    self.assertNotEqual(engine.request(token=signed(new_key, "next-key", "bob", "persistent-binding"))[0], 200)
                    self.assertNotEqual(engine.request(token=signed(old_key, "test-key"))[0], 200)
                    self.assertEqual(engine.request(token=signed(new_key, "next-key", "bob"))[0], 200)
                finally:
                    engine.stop()


if __name__ == "__main__":
    if os.name != "posix" or os.geteuid() != 0 or not os.environ.get("TANDEM_TEST_ENGINE"):
        raise SystemExit("Disposable Linux root and a source-verified TANDEM_TEST_ENGINE are required")
    unittest.main(verbosity=2)
