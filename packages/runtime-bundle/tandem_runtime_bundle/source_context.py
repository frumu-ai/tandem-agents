"""Stage only an exact tracked Tandem revision for a Docker named context.

The Tandem repository's root .dockerignore starts with ** and excludes Rust
sources. A Git archive gives BuildKit tracked bytes without that ignore file.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


REQUIRED = ("Cargo.toml", "Cargo.lock", "engine/Cargo.toml",
            "engine/src/main.rs", "crates/tandem-core/Cargo.toml")


def _git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise ValueError(f"cannot verify Tandem source checkout: {result.stderr.strip()}")
    return result.stdout.strip()


def stage_source_context(source, revision, destination):
    source = Path(source).resolve()
    destination = Path(destination)
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Tandem source revision must be a full commit SHA")
    if _git(source, "rev-parse", "HEAD") != revision:
        raise ValueError("Tandem source checkout is not the pinned revision")
    if _git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Tandem source checkout must be clean")
    if destination.exists():
        raise ValueError("Tandem build context destination must be new")

    with tempfile.TemporaryFile() as archive:
        result = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", revision],
                                stdout=archive, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise ValueError("cannot archive pinned Tandem source")
        archive.seek(0)
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=archive, mode="r:") as content:
            for member in content:
                name = member.name.rstrip("/")
                parts = name.split("/")
                if (not name or name.startswith("/") or "\\" in name
                        or any(part in ("", ".", "..") for part in parts)
                        or not (member.isdir() or member.isfile())):
                    raise ValueError("unsafe file in Tandem source archive")
                if name == ".dockerignore":
                    continue
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with content.extractfile(member) as original, target.open("xb") as output:
                        shutil.copyfileobj(original, output)
                    target.chmod(member.mode & 0o777)
    if any(not (destination / path).is_file() for path in REQUIRED):
        raise ValueError("staged Tandem source is missing Rust build inputs")
    if (destination / ".dockerignore").exists():
        raise ValueError("staged Tandem source retained the restrictive .dockerignore")
    return destination


def main():
    parser = argparse.ArgumentParser(description="Stage an exact source-built engine context")
    parser.add_argument("--source", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    stage_source_context(args.source, args.revision, args.output)


if __name__ == "__main__":
    main()
