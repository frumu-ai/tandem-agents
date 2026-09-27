"""Source, binary and image provenance must travel together into a v3 release."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tandem_runtime_bundle import build_security_bundle
from tandem_runtime_bundle.engine_image_attestation import (
    fingerprint, load_attestation, validate_attestation,
)
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION, VERIFIED_MEMORY_ENGINE_IMAGES
from tandem_runtime_bundle.release_lane import publication_plan
from tandem_runtime_bundle.source_context import REQUIRED, stage_source_context
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
        self.assertIn("--output .v3-engine-context", workflow)
        self.assertIn("tandem-source=./.v3-engine-context", workflow)
        self.assertIn("'mode=max'", workflow)
        self.assertIn("engine_image_attestation", workflow)
        self.assertIn("scripts/hosted/release-payload.sh", workflow)
        self.assertIn("environment: hosted-release", workflow)
        self.assertIn("environment: hosted-image-publish", workflow)
        self.assertIn("secrets.HOSTED_IMAGE_PUBLISH_TOKEN", workflow)
        self.assertIn("vars.HOSTED_IMAGE_PUBLISH_ARMED", workflow)
        self.assertNotIn("secrets.GITHUB_TOKEN", workflow)
        self.assertNotIn("packages: write", workflow)
        publish_section = workflow.split("\n  publish:\n", 1)[1].split("\n  register-hosted-release:\n", 1)[0]
        self.assertIn("Require configured protected publisher", publish_section)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("TANDEM_WEB_BASE_URL: https://tandem.ac", workflow)
        self.assertIn("IMAGE_REGISTRY: ghcr.io/frumu-ai/tandem-agents", workflow)
        self.assertNotIn("inputs.tandem_web_base_url", workflow)
        self.assertNotIn("github.event.inputs.registry", workflow)
        self.assertNotIn("\n  push:\n", workflow)
        self.assertIn("COPY --from=tandem-source", dockerfile)
        self.assertIn("cargo build --release --locked -p tandem-ai --features browser,enterprise-full", dockerfile)
        self.assertNotIn("npm install", dockerfile)

    def test_source_archive_overrides_restrictive_dockerignore(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            source.mkdir()
            for name in REQUIRED:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("tracked Rust build input\n")
            (source / ".dockerignore").write_text("**\n")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-qm", "test"], check=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                               text=True).strip()
            context = Path(root) / "context"
            stage_source_context(source, revision, context)
            self.assertTrue(all((context / name).is_file() for name in REQUIRED))
            self.assertFalse((context / ".dockerignore").exists())
            with self.assertRaisesRegex(ValueError, "destination must be new"):
                stage_source_context(source, revision, context)
            (source / "untracked-secret").write_text("private\n")
            with self.assertRaisesRegex(ValueError, "must be clean"):
                stage_source_context(source, revision, Path(root) / "other")

    def test_publication_requires_main_and_reserves_v3_suffix(self):
        good = publication_plan("workflow_dispatch", "refs/heads/main", "v0.7.2-v3",
                                "0.7.2", "3", "false")
        self.assertEqual(good["engine_revision"], MEMORY_ENGINE_REVISION)
        for case in (
            ("workflow_dispatch", "refs/heads/feature", "v0.7.2-v3", "0.7.2", "3", "false"),
            ("push", "refs/tags/v0.7.2-v3", "v0.7.2-v3", "0.7.2", "3", "false"),
            ("workflow_dispatch", "refs/heads/main", "v0.7.2-v3", "0.7.2", "1", "false"),
            ("workflow_dispatch", "refs/heads/main", "v0.7.2-v3", "0.7.2", "3", "true"),
            ("workflow_dispatch", "refs/heads/main", "v0.7.1-v3", "0.7.2", "3", "false"),
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                publication_plan(*case)
        self.assertEqual(publication_plan("workflow_dispatch", "refs/heads/main", "v0.7.2",
                                          "0.7.2", "1", "false")["engine_revision"], "")

    def test_v3_aca_has_no_legacy_engine_install_path(self):
        repo = Path(__file__).resolve().parents[3]
        workflow = (repo / ".github/workflows/publish-images.yml").read_text()
        local_build = (repo / "scripts/hosted/build-images.sh").read_text()
        aca = (repo / "config/Dockerfile").read_text()
        compose = (repo / "scripts/hosted/compose.py").read_text()
        self.assertIn("TANDEM_ENGINE_INSTALL_MODE=${{ matrix.image == 'aca-enterprise'", workflow)
        self.assertIn("TANDEM_ENGINE_INSTALL_MODE=omit", local_build)
        self.assertIn('if [ "${TANDEM_ENGINE_INSTALL_MODE}" = "omit" ]; then', aca)
        self.assertIn("test ! -e /home/node/npm/bin/tandem-engine", aca)
        self.assertIn("Verify v3 ACA cannot start a bundled legacy engine", workflow)
        self.assertIn("TANDEM_ENGINE_STARTUP_MODE: reuse_only", compose)

    def test_publisher_yaml_has_scoped_permissions_and_security_ci_paths(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is installed in Linux security CI")
        repo = Path(__file__).resolve().parents[3]
        publisher = yaml.safe_load((repo / ".github/workflows/publish-images.yml").read_text())
        triggers = publisher.get("on", publisher.get(True))
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        self.assertEqual(publisher["permissions"], {"contents": "read"})
        jobs = publisher["jobs"]
        self.assertIn("refs/heads/main", jobs["resolve"]["if"])
        self.assertEqual(jobs["publish"]["permissions"], {"contents": "read"})
        self.assertEqual(jobs["publish"]["environment"], "hosted-image-publish")
        self.assertIn("Require configured protected publisher",
                      [step.get("name") for step in jobs["publish"]["steps"]])
        self.assertEqual(jobs["register-hosted-release"]["environment"], "hosted-release")
        self.assertEqual(jobs["register-hosted-release"]["permissions"], {"contents": "read"})
        security = yaml.safe_load((repo / ".github/workflows/runtime-security.yml").read_text())
        paths = security.get("on", security.get(True))["pull_request"]["paths"]
        for required in (".github/workflows/publish-images.yml", "config/Dockerfile.engine-v3",
                         "config/Dockerfile", ".dockerignore"):
            self.assertIn(required, paths)


if __name__ == "__main__":
    unittest.main()
