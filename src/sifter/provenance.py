"""SLSA provenance predicates for Sifter-built containers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from sifter import __version__
from sifter.hasher import HashCache
from sifter.manifest import Manifest
from sifter.models import Build

SLSA_PROVENANCE_TYPE = "https://slsa.dev/provenance/v1"
SIFTER_BUILD_TYPE = "https://github.com/AI-Safety-Institute/sifter"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_revision(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def predicate_for_build(*, build: Build, manifest: Manifest, repo_dir: Path) -> dict[str, object]:
    """Return a SLSA v1 predicate without embedding build-argument values."""
    hasher = HashCache(repo_dir, manifest)
    dependencies: list[dict[str, object]] = []
    for step in build.steps:
        definition = repo_dir / step.path
        dependencies.append(
            {"uri": f"file:{step.path.as_posix()}", "digest": {"sha256": _sha256(definition)}}
        )
    if build.base:
        dependencies.append({"uri": f"sifter:{build.base}"})
    if revision := _source_revision(repo_dir):
        dependencies.append({"uri": "git+local", "digest": {"gitCommit": revision}})

    return {
        "buildDefinition": {
            "buildType": SIFTER_BUILD_TYPE,
            "externalParameters": {
                "build": build.tag,
                "definitionPaths": [step.path.as_posix() for step in build.steps],
                "base": build.base,
            },
            "internalParameters": {"sifterContentHash": hasher.get_hash(build)},
            "resolvedDependencies": dependencies,
        },
        "runDetails": {
            "builder": {"id": f"{SIFTER_BUILD_TYPE}@{__version__}"},
        },
    }


def write_predicate(
    *, image: Path, build: Build, manifest: Manifest, repo_dir: Path, output_dir: Path
) -> Path:
    """Write a deterministic provenance predicate to shared storage for the transfer job."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{image.name}.provenance.json"
    payload = predicate_for_build(build=build, manifest=manifest, repo_dir=repo_dir)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output
