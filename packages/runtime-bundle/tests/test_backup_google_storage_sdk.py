"""Pinned Google SDK API-shape checks without cloud credentials or requests."""

from datetime import datetime, timezone
import hashlib
import inspect
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

from tandem_runtime_bundle import backup_google_storage as gcs
from tandem_runtime_bundle.backup_google_storage import _HashSink, _checked_bucket

try:
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage
except ImportError:
    storage = None


@unittest.skipIf(storage is None, "google-cloud-storage optional extra not installed")
class PinnedStorageSdkTests(unittest.TestCase):
    def test_bucket_metadata_types_and_blob_preconditions(self):
        config = {"bucket": "private-tandem-backups", "minimum_retention_seconds": 2592000}
        client = storage.Client(project="synthetic-project", credentials=AnonymousCredentials())
        bucket = client.bucket(config["bucket"])
        bucket._properties = {
            "name": config["bucket"],
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
            "retentionPolicy": {"retentionPeriod": "2592000",
                                "effectiveTime": "2026-09-01T00:00:00Z",
                                "isLocked": True},
            "versioning": {"enabled": False},
        }
        self.assertIs(type(bucket.retention_period), int)
        self.assertIs(bucket.retention_policy_locked, True)
        self.assertIsInstance(bucket.retention_policy_effective_time, datetime)
        self.assertIsNotNone(bucket.retention_policy_effective_time.tzinfo)
        self.assertIs(bucket.versioning_enabled, False)
        self.assertIs(bucket.iam_configuration.uniform_bucket_level_access_enabled, True)
        self.assertEqual(bucket.iam_configuration.public_access_prevention, "enforced")
        with patch.object(bucket, "reload") as reload_bucket:
            wrapper = type("Client", (), {"bucket": lambda _self, _name: bucket})()
            self.assertIs(_checked_bucket(wrapper, config), bucket)
            reload_bucket.assert_called_once_with(timeout=30)

        blob = bucket.blob("v3/synthetic/object")
        self.assertTrue({"size", "if_generation_match", "checksum", "timeout"}.issubset(
            inspect.signature(blob.upload_from_file).parameters))
        self.assertTrue({"if_generation_match", "raw_download", "checksum", "timeout"}.issubset(
            inspect.signature(blob.download_to_file).parameters))
        blob._properties = {"generation": "42"}
        self.assertIs(type(blob.generation), int)
        self.assertEqual(blob.generation, 42)
        url = blob._get_download_url(client, if_generation_match=42)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["generation"], ["42"])
        self.assertEqual(query["ifGenerationMatch"], ["42"])

    def test_real_blob_upload_reload_and_readback_convert_json_generation_strings(self):
        client = storage.Client(project="synthetic-project", credentials=AnonymousCredentials())
        bucket = client.bucket("private-tandem-backups")
        payload = b"encrypted fixture bytes"
        metadata = {"generation": "42", "size": str(len(payload))}
        request = {"object_key": "v3/synthetic/archive.aead", "size": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.aead"
            path.write_bytes(payload)
            observed = path.stat()
            private_info = SimpleNamespace(**{name: getattr(observed, name) for name in (
                "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")},
                st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
            with (patch.object(gcs, "_private_file", return_value=path),
                  patch.object(gcs.os, "fstat", return_value=private_info),
                  patch.object(storage.Blob, "_do_upload", return_value=metadata) as upload):
                generation = gcs._put(bucket, {**request, "source_path": str(path)})
            upload.assert_called_once()
            self.assertIs(type(generation), int)
            self.assertEqual(generation, 42)
        def download(_blob, sink, **kwargs):
            self.assertEqual(kwargs["if_generation_match"], generation)
            sink.write(payload)
        # Blob.reload is real and consumes the JSON API response; only network
        # request and payload delivery are substituted for offline execution.
        with (patch.object(client._connection, "api_request", return_value=metadata) as reload,
              patch.object(storage.Blob, "download_to_file", new=download)):
            self.assertEqual(gcs._verify(bucket, request, generation=generation), 42)
        self.assertEqual(reload.call_count, 2)

    def test_hash_sink_supports_stream_reset_and_bounded_seek(self):
        sink = _HashSink()
        sink.write(b"bad")
        self.assertEqual(sink.tell(), 3)
        self.assertEqual(sink.seek(0), 0)
        sink.write(b"encrypted bytes")
        self.assertEqual(sink.size, len(b"encrypted bytes"))
        self.assertEqual(sink.digest.hexdigest(), hashlib.sha256(b"encrypted bytes").hexdigest())
        with self.assertRaises(OSError):
            sink.seek(1)


if __name__ == "__main__":
    unittest.main()
