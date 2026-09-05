import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tandem_runtime_bundle.keyring_lifecycle import (
    fingerprint, install_keyring, parse_document, validate_document, validate_transition,
)
from tandem_runtime_bundle.prepare import prepare_security
from fixtures import DEPLOYMENT, ORGANIZATION, keyring, provisioned_paths


def overlap():
    document = keyring()
    document["next-key"] = copy.deepcopy(document["test-key"])
    return document


class DocumentTests(unittest.TestCase):
    def test_scoped_metadata_rejects_ambiguous_or_unknown_fields(self):
        validate_document(overlap(), DEPLOYMENT, ORGANIZATION)
        for field, value in [("private_key", "forbidden"), ("not_before_ms", True),
                             ("not_after_ms", -1), ("not_after_ms", 2**64),
                             ("allowed_resource_scope_prefixes", "*"), ("kms_key_reference", {})]:
            document = keyring()
            document["test-key"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_document(document, DEPLOYMENT, ORGANIZATION)
        with self.assertRaises(ValueError):
            parse_document('{"same": {}, "same": {}}')
        with self.assertRaises(ValueError):
            parse_document('{"key": {"status": "retired", "status": "active"}}')

    def test_existing_material_scope_and_retirement_cannot_be_rolled_back(self):
        previous = overlap()
        previous["test-key"].update(status="retired", not_after_ms=2000)
        valid = copy.deepcopy(previous)
        valid["test-key"]["status"] = "revoked"
        validate_transition(previous, valid)
        for mutate in [lambda d: d.pop("test-key"),
                       lambda d: d["test-key"].update(status="active"),
                       lambda d: d["test-key"].update(not_after_ms=2001),
                       lambda d: d["test-key"].pop("not_after_ms"),
                       lambda d: d["next-key"].update(public_key="another-key"),
                       lambda d: d["next-key"].update(allowed_resource_scope_prefixes=["wider"])]:
            document = copy.deepcopy(previous)
            mutate(document)
            with self.assertRaises(ValueError):
                validate_transition(previous, document)


@unittest.skipUnless(os.name == "posix", "real Linux file ownership, flock and fsync")
class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        _, self.bundle, self.source = provisioned_paths(self.temp.name)
        self.original = keyring()
        prepare_security(self.bundle, self.original, self.source)
        self.path = Path(self.bundle["host_paths"]["security"]) / "context-keyring.json"

    def test_preview_compare_and_swap_retry_and_bootstrap_cannot_revive_retired_key(self):
        proposed = overlap()
        expected = fingerprint(self.original)
        preview = install_keyring(self.bundle, proposed, expected_fingerprint=expected, apply=False)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(json.loads(self.path.read_bytes()), self.original)
        first = install_keyring(self.bundle, proposed, expected_fingerprint=expected)
        self.assertEqual(first["status"], "staged")
        self.assertTrue(first["runtime_reload_required"])
        retry = install_keyring(self.bundle, proposed, expected_fingerprint=expected)
        self.assertEqual(retry["status"], "already_staged")
        self.assertEqual(retry["document_sha256"], first["document_sha256"])
        retired = copy.deepcopy(proposed)
        retired["test-key"]["status"] = "retired"
        with self.assertRaises(ValueError):
            install_keyring(self.bundle, retired, expected_fingerprint=expected)
        install_keyring(self.bundle, retired, expected_fingerprint=first["document_sha256"])
        with self.assertRaises(ValueError):
            prepare_security(self.bundle, self.original, self.source)
        with self.assertRaises(ValueError):
            prepare_security(self.bundle, proposed, self.source)
        self.assertEqual(json.loads(self.path.read_bytes()), retired)

    def test_competing_operator_updates_have_one_winner(self):
        a, b = overlap(), overlap()
        b["other-key"] = b.pop("next-key")
        def stage(document):
            try:
                install_keyring(self.bundle, document, expected_fingerprint=fingerprint(self.original))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(stage, (a, b))), [False, True])
        self.assertIn(json.loads(self.path.read_bytes()), (a, b))

    def test_malformed_candidate_or_failed_replace_preserves_previous_document(self):
        malformed = overlap()
        malformed["next-key"]["organization_id"] = "foreign"
        with self.assertRaises(ValueError):
            install_keyring(self.bundle, malformed)
        with patch("tandem_runtime_bundle.prepare.os.replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                install_keyring(self.bundle, overlap())
        self.assertEqual(json.loads(self.path.read_bytes()), self.original)
        self.assertFalse(list(self.path.parent.glob(".security-*")))

    def test_visible_replace_with_failed_directory_sync_can_be_retried(self):
        original_sync = os.fsync
        def failed_directory(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic directory sync failure")
            original_sync(fd)
        proposed = overlap()
        with patch("tandem_runtime_bundle.prepare.os.fsync", side_effect=failed_directory):
            with self.assertRaises(OSError):
                install_keyring(self.bundle, proposed)
        self.assertEqual(json.loads(self.path.read_bytes()), proposed)
        result = install_keyring(self.bundle, proposed, expected_fingerprint=fingerprint(self.original))
        self.assertEqual(result["status"], "already_staged")

    def test_missing_initialized_keyring_symlink_and_expired_only_candidate_reject(self):
        expired = keyring()
        expired["test-key"]["not_after_ms"] = int(time.time() * 1000) - 1
        with self.assertRaises(ValueError):
            install_keyring(self.bundle, expired)
        self.path.unlink()
        with self.assertRaises(ValueError):
            prepare_security(self.bundle, self.original, self.source)
        self.path.symlink_to(self.source)
        with self.assertRaises(ValueError):
            install_keyring(self.bundle, overlap())


if __name__ == "__main__":
    unittest.main()
