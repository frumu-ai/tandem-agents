"""Fail-closed publication plan for the hosted runtime security lanes."""
import argparse
import re

from .contract import ENGINE_VERSION
from .policy_contract import MEMORY_ENGINE_REVISION

TAG = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?\Z")


def publication_plan(event, ref, tag, version, security_version, push_latest):
    if event != "workflow_dispatch" or ref != "refs/heads/main":
        raise ValueError("hosted image publication requires manual dispatch from main")
    if not TAG.fullmatch(tag) or not VERSION.fullmatch(version):
        raise ValueError("invalid hosted image tag or release version")
    if push_latest not in ("true", "false"):
        raise ValueError("invalid push_latest choice")
    if security_version == "3":
        if version != ENGINE_VERSION or tag != f"v{version}-v3" or push_latest != "false":
            raise ValueError("v3 requires the tested version, exact -v3 tag and no latest alias")
        revision = MEMORY_ENGINE_REVISION
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("v3 requires a full pinned source revision")
    elif security_version == "1":
        if tag.endswith("-v3"):
            raise ValueError("the -v3 image tag suffix is reserved for source-built v3")
        revision = ""
    else:
        raise ValueError("unsupported runtime security version")
    return {"tag": tag, "version": version, "push_latest": push_latest,
            "security_version": security_version, "engine_revision": revision}


def main():
    parser = argparse.ArgumentParser(description="Resolve a trusted hosted image publication")
    for name in ("event", "ref", "tag", "version", "security-version", "push-latest"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    for key, value in publication_plan(args.event, args.ref, args.tag, args.version,
                                       args.security_version, args.push_latest).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
