"""Reviewable source/binary/image evidence for the v3 enterprise engine.

The document is an observation, not release authority. The separately reviewed
image allowlist binds its fingerprint and binary digest before v3 can render.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from .policy_contract import MEMORY_ENGINE_REVISION

FIELDS = {"schema_version", "platform", "engine_source_repository",
          "engine_source_revision", "engine_binary_sha256", "engine_image_ref",
          "builder_repository", "builder_revision", "workflow_run_id"}
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def fingerprint(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def validate_attestation(document, image_ref, revision=MEMORY_ENGINE_REVISION):
    if (not isinstance(document, dict) or set(document) != FIELDS
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["platform"] != "linux/amd64"
            or document["engine_source_repository"] != "frumu-ai/tandem"
            or document["engine_source_revision"] != revision
            or document["engine_image_ref"] != image_ref
            or document["builder_repository"] != "frumu-ai/tandem-agents"
            or not isinstance(document["engine_binary_sha256"], str)
            or not SHA256.fullmatch(document["engine_binary_sha256"])
            or not isinstance(document["builder_revision"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", document["builder_revision"])
            or not isinstance(document["workflow_run_id"], str)
            or not re.fullmatch(r"[1-9][0-9]*", document["workflow_run_id"])
            or not isinstance(image_ref, str) or not IMAGE.fullmatch(image_ref)):
        raise ValueError("v3 engine attestation must bind exact source, binary and image")
    return fingerprint(document)


def load_attestation(path, image_ref, revision=MEMORY_ENGINE_REVISION):
    raw = Path(path).read_bytes()
    if len(raw) > 16 * 1024:
        raise ValueError("v3 engine attestation is too large")
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate v3 engine attestation field")
            result[name] = value
        return result
    document = json.loads(raw, object_pairs_hook=unique)
    digest = validate_attestation(document, image_ref, revision)
    return document, digest


def main():
    parser = argparse.ArgumentParser(description="Record the published v3 engine image observation")
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--builder-revision", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    document = {
        "schema_version": 1, "platform": "linux/amd64",
        "engine_source_repository": "frumu-ai/tandem",
        "engine_source_revision": args.source_revision,
        "engine_binary_sha256": args.binary_sha256,
        "engine_image_ref": args.image_ref,
        "builder_repository": "frumu-ai/tandem-agents",
        "builder_revision": args.builder_revision,
        "workflow_run_id": args.workflow_run_id,
    }
    digest = validate_attestation(document, args.image_ref)
    Path(args.output).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(digest)


if __name__ == "__main__":
    main()
