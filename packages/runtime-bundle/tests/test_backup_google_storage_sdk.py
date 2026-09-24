"""Pinned Google SDK API-shape checks without cloud credentials or requests."""

from datetime import datetime, timezone
import hashlib
import inspect
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

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
        url = blob._get_download_url(client, if_generation_match=42)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["generation"], ["42"])
        self.assertEqual(query["ifGenerationMatch"], ["42"])

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
