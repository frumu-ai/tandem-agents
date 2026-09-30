"""Registration must consume the approved publication without a rebuild."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tandem_runtime_bundle.engine_image_attestation import fingerprint
from tandem_runtime_bundle.policy_contract import MEMORY_ENGINE_REVISION, VERIFIED_MEMORY_ENGINE_IMAGES
from tandem_runtime_bundle.release_lane import publication_plan
from tandem_runtime_bundle.reviewed_release import validate_publisher_run, validate_reviewed_engine
from fixtures import SYNTHETIC_ENGINE_IMAGE


class ReviewedReleaseTests(unittest.TestCase):
    def run_document(self):
        return {"id": 123, "event": "workflow_dispatch", "head_branch": "main",
                "path": ".github/workflows/publish-images.yml", "status": "completed",
                "conclusion": "success", "head_sha": "d" * 40,
                "repository": {"full_name": "frumu-ai/tandem-agents"}}

    def attestation(self):
        return {"schema_version": 1, "platform": "linux/amd64",
                "engine_source_repository": "frumu-ai/tandem",
                "engine_source_revision": MEMORY_ENGINE_REVISION,
                "engine_binary_sha256": "b" * 64,
                "engine_image_ref": SYNTHETIC_ENGINE_IMAGE,
                "builder_repository": "frumu-ai/tandem-agents",
                "builder_revision": "d" * 40, "workflow_run_id": "123"}

    def test_registration_preserves_the_prior_reviewed_builder_digest(self):
        plan = publication_plan("workflow_dispatch", "refs/heads/main", "v0.7.2-v3",
                                "0.7.2", "3", "false", "true", "123")
        self.assertEqual(plan["reuse_reviewed_images"], "true")
        document = self.attestation()
        digest = fingerprint(document)
        approval = {"source_revision": MEMORY_ENGINE_REVISION,
                    "binary_sha256": "b" * 64, "attestation_sha256": digest}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "attestation.json"
            path.write_text(json.dumps(document))
            with patch.dict(VERIFIED_MEMORY_ENGINE_IMAGES, {SYNTHETIC_ENGINE_IMAGE: approval}):
                loaded, observed = validate_reviewed_engine(self.run_document(), "123", path,
                                                             SYNTHETIC_ENGINE_IMAGE)
                self.assertEqual(observed, digest)
                self.assertEqual(loaded["builder_revision"], "d" * 40)
                # Neither a new approval commit nor a different selected
                # publisher may relabel the approved candidate observation.
                for changed in ({"head_sha": "e" * 40}, {"id": 124}):
                    run = {**self.run_document(), **changed}
                    with self.assertRaises(ValueError):
                        validate_reviewed_engine(run, str(run["id"]), path, SYNTHETIC_ENGINE_IMAGE)
            with self.assertRaisesRegex(ValueError, "verified exact-source"):
                validate_reviewed_engine(self.run_document(), "123", path, SYNTHETIC_ENGINE_IMAGE)

    def test_reviewed_run_must_be_successful_same_repo_main_manual_publisher(self):
        valid = self.run_document()
        for change in ({"id": "123"}, {"event": "pull_request"},
                       {"head_branch": "codex/candidate"}, {"conclusion": "failure"},
                       {"status": "in_progress"}, {"path": ".github/workflows/ci.yml"},
                       {"repository": {"full_name": "elsewhere/runtime"}},
                       {"repository": None},
                       {"head_sha": "bad"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_publisher_run({**valid, **change}, "123")
        self.assertEqual(validate_publisher_run(valid, "123"), "d" * 40)

    def test_approval_cannot_authorize_changed_source_binary_image_or_observation(self):
        document = self.attestation()
        approval = {"source_revision": MEMORY_ENGINE_REVISION,
                    "binary_sha256": "b" * 64, "attestation_sha256": fingerprint(document)}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "attestation.json"
            with patch.dict(VERIFIED_MEMORY_ENGINE_IMAGES, {SYNTHETIC_ENGINE_IMAGE: approval}):
                for field, changed in (("engine_source_revision", "0" * 40),
                                       ("engine_binary_sha256", "f" * 64),
                                       ("engine_image_ref", SYNTHETIC_ENGINE_IMAGE[:-1] + "f"),
                                       ("builder_revision", "e" * 40),
                                       ("workflow_run_id", "124")):
                    with self.subTest(field=field):
                        path.write_text(json.dumps({**document, field: changed}))
                        with self.assertRaises(ValueError):
                            validate_reviewed_engine(self.run_document(), "123", path,
                                                     SYNTHETIC_ENGINE_IMAGE)
            self.assertFalse(VERIFIED_MEMORY_ENGINE_IMAGES,
                             "synthetic approvals must never populate the production allowlist")

    def test_candidate_and_registration_dispatches_are_distinct(self):
        args = ("workflow_dispatch", "refs/heads/main", "v0.7.2-v3", "0.7.2", "3", "false")
        self.assertEqual(publication_plan(*args)["reuse_reviewed_images"], "false")
        for register, run in (("true", ""), ("true", "0"), ("true", "123\n"),
                              ("false", "123")):
            with self.subTest(register=register, run=run), self.assertRaises(ValueError):
                publication_plan(*args, register, run)


if __name__ == "__main__":
    unittest.main()
