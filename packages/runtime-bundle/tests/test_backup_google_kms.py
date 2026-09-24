"""Google KMS backup DEK wire-contract regressions; no cloud credentials."""

import base64
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tandem_runtime_bundle.backup_google_kms import _crc32c, execute, load_config, main


ORGANIZATION = "2380b3a6-5395-4e2f-b773-4ce3bac7437b"
DEPLOYMENT = "58d86a26-3ec0-4338-baa6-528746caf2ad"
BACKUP = "c7194be1-d8ab-4530-aec2-79a226df0de3"
KEY = "projects/test/locations/global/keyRings/backup/cryptoKeys/dek"
VERSION = KEY + "/cryptoKeyVersions/4"
DEK = b"a" * 32
CIPHERTEXT = b"ciphertext-for-one-backup-" + b"z" * 40


class FakeKms:
    def __init__(self):
        self.request = None
        self.expected_aad = None
        self.encrypt_response = SimpleNamespace(
            name=VERSION, ciphertext=CIPHERTEXT,
            ciphertext_crc32c=_crc32c(CIPHERTEXT),
            verified_plaintext_crc32c=True,
            verified_additional_authenticated_data_crc32c=True)
        self.decrypt_response = SimpleNamespace(
            plaintext=DEK, plaintext_crc32c=_crc32c(DEK))

    def encrypt(self, *, request, timeout):
        self.request = request
        assert timeout == 30
        return self.encrypt_response

    def decrypt(self, *, request, timeout):
        self.request = request
        assert timeout == 30
        if (self.expected_aad is not None
                and request["additional_authenticated_data"] != self.expected_aad):
            raise ValueError("KMS rejected a different backup scope")
        return self.decrypt_response


def config(operation):
    return {"schema_version": 1, "operation": operation, "key_id": KEY,
            "key_version": VERSION, "organization_id": ORGANIZATION,
            "deployment_id": DEPLOYMENT, "credentials_file": "/etc/tandem-backup-kms/creds.json"}


def request(operation):
    return {"schema_version": 1, "operation": operation, "key_id": KEY,
            "key_version": VERSION, "organization_id": ORGANIZATION,
            "deployment_id": DEPLOYMENT, "backup_id": BACKUP,
            ("plaintext_dek_base64" if operation == "wrap" else "wrapped_dek_base64"):
            base64.b64encode(DEK if operation == "wrap" else CIPHERTEXT).decode("ascii")}


class GoogleBackupKmsTests(unittest.TestCase):
    def test_crc32c_standard_vector(self):
        self.assertEqual(_crc32c(b"123456789"), 0xe3069283)

    def test_pinned_google_client_accepts_exact_version_and_crc_fields(self):
        try:
            from google.cloud import kms_v1
        except ImportError:
            if os.environ.get("CI") == "true":
                raise
            self.skipTest("optional google-backup-kms extra is not installed")
        encrypt = kms_v1.EncryptRequest(
            name=VERSION, plaintext=DEK, additional_authenticated_data=b"aad",
            plaintext_crc32c=_crc32c(DEK),
            additional_authenticated_data_crc32c=_crc32c(b"aad"))
        decrypt = kms_v1.DecryptRequest(
            name=KEY, ciphertext=CIPHERTEXT, additional_authenticated_data=b"aad",
            ciphertext_crc32c=_crc32c(CIPHERTEXT),
            additional_authenticated_data_crc32c=_crc32c(b"aad"))
        self.assertEqual(encrypt.name, VERSION)
        self.assertEqual(decrypt.name, KEY)
        self.assertEqual(getattr(encrypt.plaintext_crc32c, "value",
                                 encrypt.plaintext_crc32c), _crc32c(DEK))
        self.assertEqual(getattr(decrypt.ciphertext_crc32c, "value",
                                 decrypt.ciphertext_crc32c), _crc32c(CIPHERTEXT))

    def test_wrap_uses_exact_version_scope_aad_and_verified_crc(self):
        client = FakeKms()
        result = execute(request("wrap"), config("wrap"), client)
        self.assertEqual(result["wrapped_dek_base64"], base64.b64encode(CIPHERTEXT).decode())
        self.assertEqual(result["backup_id"], BACKUP)
        self.assertEqual(client.request["name"], VERSION)
        self.assertEqual(client.request["plaintext"], DEK)
        self.assertEqual(client.request["plaintext_crc32c"], _crc32c(DEK))
        self.assertEqual(client.request["additional_authenticated_data_crc32c"],
                         _crc32c(client.request["additional_authenticated_data"]))
        self.assertIn(BACKUP.encode(), client.request["additional_authenticated_data"])

        for field, value in (("name", KEY + "/cryptoKeyVersions/5"),
                             ("verified_plaintext_crc32c", False),
                             ("verified_additional_authenticated_data_crc32c", False),
                             ("ciphertext_crc32c", 0)):
            bad = FakeKms()
            setattr(bad.encrypt_response, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                execute(request("wrap"), config("wrap"), bad)

    def test_unwrap_requires_same_scope_and_checks_decrypted_dek(self):
        client = FakeKms()
        result = execute(request("unwrap"), config("unwrap"), client)
        self.assertEqual(result["plaintext_dek_base64"], base64.b64encode(DEK).decode())
        self.assertEqual(client.request["name"], KEY)
        self.assertEqual(client.request["ciphertext_crc32c"], _crc32c(CIPHERTEXT))
        self.assertIn(BACKUP.encode(), client.request["additional_authenticated_data"])
        original_aad = client.request["additional_authenticated_data"]
        for changed in (
                {"backup_id": DEPLOYMENT},
                {"organization_id": DEPLOYMENT},
                {"key_version": KEY + "/cryptoKeyVersions/5"},
                {"wrapped_dek_base64": "!!!!"}):
            invalid = {**request("unwrap"), **changed}
            scoped_client = FakeKms()
            scoped_client.expected_aad = original_aad
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                execute(invalid, config("unwrap"), scoped_client)
        bad = FakeKms()
        bad.decrypt_response.plaintext_crc32c = 0
        with self.assertRaises(ValueError):
            execute(request("unwrap"), config("unwrap"), bad)

    def test_old_host_wrap_principal_cannot_call_unwrap(self):
        client = FakeKms()
        with self.assertRaises(ValueError):
            execute(request("unwrap"), config("wrap"), client)
        self.assertIsNone(client.request)
        with self.assertRaises(ValueError):
            execute(request("wrap"), config("unwrap"), client)
        self.assertIsNone(client.request)

    def test_malformed_or_noncanonical_requests_fail_before_kms(self):
        for changed in ({"backup_id": BACKUP.upper()}, {"schema_version": True},
                        {"plaintext_dek_base64": base64.b64encode(b"short").decode()},
                        {"unexpected": "value"}):
            invalid = {**request("wrap"), **changed}
            client = FakeKms()
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                execute(invalid, config("wrap"), client)
            self.assertIsNone(client.request)

    def test_command_failure_never_prints_cloud_or_secret_detail(self):
        stderr = io.StringIO()
        with patch("tandem_runtime_bundle.backup_google_kms.load_config",
                   side_effect=ValueError("secret credential path /root/private-token")):
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "Backup KMS request rejected.\n")

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0,
                         "requires Linux root with a disposable private directory")
    def test_root_config_and_credentials_reject_mutable_or_public_files(self):
        with tempfile.TemporaryDirectory(dir="/root") as directory:
            root = Path(directory)
            credentials = root / "credentials.json"
            credentials.write_text("{}", encoding="utf-8")
            credentials.chmod(0o600)
            settings = root / "config.json"
            settings.write_text(json.dumps(config("wrap")), encoding="utf-8")
            settings.chmod(0o600)
            value = json.loads(settings.read_text(encoding="utf-8"))
            value["credentials_file"] = str(credentials)
            settings.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_config(settings)["operation"], "wrap")
            credentials.chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(settings)
            credentials.chmod(0o600)
            settings.chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(settings)


if __name__ == "__main__":
    unittest.main()
