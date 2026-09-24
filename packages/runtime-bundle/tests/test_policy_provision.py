import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fixtures import inputs, keyring, DEPLOYMENT, ORGANIZATION, synthetic_v3_provenance
from policy_tls_fixture import tls_endpoint
from test_policy_sync import document, TOKEN
from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION, POLICY_ENGINE_REVISION
from tandem_runtime_bundle.policy_service import install_policy_service
from tandem_runtime_bundle.policy_sync import sync_once, PolicySyncError
from tandem_runtime_bundle.prepare import prepare_security


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "authorized Linux provisioning required")
class PolicyProvisionTests(unittest.TestCase):
    def setUp(self):
        self.provenance = synthetic_v3_provenance()
        self.provenance.start()
        self.addCleanup(self.provenance.stop)
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

    def provision_memory_commands(self, bundle):
        directory = Path(bundle["host_paths"]["memory_kms_commands"])
        directory.mkdir(parents=True, mode=0o750)
        directory.chmod(0o750)
        os.chown(directory, 0, bundle["gid"])
        for operation in ("encrypt", "decrypt"):
            command = directory / Path(bundle["memory_encryption"][f"{operation}_command"]).name
            command.write_text("#!/bin/sh\nexit 1\n")
            command.chmod(0o550)
            os.chown(command, 0, bundle["gid"])
        return directory

    def precreate_workload_roots(self, bundle):
        for value in (bundle["host_paths"]["state"], bundle["ordinary_paths"]["DATA"]):
            path = Path(value)
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, bundle["uid"], bundle["gid"])

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
        encrypted = build_security_bundle({**self.values, "HOSTED_RUNTIME_SECURITY_VERSION": "3",
            "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": MEMORY_ENGINE_REVISION})
        self.precreate_workload_roots(encrypted)
        self.provision_memory_commands(encrypted)
        with self.assertRaisesRegex(ValueError, "authorized memory migration"):
            prepare_security(encrypted, keyring(), self.token)
        files = install_policy_service(self.bundle, self.management, self.token)
        subprocess.run(["systemd-analyze", "verify", *[str(self.management / f"{files['unit']}.{suffix}")
            for suffix in ("service", "timer")]], check=True)

    def test_new_v3_install_is_idempotent_and_cannot_disable_encryption(self):
        fresh_values = {**self.values, "HOSTED_RUNTIME_SECURITY_VERSION": "3",
                        "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": MEMORY_ENGINE_REVISION,
                        "HOSTED_INSTALL_ROOT": str(self.root / "fresh-install"),
                        "HOSTED_AUDIT_ANCHOR_ROOT": str(self.root / "fresh-anchors")}
        encrypted = build_security_bundle(fresh_values)
        self.precreate_workload_roots(encrypted)
        data = Path(encrypted["ordinary_paths"]["DATA"])
        (data / "control-panel-config.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "command directory must be preprovisioned"):
            prepare_security(encrypted, keyring(), self.token)
        self.assertFalse(Path(encrypted["host_paths"]["security"]).exists())
        self.provision_memory_commands(encrypted)
        prepare_security(encrypted, keyring(), self.token)
        prepare_security(encrypted, keyring(), self.token)
        self.assertEqual((Path(encrypted["host_paths"]["security"]) / ".initialized").read_text(),
                         "runtime-security-v3\n")
        binding = Path(encrypted["host_paths"]["security"]) / "storage-roots.json"
        self.assertEqual(binding.stat().st_mode & 0o777, 0o600)
        for location, variable in (("state", "HOSTED_ENGINE_STATE_ROOT"),
                                   ("data", "HOSTED_DATA_ROOT")):
            swapped = build_security_bundle({**fresh_values,
                variable: str(self.root / f"swapped-{location}")})
            self.precreate_workload_roots(swapped)
            with self.subTest(swapped=location), self.assertRaisesRegex(ValueError, "workload roots changed"):
                prepare_security(swapped, keyring(), self.token)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            prepare_security(build_security_bundle({**fresh_values,
                "HOSTED_RUNTIME_SECURITY_VERSION": "2",
                "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": POLICY_ENGINE_REVISION}), keyring(), self.token)

    def test_v3_new_security_root_cannot_hide_legacy_plaintext(self):
        for location in ("state", "data"):
            with self.subTest(location=location):
                values = {**self.values, "HOSTED_INSTALL_ROOT": str(self.root / f"legacy-{location}"),
                    "HOSTED_AUDIT_ANCHOR_ROOT": str(self.root / f"anchors-{location}")}
                old = build_security_bundle(values)
                prepare_security(old, keyring(), self.token)
                self.precreate_workload_roots(old)
                mount = Path(old["host_paths"]["state"] if location == "state" else
                    old["ordinary_paths"]["DATA"])
                (mount / "plaintext-memory.sqlite3").write_text("legacy memory")
                encrypted = build_security_bundle({**values, "HOSTED_RUNTIME_SECURITY_VERSION": "3",
                    "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": MEMORY_ENGINE_REVISION,
                    "HOSTED_SECURITY_ROOT": str(self.root / f"new-security-{location}")})
                self.provision_memory_commands(encrypted)
                with self.assertRaisesRegex(ValueError, "authorized encrypted memory migration"):
                    prepare_security(encrypted, keyring(), self.token)
                self.assertFalse(Path(encrypted["host_paths"]["security"]).exists())

    def test_v3_rejects_mutable_or_linked_kms_commands(self):
        encrypted = build_security_bundle({**self.values, "HOSTED_RUNTIME_SECURITY_VERSION": "3",
            "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": MEMORY_ENGINE_REVISION,
            "HOSTED_INSTALL_ROOT": str(self.root / "command-install"),
            "HOSTED_AUDIT_ANCHOR_ROOT": str(self.root / "command-anchors")})
        self.precreate_workload_roots(encrypted)
        directory = self.provision_memory_commands(encrypted)
        directory.chmod(0o770)
        with self.assertRaisesRegex(ValueError, "root-owned with runtime-group traverse"):
            prepare_security(encrypted, keyring(), self.token)
        directory.chmod(0o750)
        os.chown(directory, encrypted["uid"], encrypted["gid"])
        with self.assertRaisesRegex(ValueError, "root-owned with runtime-group traverse"):
            prepare_security(encrypted, keyring(), self.token)
        os.chown(directory, 0, encrypted["gid"])
        command = directory / "memory-kms-encrypt"
        command.chmod(0o750)
        with self.assertRaisesRegex(ValueError, "root-owned and group-executable"):
            prepare_security(encrypted, keyring(), self.token)
        command.chmod(0o550)
        linked = directory / "extra-hardlink"
        os.link(command, linked)
        with self.assertRaisesRegex(ValueError, "root-owned and group-executable"):
            prepare_security(encrypted, keyring(), self.token)
        linked.unlink()
        command.unlink()
        command.symlink_to(directory / "memory-kms-decrypt")
        with self.assertRaisesRegex(ValueError, "preprovisioned"):
            prepare_security(encrypted, keyring(), self.token)

        weak_parent = self.root / "uid-owned-command-parent"
        weak_parent.mkdir()
        os.chown(weak_parent, encrypted["uid"], encrypted["gid"])
        relocated = build_security_bundle({**self.values,
            "HOSTED_RUNTIME_SECURITY_VERSION": "3",
            "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": MEMORY_ENGINE_REVISION,
            "HOSTED_INSTALL_ROOT": str(self.root / "relocated-command-install"),
            "HOSTED_AUDIT_ANCHOR_ROOT": str(self.root / "relocated-command-anchors"),
            "HOSTED_MEMORY_KMS_COMMAND_ROOT": str(weak_parent / "commands")})
        self.precreate_workload_roots(relocated)
        self.provision_memory_commands(relocated)
        with self.assertRaisesRegex(ValueError, "ancestors must prevent non-root replacement"):
            prepare_security(relocated, keyring(), self.token)
        os.chown(weak_parent, 0, 0)
        weak_parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "ancestors must prevent non-root replacement"):
            prepare_security(relocated, keyring(), self.token)


if __name__ == "__main__":
    unittest.main()
