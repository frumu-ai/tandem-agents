"""Linux filesystem regressions for the v3 clean-host recovery boundary."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from fixtures import inputs, keyring, synthetic_v3_provenance
from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION
from tandem_runtime_bundle.prepare import prepare_security


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                     "real Linux root ownership and inodes required")
class RecoveryGuardTests(unittest.TestCase):
    def setUp(self):
        provenance = synthetic_v3_provenance()
        provenance.start()
        self.addCleanup(provenance.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = self.root / "operator-token"
        self.token.write_text("synthetic-host-agent-token-" + "a" * 32)
        self.token.chmod(0o600)
        values = inputs(self.root / "install", self.root / "independent-anchors")
        values.update(HOSTED_RUNTIME_SECURITY_VERSION="3",
                      HOSTED_TANDEM_ENGINE_SOURCE_REVISION=MEMORY_ENGINE_REVISION)
        self.bundle = build_security_bundle(values)
        for location in (self.bundle["host_paths"]["state"],
                         self.bundle["ordinary_paths"]["DATA"]):
            path = Path(location)
            path.mkdir(parents=True, mode=0o700)
            path.chmod(0o700)
            os.chown(path, self.bundle["uid"], self.bundle["gid"])
        commands = Path(self.bundle["host_paths"]["memory_kms_commands"])
        commands.mkdir(parents=True, mode=0o750)
        commands.chmod(0o750)
        os.chown(commands, 0, self.bundle["gid"])
        for operation in ("encrypt", "decrypt"):
            command = commands / Path(self.bundle["memory_encryption"][f"{operation}_command"]).name
            command.write_text("#!/bin/sh\nexit 1\n")
            command.chmod(0o550)
            os.chown(command, 0, self.bundle["gid"])
        self.prepare()

    def prepare(self):
        prepare_security(self.bundle, keyring(), self.token)

    def test_copy_and_empty_history_roots_never_rebind(self):
        binding = Path(self.bundle["host_paths"]["security"]) / "storage-roots.json"
        original_binding = binding.read_bytes()
        identity = json.loads(original_binding)
        for name in ("state", "data", "replay", "anchor"):
            self.assertIn(name, identity)
        for name in ("replay", "anchor"):
            self.assertEqual(len(identity[name]["sentinel"]), 64)

        for name, path in (("state", Path(self.bundle["host_paths"]["state"])),
                           ("data", Path(self.bundle["ordinary_paths"]["DATA"]))):
            with self.subTest(copied=name):
                original = path.with_name(path.name + "-original")
                path.rename(original)
                try:
                    shutil.copytree(original, path)
                    os.chown(path, self.bundle["uid"], self.bundle["gid"])
                    with self.assertRaisesRegex(ValueError, "workload roots changed"):
                        self.prepare()
                finally:
                    if path.exists():
                        shutil.rmtree(path)
                    original.rename(path)

        for name in ("replay", "anchor"):
            path = Path(self.bundle["host_paths"][name])
            original = path.with_name(path.name + "-original")
            with self.subTest(missing=name):
                path.rename(original)
                try:
                    with self.assertRaisesRegex(ValueError, f"v3 {name} root is missing"):
                        self.prepare()
                    self.assertFalse(path.exists())
                finally:
                    original.rename(path)
            with self.subTest(empty_replacement=name):
                path.rename(original)
                try:
                    path.mkdir(mode=0o700)
                    os.chown(path, self.bundle["uid"], self.bundle["gid"])
                    with self.assertRaisesRegex(ValueError, f"v3 {name} root sentinel is missing"):
                        self.prepare()
                    self.assertEqual(list(path.iterdir()), [])
                    path.rmdir()
                    shutil.copytree(original, path)
                    os.chown(path, self.bundle["uid"], self.bundle["gid"])
                    os.chown(path / ".runtime-security-v3-root", self.bundle["uid"], self.bundle["gid"])
                    with self.assertRaisesRegex(ValueError, "workload roots changed"):
                        self.prepare()
                finally:
                    if path.exists():
                        shutil.rmtree(path)
                    original.rename(path)
            with self.subTest(emptied_original=name):
                retained = path.with_name(path.name + "-contents")
                retained.mkdir(mode=0o700)
                for child in list(path.iterdir()):
                    child.rename(retained / child.name)
                try:
                    with self.assertRaisesRegex(ValueError, f"v3 {name} root sentinel is missing"):
                        self.prepare()
                    self.assertEqual(list(path.iterdir()), [])
                finally:
                    for child in retained.iterdir():
                        child.rename(path / child.name)
                    retained.rmdir()
            with self.subTest(tampered_sentinel=name):
                sentinel = path / ".runtime-security-v3-root"
                original = sentinel.read_bytes()
                sentinel.write_bytes((b"0" if original[:1] != b"0" else b"1") + original[1:])
                try:
                    with self.assertRaisesRegex(ValueError, "workload roots changed"):
                        self.prepare()
                finally:
                    sentinel.write_bytes(original)
        self.assertEqual(binding.read_bytes(), original_binding, "rejections must preserve the binding")
        self.prepare()


if __name__ == "__main__":
    unittest.main()
