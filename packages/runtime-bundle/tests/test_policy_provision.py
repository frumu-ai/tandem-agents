import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fixtures import inputs, keyring, DEPLOYMENT, ORGANIZATION
from policy_tls_fixture import tls_endpoint
from test_policy_sync import document, TOKEN
from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import POLICY_ENGINE_REVISION
from tandem_runtime_bundle.policy_service import install_policy_service
from tandem_runtime_bundle.policy_sync import sync_once, PolicySyncError
from tandem_runtime_bundle.prepare import prepare_security


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "authorized Linux provisioning required")
class PolicyProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.management = self.root / "management"
        self.management.mkdir(mode=0o700)
        self.token = self.management / "agent-token"
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.values = {**inputs(self.root / "install", self.root / "anchors"),
            "HOSTED_RUNTIME_SECURITY_VERSION": "2", "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": POLICY_ENGINE_REVISION}
        self.bundle = build_security_bundle(self.values)
        prepare_security(self.bundle, keyring(), self.token)

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, url, context):
        bundle = {**self.bundle, "policy_sync": {**self.bundle["policy_sync"], "control_plane_url": url}}
        files = install_policy_service(bundle, self.management, self.token)
        ca = self.management / "synthetic-ca.pem"
        ca.write_text(ssl.DER_cert_to_PEM_cert(context.get_ca_certs(binary_form=True)[0]))
        return files, ca

    def test_real_tls_fetch_atomically_replaces_snapshot_and_preserves_old_open_reader(self):
        output = Path(self.bundle["policy_sync"]["output_file"])
        for revision in (7, 8):
            body = document(policy_version=revision)
            with tls_endpoint(body) as (url, context, seen):
                files, ca = self.configure(url, context)
                previous = output.open("rb") if output.exists() else None
                try:
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(ca)}):
                        sync_once(files["config_path"])
                    self.assertEqual(output.read_bytes(), body)
                    self.assertEqual(output.stat().st_uid, self.bundle["uid"])
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(output.stat().st_nlink, 1)
                    if previous:
                        self.assertEqual(json.load(previous)["policy_version"], 7)
                    self.assertEqual(seen[0][1], f"Bearer {TOKEN}")
                finally:
                    if previous:
                        previous.close()

    def test_failed_or_wrong_scope_fetch_preserves_last_complete_snapshot(self):
        output = Path(self.bundle["policy_sync"]["output_file"])
        original = document(policy_version=7)
        output.write_bytes(original)
        os.chown(output, self.bundle["uid"], self.bundle["gid"])
        output.chmod(0o600)
        for status, body in ((503, b"unavailable"), (200, document(organization_id=DEPLOYMENT))):
            with tls_endpoint(body, status=status) as (url, context, _):
                files, ca = self.configure(url, context)
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(ca)}), self.assertRaises(PolicySyncError):
                    sync_once(files["config_path"])
                self.assertEqual(output.read_bytes(), original)

    def test_operator_inputs_and_policy_output_reject_insecure_files(self):
        with tls_endpoint(document()) as (url, context, seen):
            files, ca = self.configure(url, context)
            config = Path(files["config_path"])
            for target in (config, self.token):
                target.chmod(0o644)
                with self.assertRaisesRegex(PolicySyncError, "permissions"):
                    sync_once(config)
                target.chmod(0o600)
            output = Path(self.bundle["policy_sync"]["output_file"])
            output.symlink_to(self.token)
            with self.assertRaises(ValueError):
                sync_once(config)
            self.assertEqual(seen, [])
            self.assertEqual(self.token.read_text(), TOKEN)

    def test_policy_enabled_install_cannot_downgrade_and_units_are_valid(self):
        old = build_security_bundle({**self.values, "HOSTED_RUNTIME_SECURITY_VERSION": "1"})
        with self.assertRaisesRegex(ValueError, "downgrade"):
            prepare_security(old, keyring(), self.token)
        files = install_policy_service(self.bundle, self.management, self.token)
        subprocess.run(["systemd-analyze", "verify", *[str(self.management / f"{files['unit']}.{suffix}")
            for suffix in ("service", "timer")]], check=True)


if __name__ == "__main__":
    unittest.main()
