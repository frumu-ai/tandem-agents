"""Source, binary and image provenance must travel together into a v3 release."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.engine_image_attestation import (
    fingerprint, load_attestation, validate_attestation,
)
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION, VERIFIED_MEMORY_ENGINE_IMAGES
from fixtures import inputs, SYNTHETIC_ENGINE_IMAGE


class EngineImageAttestationTests(unittest.TestCase):
    def document(self):
        return {
            "schema_version": 1, "platform": "linux/amd64",
            "engine_source_repository": "frumu-ai/tandem",
            "engine_source_revision": MEMORY_ENGINE_REVISION,
            "engine_binary_sha256": "b" * 64,
            "engine_image_ref": SYNTHETIC_ENGINE_IMAGE,
            "builder_repository": "frumu-ai/tandem-agents",
            "builder_revision": "d" * 40,
            "workflow_run_id": "123",
        }

    def test_exact_document_fingerprint_and_duplicate_field_rejection(self):
        document = self.document()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "attestation.json"
            path.write_text(json.dumps(document))
            loaded, digest = load_attestation(path, SYNTHETIC_ENGINE_IMAGE)
            self.assertEqual(loaded, document)
            self.assertEqual(digest, fingerprint(document))
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_attestation(path, SYNTHETIC_ENGINE_IMAGE)

    def test_source_binary_image_and_builder_mismatches_reject(self):
        valid = self.document()
        for field, value in (
            ("engine_source_revision", "0" * 40),
            ("engine_binary_sha256", "invalid"),
            ("engine_image_ref", SYNTHETIC_ENGINE_IMAGE.replace("a", "e")),
            ("builder_revision", "not-a-sha"),
            ("workflow_run_id", "0"),
            ("platform", "linux/arm64"),
        ):
            with self.subTest(field=field):
                document = {**valid, field: value}
                with self.assertRaisesRegex(ValueError, "exact source, binary and image"):
                    validate_attestation(document, SYNTHETIC_ENGINE_IMAGE)

    def test_reviewed_image_requires_matching_binary_and_attestation_hashes(self):
        document = self.document()
        digest = fingerprint(document)
        record = {"source_revision": MEMORY_ENGINE_REVISION,
                  "binary_sha256": document["engine_binary_sha256"],
                  "attestation_sha256": digest}
        values = {**inputs(), "HOSTED_RUNTIME_SECURITY_VERSION": "3",
                  "HOSTED_TANDEM_ENGINE_SOURCE_REVISION": MEMORY_ENGINE_REVISION,
                  "HOSTED_ENGINE_BINARY_SHA256": document["engine_binary_sha256"],
                  "HOSTED_ENGINE_ATTESTATION_SHA256": digest}
        with patch.dict(VERIFIED_MEMORY_ENGINE_IMAGES, {SYNTHETIC_ENGINE_IMAGE: record}):
            self.assertEqual(build_security_bundle(values)["engine_provenance"], {
                "source_revision": MEMORY_ENGINE_REVISION,
                "binary_sha256": document["engine_binary_sha256"],
                "attestation_sha256": digest,
            })
            for name in ("HOSTED_ENGINE_BINARY_SHA256", "HOSTED_ENGINE_ATTESTATION_SHA256"):
                with self.subTest(missing=name):
                    incomplete = values.copy()
                    del incomplete[name]
                    with self.assertRaisesRegex(ValueError, "verified exact-source"):
                        build_security_bundle(incomplete)
                with self.subTest(changed=name):
                    altered = {**values, name: "f" * 64}
                    with self.assertRaisesRegex(ValueError, "verified exact-source"):
                        build_security_bundle(altered)
        with self.assertRaisesRegex(ValueError, "verified exact-source"):
            build_security_bundle(values)

    def test_workflow_uses_source_context_and_approved_registration(self):
        repo = Path(__file__).resolve().parents[3]
        workflow = (repo / ".github/workflows/publish-images.yml").read_text()
        dockerfile = (repo / "config/Dockerfile.engine-v3").read_text()
        self.assertIn("ref: ${{ needs.resolve.outputs.engine_revision }}", workflow)
        self.assertIn("- uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n      - id: resolve", workflow)
        self.assertIn("tandem-source=./.v3-tandem-source", workflow)
        self.assertIn("'mode=max'", workflow)
        self.assertIn("engine_image_attestation", workflow)
        self.assertIn("scripts/hosted/release-payload.sh", workflow)
        self.assertIn("COPY --from=tandem-source", dockerfile)
        self.assertIn("cargo build --release --locked -p tandem-ai --features browser,enterprise-full", dockerfile)
        self.assertNotIn("npm install", dockerfile)


if __name__ == "__main__":
    unittest.main()
