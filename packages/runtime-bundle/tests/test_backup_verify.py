"""Disposable Linux preflight checks for encrypted v3 export artifacts."""

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fixtures import DEPLOYMENT, ORGANIZATION
import test_backup_export
from tandem_runtime_bundle.backup_archive import canonical_json
from tandem_runtime_bundle.backup_commands import BackupKms, validate_operator_command
from tandem_runtime_bundle.backup_recovery_authority import (
    MemoryKmsChallenge, RecoveryAuthority)
from tandem_runtime_bundle.backup_verify import (
    _ENTRIES_SQL, _INDEX_SQL, _METADATA_SQL, verify_recovery_candidate)


class RecoveryKms(test_backup_export.FakeKms):
    def unwrap(self, wrapped, scope):
        if scope != self.scope or wrapped != base64.b64encode(
                b"wrapped-by-test-authority-" + b"x" * 48).decode():
            raise ValueError("wrong recovery KMS scope")
        return self.dek


class FakeRecoveryAuthority:
    def __init__(self, receipt):
        self.receipt = receipt

    def attest(self, scope):
        if any(self.receipt[key] != value for key, value in scope.items()):
            raise ValueError("wrong independent scope")
        return self.receipt


class FakeMemoryKms:
    def verify(self, scope, memory, challenge):
        if memory["kek_id"] != ("projects/test/locations/global/"
                                "keyRings/memory/cryptoKeys/hosted"):
            raise ValueError("wrong memory KMS key")
        if challenge["plaintext_sha256"] != hashlib.sha256(b"m" * 32).hexdigest():
            raise ValueError("wrong memory KMS challenge")


class RecoveryPreflightTests(unittest.TestCase):
    def setUp(self):
        test_backup_export.BackupExportTests.setUp(self)
        self.kms = RecoveryKms()
        self.db.close()
        replay = Path(self.bundle["host_paths"]["replay"]) / "assertions.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            (Path(str(replay) + suffix)).unlink(missing_ok=True)
        self.db = sqlite3.connect(replay)
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute(_METADATA_SQL)
        self.db.execute(_ENTRIES_SQL)
        self.db.execute(_INDEX_SQL)
        self.db.execute("INSERT INTO replay_metadata VALUES (1, 1)")
        self.db.execute("INSERT INTO replay_entries VALUES (?, ?, ?, ?)",
                        ("a" * 64, "b" * 64, "c" * 64, 9999999999999))
        self.db.commit()
        result = test_backup_export.BackupExportTests.export(self)
        self.scope = {key: result[key] for key in (
            "backup_id", "organization_id", "deployment_id")}
        base = self.install.parent / "download"
        base.mkdir()
        self.paths = {}
        for key, payload in self.uploader.objects.items():
            path = base / key.rsplit("/", 1)[-1]
            path.write_bytes(payload)
            self.paths[path.name] = path
        manifest = self.paths["manifest.json"].read_bytes()
        commit = json.loads(manifest)
        self.receipt = {
            **self.scope,
            "manifest_object_key": self.uploader.order[-1],
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "manifest_size": len(manifest),
            "archive_sha256": commit["objects"]["archive"]["sha256"],
            "anchors_sha256": commit["objects"]["anchors"]["sha256"],
            "memory_challenge": {
                "ciphertext_base64": base64.b64encode(b"c" * 64).decode(),
                "plaintext_sha256": hashlib.sha256(b"m" * 32).hexdigest(),
            },
            "authorization_id": "test-operator-authorization",
            "old_host_fenced": True, "operator_authorized": True,
            "private": True, "immutable": True, "verified": True,
            "remote_uri": "https://private-backups.example/" + self.uploader.order[-1],
            "schema_version": 1,
        }

    def verify(self, receipt=None):
        return verify_recovery_candidate(
            self.paths["manifest.json"], self.paths["archive.aead"],
            self.paths["anchors.aead"], self.scope,
            FakeRecoveryAuthority(receipt or self.receipt),
            self.kms, FakeMemoryKms())

    def test_verified_archive_replay_anchor_and_binding_are_read_only(self):
        original = (Path(self.bundle["host_paths"]["security"]) /
                    "storage-roots.json").read_bytes()
        report = self.verify()
        self.assertEqual(report["verdict"], "verified_read_only_preflight")
        self.assertEqual(report["replay_entries"], 1)
        self.assertGreaterEqual(report["anchor_members"], 2)
        self.assertEqual((Path(self.bundle["host_paths"]["security"]) /
                          "storage-roots.json").read_bytes(), original)

    def test_missing_or_changed_independent_receipt_fails(self):
        for key, value in (
                ("manifest_sha256", "0" * 64), ("archive_sha256", "0" * 64),
                ("anchors_sha256", "0" * 64)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify({**self.receipt, key: value})

    def test_ciphertext_tamper_or_missing_anchor_fails(self):
        for name in ("archive.aead", "anchors.aead"):
            path = self.paths[name]
            original = path.read_bytes()
            try:
                changed = bytearray(original)
                changed[-1] ^= 1
                path.write_bytes(changed)
                with self.subTest(name=name), self.assertRaises(Exception):
                    self.verify()
            finally:
                path.write_bytes(original)
        self.paths["anchors.aead"].unlink()
        with self.assertRaises(OSError):
            self.verify()

    def test_wrong_kms_scope_and_wrong_challenge_fail(self):
        original = self.kms.scope
        self.kms.scope = {**self.scope, "deployment_id": ORGANIZATION}
        try:
            with self.assertRaisesRegex(ValueError, "scope"):
                self.verify()
        finally:
            self.kms.scope = original
        bad = json.loads(json.dumps(self.receipt))
        bad["memory_challenge"]["plaintext_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "challenge"):
            self.verify(bad)

    def test_empty_sealed_memory_kms_rejects_before_challenge(self):
        commit = json.loads(self.paths["manifest.json"].read_bytes())
        outer = {key: value for key, value in commit.items()
                 if key not in ("manifest_nonce_base64", "manifest_ciphertext_base64")}
        nonce = base64.b64decode(commit["manifest_nonce_base64"])
        inner = json.loads(AESGCM(self.kms.dek).decrypt(
            nonce, base64.b64decode(commit["manifest_ciphertext_base64"]),
            canonical_json(outer)))
        inner["memory_kms"] = {}
        commit["manifest_ciphertext_base64"] = base64.b64encode(
            AESGCM(self.kms.dek).encrypt(nonce, canonical_json(inner),
                                         canonical_json(outer))).decode()
        manifest = canonical_json(commit)
        self.paths["manifest.json"].write_bytes(manifest)
        self.receipt["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
        self.receipt["manifest_size"] = len(manifest)
        with self.assertRaisesRegex(ValueError, "memory KMS references"):
            self.verify()

    def test_uncheckpointed_replay_wal_rejected(self):
        self.db.close()
        replay = Path(self.bundle["host_paths"]["replay"]) / "assertions.sqlite3"
        self.db = sqlite3.connect(replay)
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("INSERT INTO replay_entries VALUES (?, ?, ?, ?)",
                        ("d" * 64, "e" * 64, "f" * 64, 9999999999999))
        self.db.commit()
        self.assertTrue(Path(str(replay) + "-wal").exists())
        self.uploader.objects.clear()
        self.uploader.order.clear()
        result = test_backup_export.BackupExportTests.export(self)
        self.scope = {key: result[key] for key in self.scope}
        for key, payload in self.uploader.objects.items():
            (self.paths["manifest.json"].parent / key.rsplit("/", 1)[-1]).write_bytes(payload)
        manifest = self.paths["manifest.json"].read_bytes()
        commit = json.loads(manifest)
        self.receipt.update(
            **self.scope,
            manifest_object_key=self.uploader.order[-1],
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            manifest_size=len(manifest),
            archive_sha256=commit["objects"]["archive"]["sha256"],
            anchors_sha256=commit["objects"]["anchors"]["sha256"])
        with self.assertRaisesRegex(ValueError, "uncheckpointed WAL"):
            self.verify()

    def test_invalid_replay_schema_inside_valid_export_fails(self):
        # The exporter checks the SQLite header; the recovery preflight also
        # demands the exact pinned engine schema and version.
        self.db.close()
        replay = Path(self.bundle["host_paths"]["replay"]) / "assertions.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            Path(str(replay) + suffix).unlink(missing_ok=True)
        with closing(sqlite3.connect(replay)) as wrong:
            wrong.execute("CREATE TABLE replay(token TEXT)")
        self.uploader.objects.clear()
        self.uploader.order.clear()
        result = test_backup_export.BackupExportTests.export(self)
        self.scope = {key: result[key] for key in self.scope}
        for key, payload in self.uploader.objects.items():
            (self.paths["manifest.json"].parent / key.rsplit("/", 1)[-1]).write_bytes(payload)
        manifest = self.paths["manifest.json"].read_bytes()
        commit = json.loads(manifest)
        self.receipt.update(
            **self.scope,
            manifest_object_key=self.uploader.order[-1],
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            manifest_size=len(manifest),
            archive_sha256=commit["objects"]["archive"]["sha256"],
            anchors_sha256=commit["objects"]["anchors"]["sha256"])
        with self.assertRaisesRegex(ValueError, "replay schema"):
            self.verify()

    def test_replay_namespace_above_pinned_engine_limit_fails(self):
        # The engine rejects this database at startup even though its SQLite
        # schema and total row count are valid. The preflight must agree.
        self.db.executemany(
            "INSERT INTO replay_entries VALUES (?, ?, ?, ?)",
            ((f"{index:064x}", "b" * 64, "c" * 64, 9999999999999)
             for index in range(10000)),
        )
        self.db.commit()
        self.uploader.objects.clear()
        self.uploader.order.clear()
        result = test_backup_export.BackupExportTests.export(self)
        self.scope = {key: result[key] for key in self.scope}
        for key, payload in self.uploader.objects.items():
            (self.paths["manifest.json"].parent / key.rsplit("/", 1)[-1]).write_bytes(payload)
        manifest = self.paths["manifest.json"].read_bytes()
        commit = json.loads(manifest)
        self.receipt.update(
            **self.scope,
            manifest_object_key=self.uploader.order[-1],
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            manifest_size=len(manifest),
            archive_sha256=commit["objects"]["archive"]["sha256"],
            anchors_sha256=commit["objects"]["anchors"]["sha256"],
        )
        with self.assertRaisesRegex(ValueError, "replay rows"):
            self.verify()


class AuthorityProtocolTests(unittest.TestCase):
    def test_unwrap_requires_exact_key_scope(self):
        with patch("tandem_runtime_bundle.backup_commands.validate_operator_command",
                   return_value=Path("/operator/kms")):
            kms = BackupKms("/operator/kms", test_backup_export.FakeKms.key_id, test_backup_export.FakeKms.key_version)
        scope = {"backup_id": DEPLOYMENT, "organization_id": ORGANIZATION,
                 "deployment_id": DEPLOYMENT}
        with patch("tandem_runtime_bundle.backup_commands.call_command", return_value={
            "schema_version": 1, "key_id": test_backup_export.FakeKms.key_id,
            "key_version": test_backup_export.FakeKms.key_version, **scope,
            "plaintext_dek_base64": base64.b64encode(b"x" * 32).decode(),
        }):
            self.assertEqual(kms.unwrap(base64.b64encode(b"w" * 64).decode(), scope),
                             b"x" * 32)
        with patch("tandem_runtime_bundle.backup_commands.call_command", return_value={
            "schema_version": 1, "key_id": test_backup_export.FakeKms.key_id,
            "key_version": test_backup_export.FakeKms.key_version,
            "plaintext_dek_base64": base64.b64encode(b"x" * 32).decode(),
        }):
            with self.assertRaisesRegex(ValueError, "scope"):
                kms.unwrap(base64.b64encode(b"w" * 64).decode(), scope)

    def test_receipt_requires_fence_and_memory_challenge(self):
        scope = {"backup_id": DEPLOYMENT, "organization_id": ORGANIZATION,
                 "deployment_id": DEPLOYMENT}
        with patch("tandem_runtime_bundle.backup_recovery_authority.validate_operator_command",
                   return_value=Path("/operator/authority")):
            authority = RecoveryAuthority("/operator/authority", "private-backups.example")
        receipt = {
            "schema_version": 1, **scope,
            "manifest_object_key": "v3/" + ORGANIZATION + "/" + DEPLOYMENT + "/" +
                                   DEPLOYMENT + "/manifest.json",
            "manifest_sha256": "a" * 64, "archive_sha256": "b" * 64,
            "anchors_sha256": "c" * 64, "manifest_size": 100,
            "private": True, "immutable": True, "verified": True,
            "old_host_fenced": True, "operator_authorized": True,
            "authorization_id": "test-authority-1",
            "remote_uri": "https://private-backups.example/v3/" + ORGANIZATION +
                          "/" + DEPLOYMENT + "/" + DEPLOYMENT + "/manifest.json",
            "memory_challenge": {
                "ciphertext_base64": base64.b64encode(b"c" * 64).decode(),
                "plaintext_sha256": "d" * 64},
        }
        with patch("tandem_runtime_bundle.backup_recovery_authority.call_command",
                   return_value=receipt):
            self.assertEqual(authority.attest(scope), receipt)
        with patch("tandem_runtime_bundle.backup_recovery_authority.call_command",
                   return_value={**receipt, "old_host_fenced": False}):
            with self.assertRaisesRegex(ValueError, "fenced scope"):
                authority.attest(scope)

    def test_memory_challenge_requires_exact_digest(self):
        with patch("tandem_runtime_bundle.backup_recovery_authority.validate_operator_command",
                   return_value=Path("/operator/memory-kms")):
            verifier = MemoryKmsChallenge("/operator/memory-kms")
        scope = {"backup_id": DEPLOYMENT, "organization_id": ORGANIZATION,
                 "deployment_id": DEPLOYMENT}
        memory = {"provider": "google_cloud_kms", "runtime_principal_id": "runtime:" + DEPLOYMENT,
                  "kek_id": "projects/test/locations/global/keyRings/memory/cryptoKeys/hosted",
                  "kek_version": "1", "rotation_epoch": 0}
        challenge = {"ciphertext_base64": base64.b64encode(b"c" * 64).decode(),
                     "plaintext_sha256": hashlib.sha256(b"x" * 32).hexdigest()}
        response = {"schema_version": 1, **scope, **memory,
                    "plaintext_base64": base64.b64encode(b"x" * 32).decode()}
        with patch("tandem_runtime_bundle.backup_recovery_authority.call_command",
                   return_value=response):
            verifier.verify(scope, memory, challenge)
        with patch("tandem_runtime_bundle.backup_recovery_authority.call_command",
                   return_value={**response, "plaintext_base64":
                                 base64.b64encode(b"y" * 32).decode()}):
            with self.assertRaisesRegex(ValueError, "challenge"):
                verifier.verify(scope, memory, challenge)


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                     "requires a disposable Linux root operator")
class OperatorPathTests(unittest.TestCase):
    def test_root_only_command_and_ancestors(self):
        with tempfile.TemporaryDirectory(prefix="tandem-recovery-command-") as temporary:
            root = Path(temporary)
            command = root / "authority"
            command.write_text("#!/bin/sh\nexit 0\n")
            command.chmod(0o500)
            self.assertEqual(validate_operator_command(command), command)
            command.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "root-only"):
                validate_operator_command(command)
            command.chmod(0o500)
            root.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "ancestors"):
                validate_operator_command(command)
            root.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
