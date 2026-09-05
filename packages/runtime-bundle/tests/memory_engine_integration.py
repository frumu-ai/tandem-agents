"""Actual hosted memory privacy; no local identity fallback, model or customer data."""
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
from tandem_runtime_bundle.policy_contract import POLICY_ENGINE_REVISION
from tandem_runtime_bundle.policy_service import install_policy_service
from tandem_runtime_bundle.prepare import prepare_security
from engine_integration import Engine, encode
from fixtures import DEPLOYMENT, ORGANIZATION, inputs, keyring
from policy_engine_integration import TOKEN, policy_document, session_request, wait_for
from policy_tls_fixture import tls_endpoint


def signed(key, actor, version, unit, organization, deployment):
    now = int(time.time() * 1000)
    principal = {"actor_id": actor, "source": "tandem-web"}
    claims = {"version": "v1", "issuer": "tandem-web", "audience": "tandem-runtime",
        "issued_at_ms": now, "expires_at_ms": now + 240_000,
        "assertion_id": f"memory-{actor}-{time.time_ns()}",
        "tenant_context": {"org_id": organization, "workspace_id": deployment,
            "deployment_id": deployment, "actor_id": actor, "source": "explicit"},
        "human_actor": {"actor_id": actor, "provider": "tandem"},
        "authority_chain": {"initiated_by": principal, "executed_as": {"kind": "request", **principal}},
        "roles": ["hosted:role:member"], "capabilities": ["hosted.use"],
        "policy_version": version, "org_units": [unit]}
    header = {"alg": "EdDSA", "typ": "tandem-tenant-context+jws", "kid": "test-key"}
    content = ".".join(encode(json.dumps(row, separators=(",", ":")).encode()) for row in (header, claims))
    return content + "." + encode(key.sign(content.encode()))


class HostedMemoryTests(unittest.TestCase):
    def test_private_department_shared_scope_mutations_and_restart(self):
        # Run the same fixture/artifact for two customers, reusing actor and
        # department names. Only the verified tenant/deployment bindings differ.
        for organization, deployment in [(ORGANIZATION, DEPLOYMENT),
                ("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "dddddddd-dddd-4ddd-8ddd-dddddddddddd")]:
            with self.subTest(organization=organization), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                root.chmod(0o755)
                home, install = root / "runtime-home", root / "install"
                for directory in (home, install):
                    directory.mkdir(mode=0o700)
                    os.chown(directory, 1000, 1000)
                policy = {"version": 1, "bob_unit": "eng", "actors": ["alice", "bob"]}
                def document():
                    value = json.loads(policy_document(policy["version"], policy["actors"]))
                    value.update(organization_id=organization, deployment_id=deployment)
                    for row in value["deployment_grants"]:
                        row.update(deployment_id=deployment, resource_id=deployment)
                    for row in value["org_unit_memberships"]:
                        row["unit_id"] = "eng" if row["user_id"] == "alice" else policy["bob_unit"]
                    return json.dumps(value).encode()
                with tls_endpoint(document, required_token=TOKEN) as (url, context, _):
                    values = inputs(install, root / "independent-anchors")
                    values.update(HOSTED_RUNTIME_SECURITY_VERSION="2", HOSTED_TANDEM_ENGINE_SOURCE_REVISION=POLICY_ENGINE_REVISION,
                        HOSTED_CONTROL_PLANE_URL=url, HOSTED_ORGANIZATION_ID=organization, HOSTED_DEPLOYMENT_ID=deployment)
                    bundle = build_security_bundle(values)
                    source = root / "operator-token"
                    source.write_text(TOKEN)
                    source.chmod(0o600)
                    key = Ed25519PrivateKey.generate()
                    keys = keyring(key.public_key().public_bytes_raw())
                    keys["test-key"].update(organization_id=organization, deployment_id=deployment)
                    prepare_security(bundle, keys, source)
                    files = install_policy_service(bundle, root / "operator-agent", source)
                    ca = root / "synthetic-ca.pem"
                    ca.write_text(ssl.DER_cert_to_PEM_cert(context.get_ca_certs(binary_form=True)[0]))
                    def fetch():
                        result = subprocess.run([sys.executable, "-s", "-m", "tandem_runtime_bundle.policy_sync", "--config", files["config_path"]],
                            cwd=root / "operator-agent", env={"PATH": os.environ["PATH"], "SSL_CERT_FILE": str(ca)},
                            capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    engine = Engine(home, bundle)
                    executable = root / "tandem-engine"
                    shutil.copyfile(engine.binary, executable)
                    executable.chmod(0o755)
                    engine.binary = str(executable)
                    os.chown(engine.env["TANDEM_STATE_DIR"], 1000, 1000)
                    engine.process_options = {"user": 1000, "group": 1000, "extra_groups": []}
                    token = lambda actor: signed(key, actor, policy["version"], "eng" if actor == "alice" else policy["bob_unit"], organization, deployment)
                    ready = lambda: json.loads(engine.request("/global/health")[1])["ready"]
                    partition = {"org_id": organization, "workspace_id": deployment,
                                 "project_id": "company-brain-text", "tier": "session"}
                    resource = {"organization_id": organization, "workspace_id": deployment,
                        "project_id": partition["project_id"], "resource_kind": "memory_space",
                        "resource_id": "company-brain-notes"}
                    # Trusted operator setup of the existing persisted registry,
                    # before the non-root engine starts. HostedUse is not DataRead.
                    # This isolated fixture does not claim encrypted storage or a
                    # production grant-provisioning flow.
                    registry = {}
                    now = int(time.time() * 1000)
                    for unit in ("eng", "ops"):
                        grant_id = "acceptance-notes-" + unit
                        registry[f"{organization}::{deployment}::{deployment}::{grant_id}"] = {
                            "grant_id": grant_id, "tenant_context": {"org_id": organization,
                                "workspace_id": deployment, "deployment_id": deployment, "source": "explicit"},
                            "unit": {"kind": "organization_unit", "id": "hosted-control-plane/" + unit},
                            "resource": resource, "effect": "allow", "permissions": ["read"],
                            "data_classes": ["internal"], "state": "active",
                            "created_at_ms": now, "updated_at_ms": now}
                    data = Path(engine.env["TANDEM_STATE_DIR"]) / "data"
                    for directory in (data, data / "enterprise"):
                        directory.mkdir(mode=0o700, exist_ok=True)
                        os.chown(directory, 1000, 1000)
                    grants = data / "enterprise" / "org_unit_access_grants.json"
                    grants.write_text(json.dumps(registry))
                    grants.chmod(0o600)
                    os.chown(grants, 1000, 1000)
                    def put(actor, content, private=False, metadata=None, **changes):
                        body = {"run_id": "memory-acceptance", "partition": partition, "kind": "note",
                            "content": content, "classification": "internal", "private": private,
                            "metadata": {"knowledge_scope_registry": {"registry_id": "acceptance-notes",
                                "resource_ref": resource, "data_class": "internal",
                                "allowed_write_tiers": ["session"]}, **(metadata or {})}, **changes}
                        return session_request(engine, token(actor), "POST", "/memory/put", body)
                    def recall(actor):
                        status, body = session_request(engine, token(actor), "POST", "/memory/search",
                            {"run_id": "memory-acceptance", "partition": partition, "read_scopes": ["session"],
                             "query": "acceptance lantern", "limit": 20})
                        self.assertEqual(status, 200, body)
                        return json.loads(body)["results"]
                    def visible(actor, include, exclude):
                        body = json.dumps(recall(actor))
                        for marker in include:
                            self.assertIn(marker, body, f"{actor} must recall authorized memory")
                        for marker in exclude:
                            self.assertNotIn(marker, body, f"{actor} must not recall unauthorized memory")
                    try:
                        engine.start(wait_ready=False)
                        fetch()
                        wait_for(ready)
                        ids = {}
                        for actor, name, private, metadata in [
                            # Personal notes are subject-owned across the tenant;
                            # department-private notes deliberately require both axes.
                            ("alice", "alice-private", True, {"tenant_shared": True}),
                            ("bob", "bob-private", True, {"tenant_shared": True}),
                            ("bob", "bob-department-private", True, {"owner_org_unit_id": "eng"}),
                            ("alice", "engineering-shared", False, {"owner_org_unit_id": "eng"}),
                            ("alice", "tenant-shared", False, {"tenant_shared": True})]:
                            status, body = put(actor, f"acceptance lantern {name}", private, metadata)
                            self.assertEqual(status, 200, body)
                            ids[name] = json.loads(body)["id"]
                        visible("alice", ["alice-private", "engineering-shared", "tenant-shared"], ["bob-private", "bob-department-private"])
                        visible("bob", ["bob-private", "bob-department-private", "engineering-shared", "tenant-shared"], ["alice-private"])
                        # Private subject authority cannot be changed by request metadata,
                        # selecting a partition or asking to mutate the known record ID.
                        self.assertIn(session_request(engine, token("bob"), "DELETE", f"/memory/{ids['alice-private']}")[0], (403, 404))
                        self.assertEqual(put("bob", "scope spoof", metadata={"owner_org_unit_id": "ops"})[0], 403)
                        foreign_partition = {**partition, "org_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"}
                        self.assertEqual(put("bob", "tenant spoof", partition=foreign_partition)[0], 403)
                        for tier in ("team", "curated"):
                            self.assertEqual(put("bob", "unsupported backing store", partition={**partition, "tier": tier})[0], 403)
                        foreign = signed(key, "alice", 1, "eng", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", deployment)
                        self.assertEqual(engine.request(token=foreign)[0], 403)
                        old_bob = token("bob")
                        policy.update(version=2, bob_unit="ops")
                        fetch()
                        wait_for(lambda: engine.request(token=old_bob)[0] == 403)
                        visible("alice", ["alice-private", "engineering-shared", "tenant-shared"], ["bob-private"])
                        visible("bob", ["bob-private", "tenant-shared"], ["alice-private", "engineering-shared", "bob-department-private"])
                        status, body = put("bob", "acceptance lantern operations-shared", metadata={"owner_org_unit_id": "ops"})
                        self.assertEqual(status, 200, body)
                        visible("alice", ["engineering-shared"], ["operations-shared", "bob-private"])
                        engine.stop()
                        engine.start(wait_ready=False)
                        self.assertFalse(ready())
                        fetch()
                        wait_for(ready)
                        visible("alice", ["alice-private", "engineering-shared", "tenant-shared"], ["bob-private", "operations-shared"])
                        visible("bob", ["bob-private", "operations-shared", "tenant-shared"], ["alice-private", "engineering-shared", "bob-department-private"])
                    finally:
                        engine.stop()


if __name__ == "__main__":
    if os.name != "posix" or os.geteuid() != 0 or not os.environ.get("TANDEM_TEST_ENGINE"):
        raise SystemExit("Run as Linux root with an exact-source TANDEM_TEST_ENGINE")
    unittest.main(verbosity=2)
