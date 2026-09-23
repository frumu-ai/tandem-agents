"""Export-only v3 backup regression and atomicity tests (no Docker required)."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from fixtures import DEPLOYMENT, ORGANIZATION, keyring
from tandem_runtime_bundle.backup_archive import MAGIC, canonical_json
from tandem_runtime_bundle.backup_commands import BackupKms, OffsiteUploader
from tandem_runtime_bundle import backup_export


class FakeKms:
    key_id = "projects/test/locations/global/keyRings/backups/cryptoKeys/hosted"
    key_version = key_id + "/cryptoKeyVersions/1"

    def wrap(self, dek, scope):
        self.dek = dek
        self.scope = scope
        return base64.b64encode(b"wrapped-by-test-authority-" + b"x" * 48).decode()


class FakeUploader:
    def __init__(self, *, fail_on=None, fail_after_write_on=None):
        self.objects = {}
        self.order = []
        self.fail_on = fail_on
        self.fail_after_write_on = fail_after_write_on

    def put_verified(self, source_path, object_key, sha256, size):
        if self.fail_on and object_key.endswith(self.fail_on):
            raise ValueError("synthetic off-site failure")
        payload = Path(source_path).read_bytes()
        assert len(payload) == size
        assert hashlib.sha256(payload).hexdigest() == sha256
        assert object_key not in self.objects
        self.objects[object_key] = payload
        self.order.append(object_key)
        if self.fail_after_write_on and object_key.endswith(self.fail_after_write_on):
            raise ValueError("synthetic acknowledgement failure")
        return "https://private-backups.example/" + object_key


def decrypt_chunks(payload, dek, details, context):
    assert payload.startswith(MAGIC)
    cursor = len(MAGIC)
    prefix = bytes.fromhex(details["nonce_prefix"])
    chunks = []
    index = 0
    while cursor < len(payload):
        length = int.from_bytes(payload[cursor:cursor + 4], "big")
        cursor += 4
        ciphertext = payload[cursor:cursor + length]
        cursor += length
        number = index.to_bytes(4, "big")
        chunks.append(AESGCM(dek).decrypt(prefix + number, ciphertext,
                                          canonical_json(context) + number))
        index += 1
    assert index == details["chunks"]
    return b"".join(chunks)


class BackupExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.install = base / "install"
        self.install.mkdir()
        self.staging = base / "staging"
        self.staging.mkdir()
        hosts = {name: str(base / name) for name in
                 ("state", "security", "replay", "anchor", "panel_auth", "policy")}
        hosts["memory_kms_commands"] = str(base / "memory-kms-commands")
        ordinary = {name: str(self.install / name.lower()) for name in
                    ("DATA", "REPOS", "RUNS", "SECRETS", "PANEL_STATE", "KB_DOCS",
                     "KB_INDEX", "PROXY_DATA", "PROXY_CONFIG")}
        for value in [*hosts.values(), *ordinary.values()]:
            Path(value).mkdir(parents=True, mode=0o700)
        self.bundle = {
            "schema_version": 3, "profile": "hosted-single-node-v3",
            "organization_id": ORGANIZATION, "deployment_id": DEPLOYMENT,
            "uid": os.stat(hosts["state"]).st_uid,
            "gid": os.stat(hosts["state"]).st_gid,
            "host_paths": hosts, "ordinary_paths": ordinary,
            "images": {"engine": "ghcr.io/example/engine@sha256:" + "a" * 64},
            "engine_provenance": {"source_revision": "b" * 40,
                                  "binary_sha256": "c" * 64,
                                  "attestation_sha256": "d" * 64},
            "memory_encryption": {"provider": "google_cloud_kms",
                                  "runtime_principal_id": "runtime:" + DEPLOYMENT,
                                  "kek_id": "projects/test/locations/global/keyRings/memory/cryptoKeys/hosted",
                                  "kek_version": "1", "rotation_epoch": 0},
            "engine_environment": {"TANDEM_AUDIT_HMAC_KEY_ID": "hosted-audit-v1"},
        }
        security = Path(hosts["security"])
        (security / ".initialized").write_bytes(b"runtime-security-v3\n")
        (security / "audit-hmac-key").write_bytes(b"a" * 64)
        (security / "context-keyring.json").write_text(json.dumps(keyring()))
        identity = {}
        for name, path in (("state", Path(hosts["state"])),
                           ("data", Path(ordinary["DATA"])),
                           ("replay", Path(hosts["replay"])),
                           ("anchor", Path(hosts["anchor"]))):
            info = path.lstat()
            identity[name] = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
            if name in ("replay", "anchor"):
                sentinel = (b"b" if name == "replay" else b"a") * 64
                (path / ".runtime-security-v3-root").write_bytes(sentinel)
                identity[name]["sentinel"] = sentinel.decode()
        (security / "storage-roots.json").write_text(json.dumps(identity))
        self.binding_before = (security / "storage-roots.json").read_bytes()
        self.db = sqlite3.connect(Path(hosts["replay"]) / "assertions.sqlite3")
        self.addCleanup(self.db.close)
        self.db.execute("pragma journal_mode=WAL")
        self.db.execute("create table replay (token text)")
        self.db.execute("insert into replay values ('replay-canary')")
        self.db.commit()
        (Path(hosts["anchor"]) / "anchor-0001.json").write_text("anchor-canary")
        (Path(ordinary["DATA"]) / "private.txt").write_text("private-memory-canary")
        (Path(hosts["policy"]) / "current.json").write_text(json.dumps({
            "schema_version": 1, "organization_id": ORGANIZATION,
            "deployment_id": DEPLOYMENT, "policy_version": 7,
        }))
        release = {"runtime_security_version": 3,
                   "engine_provenance": self.bundle["engine_provenance"],
                   "engine_image": self.bundle["images"]["engine"],
                   "release_tag": "test-v3"}
        for suffix in ("hosted.env", "docker-compose.hosted.yml", "release-manifest.env"):
            (self.install / suffix).write_text("test\n")
        (self.install / "release-manifest.json").write_text(json.dumps(release))
        (self.install / "runtime-security.json").write_text(json.dumps(self.bundle))
        (self.install / "proxy").mkdir()
        (self.install / "proxy" / "Caddyfile").write_text("test\n")
        self.kms = FakeKms()
        self.uploader = FakeUploader()

    def export(self, **kwargs):
        return backup_export.export_backup(
            self.install, self.staging, self.kms, self.uploader,
            quiescence=lambda *_: None, strict_host=False, **kwargs)

    def test_manifest_last_and_all_security_roots_are_encrypted(self):
        result = self.export()
        self.assertEqual(result["deployment_id"], DEPLOYMENT)
        self.assertEqual(len(self.uploader.order), 3)
        self.assertTrue(self.uploader.order[-1].endswith("/manifest.json"))
        self.assertEqual((Path(self.bundle["host_paths"]["security"]) /
                          "storage-roots.json").read_bytes(), self.binding_before)
        for payload in self.uploader.objects.values():
            self.assertNotIn(b"private-memory-canary", payload)
            self.assertNotIn(b"anchor-canary", payload)
            self.assertNotIn(b"replay-canary", payload)
        commit = json.loads(self.uploader.objects[self.uploader.order[-1]])
        outer = {key: value for key, value in commit.items()
                 if key not in ("manifest_nonce_base64", "manifest_ciphertext_base64")}
        inner = json.loads(AESGCM(self.kms.dek).decrypt(
            base64.b64decode(commit["manifest_nonce_base64"]),
            base64.b64decode(commit["manifest_ciphertext_base64"]),
            canonical_json(outer)))
        self.assertEqual(inner["policy_version"], 7)
        self.assertEqual(inner["storage_identity"]["anchor"]["sentinel"], "a" * 64)
        self.assertEqual(inner["memory_kms"]["kek_version"], "1")
        archive = commit["objects"]["archive"]
        ciphertext = self.uploader.objects[archive["object_key"]]
        payload = decrypt_chunks(ciphertext, self.kms.dek, archive,
                                 {**self.kms.scope, "format": 1, "kind": "archive"})
        tampered = bytearray(ciphertext)
        tampered[-1] ^= 1
        with self.assertRaises(InvalidTag):
            decrypt_chunks(tampered, self.kms.dek, archive,
                           {**self.kms.scope, "format": 1, "kind": "archive"})
        forged_outer = json.loads(json.dumps(outer))
        forged_outer["objects"]["archive"]["sha256"] = "0" * 64
        with self.assertRaises(InvalidTag):
            AESGCM(self.kms.dek).decrypt(
                base64.b64decode(commit["manifest_nonce_base64"]),
                base64.b64decode(commit["manifest_ciphertext_base64"]),
                canonical_json(forged_outer))
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
            names = tar.getnames()
            self.assertIn("host-replay/assertions.sqlite3", names)
            self.assertIn("host-replay/assertions.sqlite3-wal", names)
            self.assertIn("host-security/storage-roots.json", names)
            self.assertIn("host-anchor/anchor-0001.json", names)
            self.assertIn("host-policy/current.json", names)
            self.assertIn("ordinary-data/private.txt", names)
            self.assertEqual(tar.extractfile("ordinary-data/private.txt").read(),
                             b"private-memory-canary")
        anchors = commit["objects"]["anchors"]
        anchor_payload = decrypt_chunks(self.uploader.objects[anchors["object_key"]],
                                        self.kms.dek, anchors,
                                        {**self.kms.scope, "format": 1, "kind": "anchors"})
        with tarfile.open(fileobj=io.BytesIO(anchor_payload), mode="r:") as tar:
            self.assertIn("host-anchor/anchor-0001.json", tar.getnames())
            self.assertEqual(tar.extractfile("host-anchor/anchor-0001.json").read(),
                             b"anchor-canary")

    def test_missing_history_and_unsafe_files_never_publish(self):
        sentinel = Path(self.bundle["host_paths"]["anchor"]) / ".runtime-security-v3-root"
        sentinel.unlink()
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(self.uploader.objects, {})
        sentinel.write_bytes(b"a" * 64)
        target = Path(self.bundle["ordinary_paths"]["DATA"]) / "private.txt"
        link = target.with_name("link.txt")
        try:
            link.symlink_to(target)
        except OSError:
            os.link(target, link)
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(self.uploader.objects, {})

    def test_failed_or_mutated_export_has_no_commit_manifest(self):
        self.uploader = FakeUploader(fail_on="anchors.aead")
        with self.assertRaisesRegex(ValueError, "synthetic off-site failure"):
            self.export()
        self.assertEqual(len(self.uploader.order), 1)
        self.assertFalse(any(key.endswith("manifest.json") for key in self.uploader.order))

        self.uploader = FakeUploader()
        original = backup_export.write_encrypted_tar

        def mutate_after_archive(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0].name == "archive.aead":
                (Path(self.bundle["ordinary_paths"]["DATA"]) / "private.txt").write_text("changed")
            return result

        with patch.object(backup_export, "write_encrypted_tar", side_effect=mutate_after_archive):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.export()
        self.assertEqual(self.uploader.objects, {})

        self.uploader = FakeUploader(fail_on="manifest.json")
        with self.assertRaisesRegex(ValueError, "manifest completion unconfirmed"):
            self.export()
        self.assertEqual(len(self.uploader.order), 2)
        self.assertFalse(any(key.endswith("manifest.json") for key in self.uploader.order))

    def test_unacknowledged_manifest_commit_requires_remote_reconciliation(self):
        self.uploader = FakeUploader(fail_after_write_on="manifest.json")
        with self.assertRaisesRegex(ValueError, "manifest completion unconfirmed.*reconcile") as raised:
            self.export()
        self.assertEqual(len(self.uploader.order), 3)
        self.assertTrue(self.uploader.order[-1].endswith("/manifest.json"))
        manifest = self.uploader.objects[self.uploader.order[-1]]
        self.assertIn(f"sha256={hashlib.sha256(manifest).hexdigest()}", str(raised.exception))
        self.assertIn(f"size={len(manifest)}", str(raised.exception))

    def test_invalid_replay_and_kms_failure_never_upload(self):
        replay = Path(self.bundle["host_paths"]["replay"]) / "assertions.sqlite3"
        self.db.close()
        replay.unlink()
        with self.assertRaises(OSError):
            self.export()
        self.assertEqual(self.uploader.objects, {})
        sqlite3.connect(replay).close()
        with self.assertRaisesRegex(ValueError, "replay database"):
            self.export()
        self.assertEqual(self.uploader.objects, {})
        connection = sqlite3.connect(replay)
        connection.execute("create table replay (token text)")
        connection.close()
        with patch.object(self.kms, "wrap", side_effect=ValueError("synthetic KMS failure")):
            with self.assertRaisesRegex(ValueError, "synthetic KMS failure"):
                self.export()
        self.assertEqual(self.uploader.objects, {})

    def test_quiescence_failure_precedes_kms_and_upload(self):
        with self.assertRaisesRegex(ValueError, "synthetic active writer"):
            backup_export.export_backup(
                self.install, self.staging, self.kms, self.uploader,
                quiescence=lambda *_: (_ for _ in ()).throw(ValueError("synthetic active writer")),
                strict_host=False)
        self.assertEqual(self.uploader.objects, {})
        self.assertFalse(hasattr(self.kms, "dek"))


class AuthorityProtocolTests(unittest.TestCase):
    def test_wrong_kms_version_and_nonprivate_receipt_fail_closed(self):
        key_id = FakeKms.key_id
        version = FakeKms.key_version
        with patch("tandem_runtime_bundle.backup_commands.validate_operator_command",
                   return_value=Path("/operator/kms")):
            kms = BackupKms("/operator/kms", key_id, version)
        with patch("tandem_runtime_bundle.backup_commands.call_command", return_value={
            "schema_version": 1, "key_id": key_id, "key_version": key_id + "/wrong",
            "wrapped_dek_base64": base64.b64encode(b"x" * 64).decode(),
        }):
            with self.assertRaisesRegex(ValueError, "attest"):
                kms.wrap(b"x" * 32, {"backup_id": DEPLOYMENT,
                                     "organization_id": ORGANIZATION, "deployment_id": DEPLOYMENT})
        with patch("tandem_runtime_bundle.backup_commands.validate_operator_command",
                   return_value=Path("/operator/upload")):
            uploader = OffsiteUploader("/operator/upload", "private-backups.example")
        with self.assertRaisesRegex(ValueError, "mismatched receipt"):
            uploader._check_receipt({"schema_version": 1, "object_key": "v3/a",
                                     "sha256": "a" * 64, "size": 5, "private": False,
                                     "if_absent": True, "verified": False,
                                     "remote_uri": "https://private-backups.example/v3/a"},
                                    "v3/a", "a" * 64, 5, verified=False)


if __name__ == "__main__":
    unittest.main()
