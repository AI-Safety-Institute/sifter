"""Core data models for Sifter CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sifter.text import reject_control_characters, reject_path_component

if TYPE_CHECKING:
    from sifter.dag import BuildDAG


def cache_filename(content_hash: str) -> str:
    """Get the cache filename for a content hash.

    Cache files are named purely by their content hash, enabling
    content-addressed storage where identical builds share storage.

    Args:
        content_hash: Content hash (e.g., "g7354f89abc1" or full SHA256)

    Returns:
        Filename like "g7354f89abc1.sif"
    """
    return f"{content_hash}.sif"


# ============================================================================
# New models for builds: manifest format
# ============================================================================


@dataclass(frozen=True)
class Step:
    """A single step in a multi-stage build.

    Each step represents a definition file to build, optionally with
    build arguments.

    Attributes:
        path: Path to the .def file (relative to repo root).
        args: Build arguments for this step.
    """

    path: Path
    args: dict[str, str]

    @classmethod
    def from_dict(cls, data: str | Mapping[str, object]) -> Step:
        """Create a Step from a string path or dict.

        Args:
            data: Either a string path or dict with 'path' and optional 'args'.

        Returns:
            Step instance.
        """
        if isinstance(data, str):
            # Shorthand: just a path string
            return cls(path=Path(data), args={})

        # Full form: dict with path and optional args
        path = Path(str(data["path"]))
        args_data = data.get("args", {})
        if not isinstance(args_data, dict):
            raise ValueError(f"Step args must be a dict, got {type(args_data)}")
        args = {str(k): str(v) for k, v in args_data.items()}

        return cls(path=path, args=args)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Step):
            return NotImplemented
        return self.path == other.path and self.args == other.args

    def __hash__(self) -> int:
        return hash((self.path, tuple(sorted(self.args.items()))))


@dataclass
class Build:
    """A container build defined in the manifest.

    In the new builds: format, the build key IS the tag (e.g., "vllm-0.14.0:0.0.1").
    The build consists of one or more steps (definition files) that are
    applied in sequence.

    Attributes:
        name: Build name (e.g., "vllm-0.14.0").
        version: Build version (e.g., "0.0.1").
        steps: List of build steps to execute in order.
        base: Optional base image tag to build on.
    """

    name: str
    version: str
    steps: list[Step]
    base: str | None = None

    @property
    def tag(self) -> str:
        """Full tag (name_version)."""
        return f"{self.name}_{self.version}"

    @property
    def output_filename(self) -> str:
        """Output SIF filename (tag.sif)."""
        return f"{self.tag}.sif"

    @classmethod
    def from_dict(cls, key: str, data: Mapping[str, object]) -> Build:
        """Create a Build from a manifest entry.

        Args:
            key: Build key in format "name_version" (e.g., "vllm-0.14.0_0.0.1").
            data: Dict with 'steps' and optional 'base'.

        Returns:
            Build instance.

        Raises:
            ValueError: If key format is invalid or steps are missing.
        """
        # The key becomes the container's filename as well as its display name, so a
        # repo naming a build with terminal escapes writes both.
        reject_control_characters("build key", key)
        reject_path_component("build key", key)
        if "_" not in key:
            raise ValueError(f"Build key must be in format 'name_version', got: {key!r}")

        name, version = key.rsplit("_", 1)
        if not name or not version:
            raise ValueError(f"Build key must be in format 'name_version', got: {key!r}")

        # Parse steps
        steps_data = data.get("steps", [])
        if not isinstance(steps_data, list):
            raise ValueError(f"Build steps must be a list, got {type(steps_data)}")
        if not steps_data:
            raise ValueError("Build must have at least one step")

        # Each step is either a string path or a dict
        steps: list[Step] = []
        for s in steps_data:
            if isinstance(s, (str, Mapping)):
                steps.append(Step.from_dict(s))
            else:
                raise ValueError(f"Step must be a string or dict, got {type(s)}")

        # Optional base
        base = data.get("base")
        if base is not None:
            base = str(base)
            # An unresolvable base is quoted back in the error naming it.
            reject_control_characters("build base", base)

        return cls(name=name, version=version, steps=steps, base=base)


@dataclass(frozen=True)
class BuildSpec:
    """Specification for a single build job.

    This represents everything needed to build one container build,
    used as a node in the build DAG.

    Attributes:
        build: Build definition from the manifest
        definition_path: Relative path to the .def file for this build
        args_dict: Flattened args for the build step
        output_filename: Cache filename (hash.sif)
        base_image: Dependency SIF filename if applicable (None for base containers)
        content_hash: Hash for caching/deduplication
        should_tag: Whether to copy the output to the registry with a tag
        registry_tag: Optional tag for registry (e.g., "myapp_v1.0.0")
    """

    build: Build
    definition_path: Path
    args_dict: dict[str, str]
    output_filename: str
    base_image: str | None
    content_hash: str
    should_tag: bool
    registry_tag: str | None = None

    @property
    def definition_file(self) -> Path:
        """Relative path to .def file."""
        return self.definition_path

    @property
    def full_name(self) -> str:
        """Full name without .sif extension."""
        return self.output_filename.removesuffix(".sif")


def parse_filename(filename: str) -> tuple[str, str, str | None]:
    """Parse a tagged container filename into components.

    This parses registry filenames (tagged images), not cache filenames.
    Use is_cache_filename() to check for cache files first.

    Args:
        filename: SIF filename like "vllm-0.14.0_0.0.5.sif" or "myapp_testing.sif"

    Returns:
        Tuple of (name, tag, legacy_hash or None)
        e.g., ("vllm-0.14.0", "0.0.5", None) or ("myapp", "testing", None)

    Raises:
        ValueError: If filename doesn't match expected pattern
    """
    # Remove .sif extension
    base = filename.removesuffix(".sif")
    if base == filename:
        raise ValueError(f"Filename must end with .sif: {filename!r}")

    # Handle legacy +dev suffix for backwards compatibility
    legacy_hash = None
    if "+dev" in base:
        base, legacy_hash = base.split("+dev", 1)
    elif "+" in base:
        base, legacy_hash = base.split("+", 1)

    # Split name and tag on last underscore
    if "_" not in base:
        raise ValueError(f"Filename must contain '_' separator: {filename!r}")

    name, tag = base.rsplit("_", 1)
    if not name or not tag:
        raise ValueError(f"Invalid filename format: {filename!r}")

    return name, tag, legacy_hash


def is_dev_filename(filename: str) -> bool:
    """Check if a filename is a legacy dev build (deprecated).

    New builds use cache files (hash-only names) instead of +dev suffix.
    This function exists for backwards compatibility with old files.

    Args:
        filename: SIF filename to check

    Returns:
        True if this is a legacy dev build (contains + suffix)
    """
    return "+" in filename


@dataclass(frozen=True)
class ContainerInfo:
    """A container with parsed identity and metadata.

    Returned by :func:`sifter.api.list_containers` — combines raw storage
    info (filename, size, modified) with parsed identity (name, tag).

    Attributes:
        name: Parsed container name (e.g., "vllm-0.14.0").
        tag: Parsed tag/version (e.g., "0.0.5").
        filename: Original SIF filename.
        location: Where the container lives ("local" or "remote").
        is_dev: Whether this is a legacy dev build.
        size_bytes: File size in bytes (None if unknown).
        modified: Last modified time (None if unknown).
    """

    name: str
    tag: str
    filename: str
    location: Literal["local", "remote"]
    is_dev: bool = False
    size_bytes: int | None = None
    modified: datetime | None = None


@dataclass(frozen=True)
class CacheInfo:
    """Cache directory statistics.

    Attributes:
        path: Path to the cache directory.
        file_count: Number of cached .sif files.
        total_bytes: Total size of cached files in bytes.
    """

    path: Path
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class TransferResult:
    """Result of a single push/pull transfer operation.

    Attributes:
        filename: SIF filename transferred.
        job_id: SLURM job ID (None if skipped).
        skipped: Whether the transfer was skipped (e.g., already exists in S3).
    """

    filename: str
    job_id: str | None
    skipped: bool


@dataclass(frozen=True)
class RunResult:
    """Result of running a container.

    Attributes:
        exit_code: Process exit code (set for local execution).
        job_id: SLURM job ID (set for SLURM submission).
    """

    exit_code: int | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class BuildJobResult:
    """Result of a single build job submission.

    Attributes:
        name: Build name/tag.
        job_id: SLURM job ID.
        action: What was done.
    """

    name: str
    job_id: str
    action: Literal["build", "pull", "tag"]


@dataclass
class BuildResult:
    """Result of a build operation.

    Attributes:
        jobs: List of submitted build jobs.
        builds_count: Number of containers that need building.
        pulls_count: Number of containers to pull from S3.
        tags_count: Number of containers to tag from cache.
        dag: The internal build DAG (advanced use — prefer the summary counts).
    """

    jobs: list[BuildJobResult] = field(default_factory=list)
    builds_count: int = 0
    pulls_count: int = 0
    tags_count: int = 0
    dag: BuildDAG | None = None
