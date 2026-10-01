"""Public convenience API for programmatic use of sifter.

This module provides high-level functions that mirror every CLI command,
returning structured data instead of Rich-formatted output. Intended for
downstream packages and automation scripts.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from typing import Literal

from sifter.cache import hash_definition
from sifter.config import SifterConfig
from sifter.dag import BuildAction, BuildDAG, DAGNode
from sifter.hasher import HashCache
from sifter.manifest import Manifest
from sifter.models import (
    Build,
    BuildJobResult,
    BuildResult,
    BuildSpec,
    CacheInfo,
    ContainerInfo,
    RunResult,
    Step,
    TransferResult,
    cache_filename,
    parse_filename,
)
from sifter.oci import OCIRegistry
from sifter.provenance import write_predicate
from sifter.registry import RemoteRegistry
from sifter.slurm import SLURMClient, SLURMError, SLURMJob
from sifter.storage import Storage, StorageError

logger = logging.getLogger(__name__)


def _prefix_re(prefix: str) -> str:
    """Regex fragment matching *prefix*, or any container name when empty."""
    return re.escape(prefix) if prefix else ".*"


def _stable_re(prefix: str) -> re.Pattern[str]:
    """Build a regex matching stable containers for the given prefix.

    Matches: {prefix}X.Y.Z_X.Y.Z.sif (semver upstream + semver definition).
    Rejects: RC, nightly, or non-standard version strings.

    An empty *prefix* matches stable containers of any name.
    """
    return re.compile(rf"^{_prefix_re(prefix)}\d+\.\d+\.\d+_\d+\.\d+\.\d+\.sif$")


def _version_re(prefix: str, version: str) -> re.Pattern[str]:
    """Build a regex matching containers built for a specific upstream version.

    An empty *prefix* matches containers of any name.
    """
    return re.compile(rf"^{_prefix_re(prefix)}{re.escape(version)}_\d+\.\d+\.\d+\.sif$")


def _match_pattern(prefix: str, version: str | None) -> str:
    """Human-readable glob describing what a prefix/version pair matches."""
    if version is None:
        return f"{prefix}*.sif"
    return f"{prefix}*{version}_*.sif" if not prefix else f"{prefix}{version}_*.sif"


def get_config(repo_dir: Path | None = None) -> SifterConfig:
    """Get sifter config from environment variables.

    Args:
        repo_dir: Repository whose ``sifter.yaml`` supplies project settings.
            Defaults to the current working directory.

    See :class:`sifter.config.SifterConfig` for available env vars.
    """
    return SifterConfig.from_env(repo_dir=repo_dir)


def registry_path() -> Path:
    """Return the local registry directory path."""
    return get_config().dist_dir


def resolve_container(name: str) -> str:
    """Resolve a bare container filename against the sifter registry.

    If *name* is already an absolute path or exists relative to cwd, return it
    unchanged.  Otherwise, look it up inside the sifter registry directory.

    Returns the resolved path as a string, or *name* unchanged if not found.
    """
    path = Path(name)
    if path.exists():
        return str(path.resolve())
    config = get_config()
    candidate = config.dist_dir / name
    if candidate.exists():
        return str(candidate)
    return name


def find_latest_container(
    prefix: str = "",
    version: str | None = None,
) -> str:
    """Find the latest container matching *prefix* in the sifter registry.

    When *version* is ``None`` (the default), only stable releases
    (``{prefix}X.Y.Z_X.Y.Z.sif``) are matched.  When *version* is
    set (e.g. ``"0.16.1rc0"``), the search is narrowed to containers whose
    upstream version segment matches exactly, and release-candidate /
    nightly versions are allowed.

    Parameters
    ----------
    prefix
        Glob prefix to match container names (e.g. ``"pytorch-"``).  The
        default (empty string) matches containers of any name.
    version
        If set, select the latest container built for this specific upstream
        version (e.g. ``"0.16.1rc0"``).

    Returns
    -------
    str
        Absolute path to the ``.sif`` file.

    Raises
    ------
    RuntimeError
        If no matching container is found.
    """
    config = get_config()
    registry = config.dist_dir

    sifs = _find_local_matches(registry, prefix, version)

    if not sifs:
        # Check remote registry for available containers and suggest a pull command
        remote_hint = _check_remote_hint(config, prefix, version)
        msg = (
            f"No container matching '{_match_pattern(prefix, version)}' found locally ({registry})."
        )
        if remote_hint:
            msg += f"\n\nAvailable in remote registry — pull with:\n  sifter pull {remote_hint}"
        else:
            msg += "\n\nNot found in remote registry either. Build one first: sifter build --all"
        raise RuntimeError(msg)

    container = sifs[-1]
    logger.debug("Using container: %s", container)
    return str(container)


def _find_local_matches(registry: Path, prefix: str, version: str | None) -> list[Path]:
    """Find matching .sif files in the local registry."""
    pattern = _version_re(prefix, version) if version is not None else _stable_re(prefix)
    sifs = sorted(registry.glob(f"{prefix}*.sif"))
    return [s for s in sifs if pattern.match(s.name)]


def _check_remote_hint(config: SifterConfig, prefix: str, version: str | None) -> str | None:
    """Check remote registry for a matching container and return its filename, or None."""
    try:
        remote = config.create_remote_registry()
        if remote is None:
            return None
        remote_files = remote.list_files(prefix=prefix)
        if not remote_files:
            return None

        if version is not None:
            matches = [f for f in remote_files if _version_re(prefix, version).match(f.filename)]
        else:
            stable = _stable_re(prefix)
            matches = [f for f in remote_files if stable.match(f.filename)]

        if matches:
            return matches[-1].filename
    except Exception:
        pass
    return None


def list_containers(
    name: str | None = None,
    tag: str | None = None,
    remote: bool = False,
) -> list[ContainerInfo]:
    """List containers in the sifter registry.

    Returns parsed container metadata suitable for programmatic use.
    This is the API equivalent of ``sifter ls``.

    Args:
        name: Filter by container name (exact match).
        tag: Filter by container tag (exact match).
        remote: If True, list remote containers; otherwise list local.

    Returns:
        Sorted list of :class:`~sifter.models.ContainerInfo` objects.

    Raises:
        StorageError: If ``remote=True`` and no remote registry is configured.
    """
    config = get_config()
    storage = Storage(local_dir=config.dist_dir)

    location: Literal["local", "remote"]
    if remote:
        remote_registry = config.create_remote_registry()
        if remote_registry is None:
            raise StorageError("No remote registry configured")
        files = remote_registry.list_files(include_dev=False)
        location = "remote"
    else:
        files = storage.list_local()
        location = "local"

    results: list[ContainerInfo] = []
    for cf in files:
        try:
            container_name, container_tag, _legacy_hash = parse_filename(cf.filename)
        except ValueError:
            continue

        if name and container_name != name:
            continue
        if tag and container_tag != tag:
            continue

        results.append(
            ContainerInfo(
                name=container_name,
                tag=container_tag,
                filename=cf.filename,
                location=location,
                is_dev=cf.is_dev,
                size_bytes=cf.size_bytes,
                modified=cf.modified,
            )
        )

    return sorted(results, key=lambda c: c.filename)


# ---------------------------------------------------------------------------
# Helper: create Storage + SLURMClient from config
# ---------------------------------------------------------------------------


def _make_storage(config: SifterConfig) -> Storage:
    """Create a Storage instance for local registry."""
    return Storage(local_dir=config.dist_dir)


def _make_slurm(config: SifterConfig, *, with_staging: bool = False) -> SLURMClient:
    """Create a SLURMClient instance from config.

    Args:
        config: Sifter configuration.
        with_staging: If True, include staging_dir (needed for build operations).
    """
    return SLURMClient(
        logs_dir=config.logs_dir,
        staging_dir=config.staging_dir if with_staging else None,
        partition=config.slurm_partition,
        cpus=config.slurm_cpus,
        gpus=config.slurm_gpus,
        mem=config.slurm_mem,
    )


# ---------------------------------------------------------------------------
# Job Operations
# ---------------------------------------------------------------------------


def get_jobs(
    job_id: str | None = None,
    active_only: bool = True,
    since: timedelta | None = None,
) -> list[SLURMJob]:
    """Query SLURM for sifter jobs.

    API equivalent of ``sifter status``.

    Args:
        job_id: If set, return only this specific job (as a single-element list).
        active_only: If True and *since* is None, return only active jobs.
        since: Time window for historical jobs (e.g., ``timedelta(days=7)``).

    Returns:
        List of :class:`~sifter.slurm.SLURMJob` objects.
    """
    config = get_config()
    slurm = _make_slurm(config)

    if job_id is not None:
        job = slurm.get_job(job_id)
        return [job] if job is not None else []

    jobs = slurm.get_user_jobs(name_pattern=r"^sifter-", since=since)

    if active_only and since is None:
        jobs = [j for j in jobs if j.is_active]

    return jobs


def get_job_log(job_id: str, tail: int = 100) -> str:
    """Read log output for a SLURM job.

    API equivalent of ``sifter logs``.

    Args:
        job_id: SLURM job ID.
        tail: Number of lines to return from the end.

    Returns:
        Log content as a string.

    Raises:
        FileNotFoundError: If no log file is found for the job.
    """
    config = get_config()
    slurm = _make_slurm(config)

    job = slurm.get_job(job_id)
    if job is None:
        # Fallback: search by ID pattern
        log_files = list(config.logs_dir.glob(f"*_{job_id}.log"))
        if not log_files:
            raise FileNotFoundError(f"No logs found for job {job_id}")
        job_name = log_files[0].name.split(f"_{job_id}")[0]
        return slurm.read_log(job_name, job_id, tail=tail)

    log_path = slurm.log_path(job.name, job_id)
    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {log_path}")

    return slurm.read_log(job.name, job_id, tail=tail)


# ---------------------------------------------------------------------------
# Cache Operations
# ---------------------------------------------------------------------------


def cache_info() -> CacheInfo:
    """Get cache directory statistics.

    API equivalent of ``sifter cache`` (without ``--purge``).

    Returns:
        :class:`~sifter.models.CacheInfo` with path, file count, and total size.
    """
    config = get_config()
    cache_dir = config.cache_dir

    if not cache_dir.exists():
        return CacheInfo(path=cache_dir, file_count=0, total_bytes=0)

    cached_files = list(cache_dir.glob("*.sif"))
    total_bytes = sum(f.stat().st_size for f in cached_files)

    return CacheInfo(path=cache_dir, file_count=len(cached_files), total_bytes=total_bytes)


def cache_purge() -> int:
    """Delete all cached containers.

    API equivalent of ``sifter cache --purge --yes``.

    Returns:
        Number of files deleted.
    """
    config = get_config()
    cache_dir = config.cache_dir

    if not cache_dir.exists():
        return 0

    deleted = 0
    for f in cache_dir.glob("*.sif"):
        try:
            f.unlink()
            deleted += 1
        except OSError:
            logger.warning("Failed to delete %s", f)

    return deleted


# ---------------------------------------------------------------------------
# Registry Management
# ---------------------------------------------------------------------------


def remove_containers(name: str | None = None) -> list[str]:
    """Remove local container files from the registry.

    API equivalent of ``sifter rm --yes``.

    Args:
        name: If set, delete only containers whose filename starts with this
            prefix. If None, delete all local containers.

    Returns:
        List of deleted filenames.
    """
    config = get_config()
    storage = _make_storage(config)

    containers = storage.list_local(prefix=name)

    deleted: list[str] = []
    for cf in containers:
        path = storage.local_path(cf.filename)
        try:
            path.unlink()
            deleted.append(cf.filename)
        except OSError:
            logger.warning("Failed to delete %s", cf.filename)

    return deleted


# ---------------------------------------------------------------------------
# Transfer Operations
# ---------------------------------------------------------------------------


def _provenance_for_push(
    *, config: SifterConfig, storage: Storage, filename: str, manifest: Manifest | None = None
) -> Path | None:
    """Create provenance for a manifest-built image, or return None for ad-hoc images."""
    try:
        loaded = manifest or Manifest.load(config.manifest_path)
    except (FileNotFoundError, OSError, ValueError):
        return None
    build = next(
        (item for item in loaded.builds.values() if item.output_filename == filename), None
    )
    if build is None:
        return None
    return write_predicate(
        image=storage.local_path(filename),
        build=build,
        manifest=loaded,
        repo_dir=config.repo_dir,
        output_dir=config.staging_dir / "provenance",
    )


def push_container(filename: str, force: bool = False) -> str:
    """Push a single container to the remote registry.

    API equivalent of ``sifter push --name``.

    Args:
        filename: SIF filename to push.
        force: If True, overwrite existing files in remote registry.

    Returns:
        SLURM job ID for the transfer.

    Raises:
        StorageError: If no remote registry is configured.
        FileNotFoundError: If the file does not exist locally.
        FileExistsError: If the file already exists remotely and *force* is False.
    """
    if not filename.endswith(".sif"):
        filename += ".sif"

    config = get_config()
    storage = _make_storage(config)
    slurm = _make_slurm(config)
    remote = config.create_remote_registry()

    if remote is None:
        raise StorageError("No remote registry configured")
    if isinstance(remote, OCIRegistry):
        remote.preflight()

    if not storage.local_exists(filename):
        raise FileNotFoundError(f"File not found: {storage.local_path(filename)}")

    if remote.exists(filename) and not force:
        raise FileExistsError(f"Already exists in remote registry: {remote.uri(filename)}")

    provenance = _provenance_for_push(config=config, storage=storage, filename=filename)
    if isinstance(remote, OCIRegistry):
        cmd = remote.generate_push_command(storage.local_path(filename), filename, provenance)
    else:
        cmd = remote.generate_push_command(storage.local_path(filename), filename)
    return slurm.submit_transfer_job(command=cmd, filename=filename, is_pull=False)


def push_release(
    manifest_path: Path | None = None,
    force: bool = False,
) -> list[TransferResult]:
    """Push all manifest containers to the remote registry.

    API equivalent of ``sifter push --all`` (formerly ``--release``).

    Args:
        manifest_path: Path to sifter.yaml (defaults to config manifest path).
        force: If True, overwrite existing files in remote registry.

    Returns:
        List of :class:`~sifter.models.TransferResult` for each manifest container.

    Raises:
        StorageError: If no remote registry is configured.
        ManifestError: If the manifest cannot be loaded.
    """
    config = get_config(repo_dir=manifest_path.parent if manifest_path is not None else None)
    storage = _make_storage(config)
    slurm = _make_slurm(config)
    remote = config.create_remote_registry()

    if remote is None:
        raise StorageError("No remote registry configured")
    if isinstance(remote, OCIRegistry):
        remote.preflight()

    path = manifest_path if manifest_path is not None else config.manifest_path
    manifest = Manifest.load(path)

    manifest_filenames = {build.output_filename for build in manifest.builds.values()}
    local_files = storage.list_local()
    local_set = {f.filename for f in local_files}

    results: list[TransferResult] = []
    for fn in sorted(manifest_filenames):
        if fn not in local_set:
            continue

        if remote.exists(fn) and not force:
            results.append(TransferResult(filename=fn, job_id=None, skipped=True))
            continue

        try:
            provenance = _provenance_for_push(
                config=config, storage=storage, filename=fn, manifest=manifest
            )
            if isinstance(remote, OCIRegistry):
                cmd = remote.generate_push_command(storage.local_path(fn), fn, provenance)
            else:
                cmd = remote.generate_push_command(storage.local_path(fn), fn)
            job_id = slurm.submit_transfer_job(command=cmd, filename=fn, is_pull=False)
            results.append(TransferResult(filename=fn, job_id=job_id, skipped=False))
        except SLURMError:
            logger.warning("Failed to submit push job for %s", fn)

    return results


def pull_container(filename: str) -> str:
    """Pull a container from the remote registry to local.

    API equivalent of ``sifter pull``.

    Args:
        filename: SIF filename to pull.

    Returns:
        SLURM job ID for the transfer.

    Raises:
        StorageError: If no remote registry is configured or file not found.
    """
    if not filename.endswith(".sif"):
        filename += ".sif"

    config = get_config()
    storage = _make_storage(config)
    slurm = _make_slurm(config)
    remote = config.create_remote_registry()

    if remote is None:
        raise StorageError("No remote registry configured")
    if isinstance(remote, OCIRegistry):
        remote.preflight()

    if not remote.exists(filename):
        raise StorageError(f"Not found in remote registry: {remote.uri(filename)}")

    cmd = remote.generate_pull_command(filename, storage.local_path(filename))
    storage.ensure_local_dir()

    return slurm.submit_transfer_job(command=cmd, filename=filename, is_pull=True)


# ---------------------------------------------------------------------------
# Container Execution
# ---------------------------------------------------------------------------


def run_container(
    container: str,
    command: list[str] | None = None,
    interactive: bool = False,
    env: list[str] | None = None,
    slurm: bool = False,
    time: str = "01:00:00",
    gpus: int | None = None,
) -> RunResult:
    """Run a container locally or via SLURM.

    API equivalent of ``sifter run``.

    Args:
        container: Container name, tag, or path to SIF file.
        command: Command to run inside the container.
        interactive: Start an interactive shell.
        env: Environment variables as ``KEY=value`` strings.
        slurm: Submit as a SLURM job instead of running locally.
        time: SLURM time limit (only with ``slurm=True``).
        gpus: Number of GPUs (only with ``slurm=True``). Defaults to
            ``SIFTER_SLURM_GPUS``.

    Returns:
        :class:`~sifter.models.RunResult` with either exit_code (local) or
        job_id (SLURM).

    Raises:
        FileNotFoundError: If the container cannot be found.
        ValueError: If neither *command* nor *interactive* is specified.
    """
    if not command and not interactive:
        raise ValueError("Specify a command or set interactive=True")

    config = get_config()
    storage = _make_storage(config)

    # Resolve container path
    container_path = _resolve_container_path(container)
    if container_path is None:
        raise FileNotFoundError(
            f"Container not found: {container} (checked cwd and registry at {storage.local_dir})"
        )

    # Build singularity command
    singularity_cmd = ["singularity"]
    singularity_cmd.append("shell" if interactive else "exec")
    singularity_cmd.extend(["--nv", "--cleanenv"])

    # Bind-mount SCRATCH
    scratch = config.dist_dir.parent.parent
    singularity_cmd.extend(["-B", f"{scratch}:{scratch}"])

    if env:
        for e in env:
            singularity_cmd.extend(["--env", e])

    singularity_cmd.append(str(container_path))

    if command and not interactive:
        singularity_cmd.extend(command)

    if slurm:
        slurm_client = _make_slurm(config)
        slurm_client.ensure_logs_dir()
        job_id = slurm_client.submit_run_job(
            container_path=container_path,
            singularity_cmd=singularity_cmd,
            time=time,
            gpus=gpus,
        )
        return RunResult(job_id=job_id)
    else:
        result = subprocess.run(singularity_cmd)
        return RunResult(exit_code=result.returncode)


def _resolve_container_path(name: str) -> Path | None:
    """Resolve a container name to a local file path.

    Delegates to :func:`resolve_container` for path/registry lookup, adding
    ``.sif`` suffix handling.
    """
    # Add .sif suffix if missing before resolving
    lookup = name if name.endswith(".sif") else f"{name}.sif"
    resolved = resolve_container(lookup)
    if resolved != lookup:
        return Path(resolved)
    # Also try without .sif in case the original name is already a valid path
    if not name.endswith(".sif"):
        resolved = resolve_container(name)
        if resolved != name:
            return Path(resolved)
    return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(
    name: str | None = None,
    all: bool = False,
    manifest_path: Path | None = None,
    base: str | None = None,
    tag: str | None = None,
    steps: str | None = None,
    ad_hoc_name: str | None = None,
    dry_run: bool = False,
    reservation: str | None = None,
) -> BuildResult:
    """Build container(s) with automatic dependency resolution.

    API equivalent of ``sifter build``.

    Supports two modes:

    * **Manifest mode** (default): loads ``sifter.yaml``, resolves
      dependencies, and builds the requested target(s).
    * **Ad-hoc mode**: pass ``steps`` (comma-separated ``.def`` files)
      with a ``tag`` to build without a manifest.

    Args:
        name: Build name from the manifest (e.g., ``"vllm-0.14.0"``), or a
            ``.def`` filename for ad-hoc mode.
        all: Build every target defined in the manifest.
        manifest_path: Path to ``sifter.yaml`` (defaults to cwd).
        base: Override the base image (skips dependency resolution).
        tag: Output tag for the container.
        steps: Comma-separated ``.def`` files for ad-hoc builds.
        ad_hoc_name: Container name for ad-hoc builds (default: last step stem).
        dry_run: If True, plan the build but do not submit jobs.
        reservation: SLURM reservation name.

    Returns:
        :class:`~sifter.models.BuildResult` with the planned DAG and any
        submitted jobs.

    Raises:
        ValueError: If arguments are invalid (missing name, tag, etc.).
        FileNotFoundError: If definition files or base images are missing.
        ManifestError: If the manifest cannot be loaded.
    """
    # Route: ad-hoc mode
    if steps or (name and name.endswith(".def")):
        step_str = steps if steps else name
        if step_str is None:
            raise ValueError("steps or name must be provided for ad-hoc builds")
        return _build_adhoc(
            steps=step_str,
            tag=tag,
            ad_hoc_name=ad_hoc_name,
            base=base,
            dry_run=dry_run,
            reservation=reservation,
        )

    # Manifest mode
    if not name and not all:
        raise ValueError("Specify a build name or set all=True")

    config = get_config(repo_dir=manifest_path.parent if manifest_path is not None else None)
    path = manifest_path if manifest_path is not None else config.manifest_path
    loaded_manifest = Manifest.load(path)

    cache_storage = Storage(local_dir=config.cache_dir)
    registry = _make_storage(config)
    slurm = _make_slurm(config, with_staging=True)
    hasher = HashCache(config.repo_dir, loaded_manifest)
    remote_cache = config.create_remote_cache()
    remote_registry = config.create_remote_registry()

    if base:
        _validate_base(base, cache_storage, registry, remote_cache, remote_registry)

    if all:
        targets = list(loaded_manifest.builds.values())
    else:
        assert name is not None
        target = loaded_manifest.get_build_by_name(name)
        if target is None:
            raise ValueError(f"Build '{name}' not found in manifest")
        targets = [target]

    dag = BuildDAG()

    for target_build in targets:
        _add_build_to_dag(
            dag=dag,
            config=config,
            manifest=loaded_manifest,
            cache=cache_storage,
            registry=registry,
            hasher=hasher,
            target=target_build,
            should_tag=True,
            base_override=base,
            custom_tag=tag,
            remote_cache=remote_cache,
        )

    result = _build_result_from_dag(dag)
    if dry_run or not dag:
        return result

    result.jobs = _execute_build_dag(
        dag, config, cache_storage, registry, slurm, reservation, remote_cache
    )
    return result


def submit_build(
    plan: BuildResult,
    reservation: str | None = None,
) -> BuildResult:
    """Execute a previously planned build.

    Use with ``build(dry_run=True)`` to plan, display, confirm, then execute
    without recomputing the DAG.

    Args:
        plan: A :class:`~sifter.models.BuildResult` from ``build(dry_run=True)``.
        reservation: SLURM reservation name.

    Returns:
        :class:`~sifter.models.BuildResult` with submitted job info.
    """
    if plan.dag is None or not plan.dag.nodes:
        return plan

    config = get_config()
    cache_storage = Storage(local_dir=config.cache_dir)
    registry = _make_storage(config)
    slurm = _make_slurm(config, with_staging=True)
    remote_cache = config.create_remote_cache()

    jobs = _execute_build_dag(
        plan.dag, config, cache_storage, registry, slurm, reservation, remote_cache
    )
    return BuildResult(
        dag=plan.dag,
        jobs=jobs,
        builds_count=plan.builds_count,
        pulls_count=plan.pulls_count,
        tags_count=plan.tags_count,
    )


# ---------------------------------------------------------------------------
# Build internals
# ---------------------------------------------------------------------------


def _build_result_from_dag(dag: BuildDAG) -> BuildResult:
    """Create a BuildResult with summary counts from a DAG."""
    return BuildResult(
        dag=dag,
        builds_count=len(dag.builds_required()) if dag else 0,
        pulls_count=len(dag.pulls_required()) if dag else 0,
        tags_count=len([n for n in dag.nodes.values() if n.action == BuildAction.TAG_CACHED])
        if dag
        else 0,
    )


def _validate_base(
    base: str,
    cache: Storage,
    registry: Storage,
    remote_cache: RemoteRegistry | None = None,
    remote_registry: RemoteRegistry | None = None,
) -> None:
    """Validate that a base image exists locally.

    Raises:
        FileNotFoundError: If the base image is not found.
    """
    filename = base if base.endswith(".sif") else f"{base}.sif"
    if cache.local_exists(filename) or registry.local_exists(filename):
        return

    # Best-effort "pull first" hint. A probe that can't answer — an unmappable
    # ref, or an ECR auth/network failure — must never turn this clean local
    # error into a registry error.
    hint = ""
    try:
        if (remote_cache and remote_cache.exists(filename)) or (
            remote_registry and remote_registry.exists(filename)
        ):
            hint = f" (found in remote registry — pull first: sifter pull {filename})"
    except (StorageError, ValueError):
        pass

    raise FileNotFoundError(f"Base image '{base}' not found locally{hint}")


def _add_build_to_dag(
    dag: BuildDAG,
    config: SifterConfig,
    manifest: Manifest,
    cache: Storage,
    registry: Storage,
    hasher: HashCache,
    target: Build,
    should_tag: bool,
    base_override: str | None,
    custom_tag: str | None = None,
    remote_cache: RemoteRegistry | None = None,
) -> DAGNode | None:
    """Add a build and its dependencies to the DAG.

    Recursive — dependencies are added first. Returns the node if added.
    """
    from dataclasses import replace

    content_hash = hasher.get_hash(target)
    output_filename = cache_filename(content_hash)
    full_name = content_hash

    registry_tag: str | None = None
    if custom_tag:
        registry_tag = custom_tag
    elif should_tag:
        registry_tag = target.tag

    existing = dag.get_node(full_name)
    if existing:
        if should_tag and not existing.build_spec.should_tag:
            existing.build_spec = replace(
                existing.build_spec, should_tag=True, registry_tag=registry_tag
            )
        return existing

    action = BuildAction.BUILD
    existing_filename: str | None = None

    if cache.local_exists(output_filename):
        action = BuildAction.TAG_CACHED
        existing_filename = output_filename
    elif remote_cache is not None and remote_cache.exists(output_filename):
        action = BuildAction.PULL_REMOTE
        existing_filename = output_filename

    dep_full_names: list[str] = []
    base_image: str | None = None
    needs_deps = action == BuildAction.BUILD

    if base_override:
        base_fn = base_override if base_override.endswith(".sif") else f"{base_override}.sif"
        if cache.local_exists(base_fn):
            base_image = str(cache.local_path(base_fn))
        elif registry.local_exists(base_fn):
            base_image = str(registry.local_path(base_fn))
        else:
            raise FileNotFoundError(f"Base image '{base_override}' not found locally")
    elif needs_deps and target.base:
        dep_build = manifest.get_build_by_tag(target.base)
        if dep_build is None:
            raise ValueError(f"Dependency '{target.base}' not found in manifest")
        dep_node = _add_build_to_dag(
            dag=dag,
            config=config,
            manifest=manifest,
            cache=cache,
            registry=registry,
            hasher=hasher,
            target=dep_build,
            should_tag=False,
            base_override=None,
            remote_cache=remote_cache,
        )
        if dep_node:
            dep_full_names.append(dep_node.full_name)
            base_image = dep_node.output_filename

    args_dict: dict[str, str] = {}
    for step in target.steps:
        args_dict.update(step.args)

    build_spec = BuildSpec(
        build=target,
        definition_path=target.steps[0].path,
        args_dict=args_dict,
        output_filename=output_filename,
        base_image=base_image,
        content_hash=content_hash,
        should_tag=should_tag,
        registry_tag=registry_tag,
    )

    return dag.add_node(
        build_spec=build_spec,
        action=action,
        dependency_full_names=dep_full_names if dep_full_names else None,
        existing_filename=existing_filename,
    )


def _execute_build_dag(
    dag: BuildDAG,
    config: SifterConfig,
    cache: Storage,
    registry: Storage,
    slurm: SLURMClient,
    reservation: str | None = None,
    remote_cache: RemoteRegistry | None = None,
) -> list[BuildJobResult]:
    """Execute a build DAG by submitting SLURM jobs.

    Returns a list of submitted job results.
    """
    cache.ensure_local_dir()
    registry.ensure_local_dir()
    slurm.ensure_logs_dir()
    slurm.ensure_staging_dir()

    build_script = Path(str(files("sifter.scripts").joinpath("build.sh")))
    job_ids: dict[str, str] = {}
    results: list[BuildJobResult] = []

    for node in dag.topological_order():
        dep_job_ids = [
            job_ids[dep.full_name] for dep in node.depends_on if dep.full_name in job_ids
        ]

        if node.action == BuildAction.TAG_CACHED:
            spec = node.build_spec
            if spec.registry_tag:
                cache_file = cache.local_path(node.output_filename)
                registry_file = registry.local_path(f"{spec.registry_tag}.sif")
                if not registry_file.exists():
                    shutil.copy2(cache_file, registry_file)
            results.append(BuildJobResult(name=spec.build.name, job_id="cached", action="tag"))
            continue

        if node.action == BuildAction.PULL_REMOTE:
            spec = node.build_spec
            assert remote_cache is not None
            pull_filename = node.existing_filename or node.output_filename
            cmd = remote_cache.generate_pull_command(pull_filename, cache.local_path(pull_filename))
            if spec.registry_tag:
                cache_file = cache.local_path(node.output_filename)
                registry_file = registry.local_path(f"{spec.registry_tag}.sif")
                cmd = (
                    f"{cmd} && cp {shlex.quote(str(cache_file))} {shlex.quote(str(registry_file))}"
                )
            job_id = slurm.submit_transfer_job(
                command=cmd,
                filename=node.output_filename,
                is_pull=True,
                dependency_job_ids=dep_job_ids if dep_job_ids else None,
                reservation=reservation,
            )
            job_ids[node.full_name] = job_id
            results.append(BuildJobResult(name=spec.build.name, job_id=job_id, action="pull"))
            continue

        if node.action == BuildAction.BUILD:
            spec = node.build_spec
            staging_dir = slurm.stage_build(
                content_hash=spec.content_hash,
                definition_path=spec.definition_file,
                args=spec.args_dict,
                repo_dir=config.repo_dir,
            )

            base_image_hash: str | None = None
            if spec.base_image:
                base_image_hash = spec.base_image.removesuffix(".sif")

            # Populate the content-hash cache on build (dedup across machines).
            # Publishing to the registry is NOT automatic — that's an explicit
            # `sifter push`, so a build never surprise-publishes a release.
            push_cache_cmd: str | None = None
            cache_file = cache.local_path(cache_filename(spec.content_hash))
            if remote_cache is not None:
                push_cache_cmd = remote_cache.generate_push_command(
                    cache_file, cache_filename(spec.content_hash)
                )

            job_id = slurm.submit_staged_build(
                build_script=build_script,
                staging_dir=staging_dir,
                definition_path=spec.definition_file,
                output_hash=spec.content_hash,
                cache_dir=config.cache_dir,
                tag=spec.registry_tag,
                registry_dir=config.dist_dir if spec.registry_tag else None,
                base_image_hash=base_image_hash,
                remote_push_cache_cmd=push_cache_cmd,
                dependency_job_ids=dep_job_ids if dep_job_ids else None,
                reservation=reservation,
            )
            job_ids[node.full_name] = job_id
            results.append(BuildJobResult(name=spec.build.name, job_id=job_id, action="build"))

    return results


def _build_adhoc(
    steps: str,
    tag: str | None,
    ad_hoc_name: str | None,
    base: str | None,
    dry_run: bool,
    reservation: str | None,
) -> BuildResult:
    """Ad-hoc build from comma-separated .def files."""
    step_files = [Path(s.strip()) for s in steps.split(",")]
    for sf in step_files:
        if not sf.exists():
            raise FileNotFoundError(f"Definition file not found: {sf}")

    if not tag:
        raise ValueError("--tag is required for ad-hoc builds")

    container_name = ad_hoc_name or step_files[-1].stem
    final_tag = f"{container_name}_{tag}"

    config = get_config()
    cache_storage = Storage(local_dir=config.cache_dir)
    registry = _make_storage(config)
    slurm = _make_slurm(config, with_staging=True)
    remote_cache = config.create_remote_cache()

    # Resolve base image
    resolved_base: str | None = None
    if base:
        base_fn = base if base.endswith(".sif") else f"{base}.sif"
        if cache_storage.local_exists(base_fn):
            resolved_base = str(cache_storage.local_path(base_fn))
        elif registry.local_exists(base_fn):
            resolved_base = str(registry.local_path(base_fn))
        else:
            raise FileNotFoundError(f"Base image '{base}' not found locally")

    dag = BuildDAG()

    prev_hash: str | None = base
    prev_node_name: str | None = None

    for i, step_file in enumerate(step_files):
        is_last = i == len(step_files) - 1
        content_hash = hash_definition(step_file, base_image_hash=prev_hash)
        output_filename = cache_filename(content_hash)

        step_build = Build(
            name=step_file.stem,
            version=tag,
            steps=[Step(path=step_file, args={})],
        )

        action = BuildAction.BUILD
        if cache_storage.local_exists(output_filename):
            action = BuildAction.TAG_CACHED
        elif remote_cache is not None and remote_cache.exists(output_filename):
            action = BuildAction.PULL_REMOTE

        base_image: str | None = None
        if i > 0 and prev_hash:
            base_image = cache_filename(prev_hash)
        elif resolved_base:
            base_image = resolved_base

        registry_tag = final_tag if is_last else None

        build_spec = BuildSpec(
            build=step_build,
            definition_path=step_file,
            args_dict={},
            output_filename=output_filename,
            base_image=base_image,
            content_hash=content_hash,
            should_tag=is_last,
            registry_tag=registry_tag,
        )

        dag.add_node(
            build_spec=build_spec,
            action=action,
            dependency_full_names=[prev_node_name] if prev_node_name else None,
            existing_filename=output_filename if action != BuildAction.BUILD else None,
        )

        prev_hash = content_hash
        prev_node_name = content_hash

    result = _build_result_from_dag(dag)
    if dry_run or not dag:
        return result

    result.jobs = _execute_build_dag(
        dag, config, cache_storage, registry, slurm, reservation, remote_cache
    )
    return result
