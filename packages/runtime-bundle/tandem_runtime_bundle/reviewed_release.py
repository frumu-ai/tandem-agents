"""Validate the earlier immutable image publication selected for registration."""
import argparse
import json
from pathlib import Path
import re

from .engine_image_attestation import load_attestation
from .policy_contract import MEMORY_ENGINE_REVISION, verify_memory_engine_image


def validate_publisher_run(document, run_id):
    repository = document.get("repository") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or not re.fullmatch(r"[1-9][0-9]*", run_id)
            or type(document.get("id")) is not int or str(document["id"]) != run_id
            or document.get("event") != "workflow_dispatch"
            or document.get("head_branch") != "main"
            or document.get("path") != ".github/workflows/publish-images.yml"
            or document.get("status") != "completed"
            or document.get("conclusion") != "success"
            or not isinstance(repository, dict)
            or repository.get("full_name") != "frumu-ai/tandem-agents"
            or not isinstance(document.get("head_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", document["head_sha"])):
        raise ValueError("reviewed publication must be a successful main publisher dispatch")
    return document["head_sha"]


def validate_reviewed_engine(run, run_id, attestation_path, image_ref):
    builder = validate_publisher_run(run, run_id)
    document, digest = load_attestation(attestation_path, image_ref)
    if document["builder_revision"] != builder or document["workflow_run_id"] != run_id:
        raise ValueError("reviewed image attestation differs from the selected publisher run")
    verify_memory_engine_image(image_ref, MEMORY_ENGINE_REVISION,
                               document["engine_binary_sha256"], digest)
    return document, digest


def main():
    parser = argparse.ArgumentParser(description="Verify a reviewed immutable v3 publication")
    parser.add_argument("--run-file", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attestation-file", required=True)
    parser.add_argument("--image-ref", required=True)
    args = parser.parse_args()
    run = json.loads(Path(args.run_file).read_bytes())
    document, digest = validate_reviewed_engine(run, args.run_id,
                                               args.attestation_file, args.image_ref)
    print("HOSTED_ENGINE_BINARY_SHA256=" + document["engine_binary_sha256"])
    print("HOSTED_ENGINE_ATTESTATION_SHA256=" + digest)
    print("HOSTED_ENGINE_ATTESTATION_FILE=" + args.attestation_file)


if __name__ == "__main__":
    main()
