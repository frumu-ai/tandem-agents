"""Fake-SDK checks for the root-only Google backup object transport."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tandem_runtime_bundle import backup_google_storage as gcs
from tandem_runtime_bundle.backup_commands import OffsiteUploader


ORGANIZATION = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT = "00000000-0000-4000-8000-000000000002"
BACKUP = "00000000-0000-4000-8000-000000000003"
KEY = f"v3/{ORGANIZATION}/{DEPLOYMENT}/{BACKUP}/archive.aead"
PAYLOAD = b"synthetic encrypted backup bytes"


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None
        self.size = None

    def upload_from_file(self, handle, **kwargs):
        self.bucket.upload_options.append(kwargs)
        if kwargs["if_generation_match"] != 0 or self.name in self.bucket.objects:
            raise ValueError("object already exists")
        value = handle.read()
        if len(value) != kwargs["size"]:
            raise ValueError("wrong upload length")
        self.bucket.objects[self.name] = (self.bucket.next_generation, value)
        self.generation = self.bucket.next_generation
        self.size = len(value)
        self.bucket.next_generation += 1

    def reload(self, **kwargs):
        self.bucket.metadata_options.append(kwargs)
        self.generation, value = self.bucket.objects[self.name]
        self.size = len(value)

    def download_to_file(self, sink, **kwargs):
        self.bucket.download_options.append(kwargs)
        generation, value = self.bucket.objects[self.name]
        if kwargs["if_generation_match"] != generation:
            raise ValueError("generation changed")
        if self.bucket.corrupt_download:
            value = b"X" + value[1:]
        for index in range(0, len(value), 5):
            sink.write(value[index:index + 5])
        if self.bucket.switch_generation_after_download:
            self.bucket.objects[self.name] = (generation + 1, value)


class FakeBucket:
    def __init__(self):
        self.name = "private-tandem-backups"
        self.iam_configuration = type("IAM", (), {
            "uniform_bucket_level_access_enabled": True,
            "public_access_prevention": "enforced",
        })()
        self.retention_policy_locked = True
        self.retention_period = 2592000
        self.retention_policy_effective_time = datetime.now(timezone.utc) - timedelta(days=1)
        self.versioning_enabled = False
        self.next_generation = 42
        self.objects = {}
        self.upload_options = []
        self.metadata_options = []
        self.download_options = []
        self.corrupt_download = False
        self.switch_generation_after_download = False
        self.reload_count = 0

    def reload(self, **kwargs):
        self.reload_count += 1
        if kwargs != {"timeout": 30}:
            raise ValueError("bucket metadata timeout is not bounded")

    def blob(self, name):
        return FakeBlob(self, name)


class FakeClient:
    def __init__(self):
        self.bucket_value = FakeBucket()

    def bucket(self, name):
        if name != self.bucket_value.name:
            raise ValueError("wrong bucket")
        return self.bucket_value


CONFIG = {"schema_version": 1, "bucket": "private-tandem-backups",
          "project_id": "example-backup-project",
          "credentials_file": "/etc/tandem-backup-gcs/credentials.json",
          "organization_id": ORGANIZATION, "deployment_id": DEPLOYMENT,
          "minimum_retention_seconds": 2592000}


def request(operation="verify", payload=PAYLOAD):
    result = {"schema_version": 1, "operation": operation, "object_key": KEY,
              "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
              "private": True}
    if operation == "put_if_absent":
        result["source_path"] = "/root/synthetic-encrypted-backup.aead"
    return result


class StorageProtocolTests(unittest.TestCase):
    def test_exact_scope_and_request_fields(self):
        self.assertEqual(gcs._request(request(), CONFIG), "verify")
        for altered in (
                {**request(), "object_key": KEY.replace(ORGANIZATION, "../../other")},
                {**request(), "object_key": KEY.replace(BACKUP, BACKUP[:-1] + "A")},
                {**request(), "object_key": KEY.replace(ORGANIZATION, DEPLOYMENT)},
                {**request(), "object_key": KEY.replace("archive.aead", "plaintext.tar")},
                {**request(), "size": True},
                {**request(), "private": False},
                {**request(), "extra": "ignored"},
                {**request(), "sha256": "A" * 64}):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                gcs._request(altered, CONFIG)

    def test_bucket_policy_must_be_explicit_private_locked_and_unversioned(self):
        client = FakeClient()
        bucket = client.bucket_value
        self.assertIs(gcs._checked_bucket(client, CONFIG), bucket)
        for target, attribute, value in (
                (bucket.iam_configuration, "public_access_prevention", "inherited"),
                (bucket.iam_configuration, "uniform_bucket_level_access_enabled", False),
                (bucket, "retention_policy_locked", False),
                (bucket, "retention_period", 100),
                (bucket, "retention_policy_effective_time", datetime.now(timezone.utc) + timedelta(days=1)),
                (bucket, "versioning_enabled", True)):
            original = getattr(target, attribute)
            setattr(target, attribute, value)
            try:
                with self.subTest(attribute=attribute), self.assertRaisesRegex(
                        ValueError, "private retention policy"):
                    gcs._checked_bucket(client, CONFIG)
            finally:
                setattr(target, attribute, original)

    def test_verify_streams_pinned_generation_and_rejects_corruption(self):
        client = FakeClient()
        bucket = client.bucket_value
        bucket.objects[KEY] = (42, PAYLOAD)
        response = gcs.execute(request(), CONFIG, client)
        self.assertEqual(response["remote_uri"],
                         f"https://private-tandem-backups.storage.googleapis.com/{KEY}")
        self.assertEqual(response["remote_generation"], 42)
        self.assertTrue(response["verified"])
        self.assertEqual(bucket.download_options[-1]["if_generation_match"], 42)
        self.assertTrue(bucket.download_options[-1]["raw_download"])
        self.assertEqual(bucket.reload_count, 2)
        with patch("tandem_runtime_bundle.backup_commands.validate_operator_command",
                   return_value=Path("/operator/upload")):
            protocol = OffsiteUploader("/operator/upload",
                                       "private-tandem-backups.storage.googleapis.com")
        self.assertEqual(protocol._check_receipt(response, KEY, request()["sha256"],
                                                 len(PAYLOAD), verified=True),
                         response["remote_uri"])
        bucket.corrupt_download = True
        with self.assertRaisesRegex(ValueError, "readback differs"):
            gcs.execute(request(), CONFIG, client)

    def test_readback_rejects_live_generation_change(self):
        client = FakeClient()
        bucket = client.bucket_value
        bucket.objects[KEY] = (42, PAYLOAD)
        bucket.switch_generation_after_download = True
        with self.assertRaisesRegex(ValueError, "live object changed"):
            gcs.execute(request(), CONFIG, client)

    def test_put_receipt_requires_created_generation(self):
        client = FakeClient()
        with patch.object(gcs, "_put", return_value=42):
            response = gcs.execute(request("put_if_absent"), CONFIG, client)
        self.assertTrue(response["created"])
        self.assertFalse(response["verified"])
        self.assertTrue(response["if_absent"])
        self.assertEqual(response["remote_generation"], 42)

    def test_policy_change_after_put_prevents_receipt(self):
        client = FakeClient()
        bucket = client.bucket_value
        original = bucket.reload

        def change_policy(**kwargs):
            original(**kwargs)
            if bucket.reload_count == 2:
                bucket.versioning_enabled = True

        bucket.reload = change_policy
        with patch.object(gcs, "_put", return_value=42), self.assertRaisesRegex(
                ValueError, "private retention policy"):
            gcs.execute(request("put_if_absent"), CONFIG, client)

    def test_main_error_does_not_print_provider_or_secret_details(self):
        fake_stdin = type("Input", (), {"buffer": io.BytesIO(b'{}')})()
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(gcs, "load_config", return_value=CONFIG),
              patch.object(gcs, "_client", side_effect=RuntimeError("secret-token-123")),
              patch.object(sys, "stdin", fake_stdin), patch.object(sys, "stdout", output),
              patch.object(sys, "stderr", error), self.assertRaises(SystemExit)):
            gcs.main()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "Backup storage request rejected.\n")


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0,
                     "requires Linux root")
class LinuxRootStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/root")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.credentials = self.root / "credentials.json"
        self.credentials.write_bytes(b"{}")
        self.credentials.chmod(0o600)
        self.config_file = self.root / "config.json"
        self.config_file.write_text(json.dumps({**CONFIG,
                                               "credentials_file": str(self.credentials)}))
        self.config_file.chmod(0o600)
        self.source = self.root / "archive.aead"
        self.source.write_bytes(PAYLOAD)
        self.source.chmod(0o600)
        self.client = FakeClient()

    def test_private_config_conditional_put_and_readback(self):
        config = gcs.load_config(self.config_file)
        item = {**request("put_if_absent"), "source_path": str(self.source)}
        put = gcs.execute(item, config, self.client)
        self.assertEqual(put["remote_generation"], 42)
        self.assertEqual(self.client.bucket_value.upload_options[-1]["if_generation_match"], 0)
        self.assertEqual(self.client.bucket_value.upload_options[-1]["checksum"], "crc32c")
        verified = gcs.execute(request(), config, self.client)
        self.assertTrue(verified["verified"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            gcs.execute(item, config, self.client)

    def test_root_only_config_and_source_reject_permissive_or_linked_files(self):
        self.source.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            gcs.execute({**request("put_if_absent"), "source_path": str(self.source)},
                        gcs.load_config(self.config_file), self.client)
        self.assertEqual(self.client.bucket_value.objects, {})
        self.source.chmod(0o600)
        self.config_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            gcs.load_config(self.config_file)
        self.config_file.chmod(0o600)
        link = self.root / "credential-link.json"
        link.symlink_to(self.credentials)
        self.config_file.write_text(json.dumps({**CONFIG, "credentials_file": str(link)}))
        with self.assertRaises(ValueError):
            gcs.load_config(self.config_file)


if __name__ == "__main__":
    unittest.main()
