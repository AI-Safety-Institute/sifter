from __future__ import annotations

from pathlib import Path

from sifter.manifest import Manifest
from sifter.provenance import predicate_for_build, write_predicate


def test_predicate_captures_digests_without_build_arg_values(sample_repo: Path) -> None:
    manifest = Manifest.load(sample_repo / "sifter.yaml")
    build = next(iter(manifest.builds.values()))
    image = sample_repo / build.output_filename
    image.write_bytes(b"sif-bytes")

    predicate = predicate_for_build(build=build, manifest=manifest, repo_dir=sample_repo)
    definition = predicate["buildDefinition"]  # type: ignore[index]
    assert definition["buildType"].endswith("/sifter")  # type: ignore[index]
    assert "resolvedDependencies" in definition  # type: ignore[operator]
    external = definition["externalParameters"]  # type: ignore[index]
    assert "args" not in external  # type: ignore[operator]
    assert "buildArgs" not in external  # type: ignore[operator]


def test_write_predicate_creates_json_sidecar(sample_repo: Path, tmp_path: Path) -> None:
    manifest = Manifest.load(sample_repo / "sifter.yaml")
    build = next(iter(manifest.builds.values()))
    image = sample_repo / build.output_filename
    image.write_bytes(b"sif-bytes")
    output = write_predicate(
        image=image, build=build, manifest=manifest, repo_dir=sample_repo, output_dir=tmp_path
    )
    assert output.name.endswith(".sif.provenance.json")
    assert '"buildType": "https://github.com/AI-Safety-Institute/sifter"' in output.read_text()
