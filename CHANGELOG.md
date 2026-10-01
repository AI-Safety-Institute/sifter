# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- OCI/ECR pushes of manifest-built images now attach a Cosign-signed SLSA v1
  provenance attestation. Cosign binds the attestation to the subject image digest;
  the predicate records the Sifter content hash, definition-file digests, dependency
  identity and source revision while
  deliberately excluding build-argument values.

### Changed

- Added public package metadata and aligned the release and security-reporting
  guidance with the repository's GitHub release process.

## [0.5.0] - 2026-08-07

### Security

- ECR auto-login now runs only for canonical private-ECR hostnames
  (`<account>.dkr.ecr[-fips].<region>.amazonaws.com[.cn]`). A project controls
  its registry name, and treating every `*.amazonaws.com` endpoint as ECR would
  mint an ECR token and pipe it into `oras login` for an attacker-owned API
  Gateway or other AWS service endpoint.
- Build keys and every filename passed through local storage must now be a
  single filename component. Absolute paths, `..` traversal and nested paths
  are refused before a manifest can make a tagged build write outside the
  configured registry directory; the storage boundary repeats the check as
  defence in depth.
- A repository's own `sifter.yaml` is now untrusted for the `oci:` section: the
  only keys it may set there are `registries` and `probe_timeout`, and it still
  wins on those. Anything else it names — `oras_bin`, `sign`, `verify`,
  `signing_key` and `verify_key` among them — is ignored, and every command that
  reads configuration names what was dropped on stderr. The binary named by
  `oras_bin` is executed, so a hostile repository could run arbitrary code on
  anyone who ran sifter inside a clone of it; it could also switch signature
  verification off, or point verification at a key its own author holds. Set
  these in `~/.config/sifter/config.yaml` or the matching `SIFTER_*` variable.
- The refusal notice names the security-relevant keys first when there are too
  many to print whole. It is bounded so that a file with thousands of refused
  keys cannot scroll it off the screen, but that file chooses the names, so
  alphabetical order let it pad the list until `oras_bin` fell past the end —
  suppressing the notice that mattered while still triggering one.
- An emptied `registries` in a project file no longer deletes the OCI
  configuration it inherits. A repository may say where its own images live; a
  repository saying there is nowhere would have dropped the signed path onto the
  fully project-settable `registry:` section, which cannot verify a signature.
- A `sign:` or `verify:` gate in the user config that is neither a boolean nor a
  string is now a configuration error. YAML reads `sign: 0` as an integer, which
  previously left the gate at its default — the opposite of what was asked.
- Registry names beginning with `-` are now rejected. A project file may name the
  registries it uses, and the name is passed to `oras` and `cosign` as an
  argument, where a leading dash makes it a flag instead of a reference. The
  check runs on the name after one `oras://` or `https://` scheme is stripped, and
  a name still carrying a scheme once that is done is rejected too, so neither
  `https://--flag` nor a doubled scheme can smuggle a dash past it. A name that is
  nothing but a scheme is rejected as empty, and an `http://` one is refused on
  both paths that read a name: the configuration parser already refused it, while
  constructing a backend directly stripped the scheme and carried on. The presence
  probe and every `oras`/`cosign` command assume HTTPS.
- `probe_timeout` is now bounded to 1–600 seconds whatever sets it, including the
  user config and `SIFTER_PROBE_TIMEOUT`. A project file may set it, and an
  unbounded value lets a repository hang every sifter run in its clone, while a
  zero one makes every registry probe fail outright.
- The lines that quote a repository's own words back — the refusal notice,
  configuration errors, the configuration summary, the build plan, the container
  listings and a build's own log — are no longer parsed as console input. A
  project file could name a setting `oras_bin[/red]` and make printing the
  refusal fail instead, suppressing the one message that named it; a registry
  named `reg/[red]team[/red]` displayed as `reg/team` while `oras` and `cosign`
  got the bracketed name. Escaping would have covered markup alone, and
  emoji-code substitution and highlighting are separate layers on the same
  string.
- Registry names, registry paths, S3 bucket and prefix names, and a build's own
  key and `base:` reference are rejected when they carry control characters. A
  terminal acts on those bytes rather than showing them, so a name could erase the
  lines reporting it — `Remote:` would name one registry on screen while `oras`
  received another — and a name containing a NUL cannot be passed to `oras` or
  `cosign` at all, which previously surfaced as a traceback. A registry name long
  enough to wrap for pages is truncated in the configuration summary for the same
  reason: it would otherwise scroll the refusal notice away.
- Registries configured beyond the first are now named as ignored. Only the first
  is consulted, and that notice used to be a `UserWarning` — invisible or fatal
  depending on filters set elsewhere, with a repository choosing which by
  configuring a second registry. Neither it nor the refusal notice goes through
  the warnings module now: both have to reach the operator whether they run with
  warnings silenced or fatal.
- The shared-filesystem registry backend no longer looks as though the
  sign/verify gates cover it. They are enforced on the OCI/ECR path only, so the
  configuration summary now says so whenever that backend is active. The S3
  backend shares the limitation and keeps its own notice, which is worded as a
  deprecation.

### Added

- `SECURITY.md`: how to report a vulnerability, what is in scope, and which
  documented limitations are not vulnerabilities.

### Fixed

- `--manifest` now selects both the manifest and that manifest's project
  configuration before either registry section is resolved. Previously the
  repo directory field changed after loading configuration, leaving the
  current working directory's OCI and legacy registry settings attached.
- `sifter logs` renders terminal control characters visibly instead of passing
  them through to the terminal, where a build log could clear or rewrite the
  output around it.
- An unusable project or user config file is now reported as a configuration
  error instead of aborting with a traceback: a bad `oci:` setting, a file that is
  not a mapping, a section that is not a mapping, bytes that are not UTF-8, and
  nesting deep enough to exhaust the YAML parser's own stack.
- Registry names are stripped of surrounding whitespace, and one with whitespace
  left inside it is rejected rather than reaching `oras` as a name nobody wrote.
- `sifter run`, `sifter logs` and `sifter latest` report a refused project
  setting and a bad config file. All three resolve a configuration like every
  other command but showed neither, so a hostile `sifter.yaml` was silent there
  and a malformed one arrived as a traceback.
- Configuration notices, refusals and command errors are written to stderr, so
  `sifter latest` still composes in `$(...)` — an error on stdout became the path
  the calling script then used — and `sifter run` still pipes only the
  container's own output. The configuration summary stays on stdout: it is the
  output that was asked for, not a notice about it.
- Naming the remote no longer emits the S3 backend's deprecation warning. The
  description is read off the configuration instead of from a constructed
  backend, so asking where images go neither opens a client to the registry nor
  warns about using one.

## [0.4.0] - 2026-07-27

### Added

- SLURM resource requests are now configurable via `SIFTER_SLURM_PARTITION`, `SIFTER_SLURM_CPUS`, `SIFTER_SLURM_GPUS`, and `SIFTER_SLURM_MEM`. They are passed to `sbatch` as command-line flags (which override the `#SBATCH` directives in the bundled job scripts). Defaults are unchanged: `workq` / 144 CPUs / 1 GPU / `--mem=0`, tuned for Isambard-class GH200 nodes.

### Changed

- The canonical repository is `AI-Safety-Institute/sifter`. Install, clone, and `sifter update` URLs now point there.
- `sifter latest` now defaults to matching containers of any name. The default `--prefix` changed from `vllm-lens-` (a site-specific container name) to the empty string; pass `--prefix` explicitly to narrow the search. The same change applies to `sifter.api.find_latest_container()`.

### Fixed

- Errors about an undeterminable dist/logs/cache/staging directory now mention `SCRATCH` alongside `SCRATCHDIR`; both have always been honoured.

### Security

- Refreshed all dependency pins (notably typer 0.27, rich 15, pydantic-settings 2.14.2, pytest 9.1, ruff 0.16, ty 0.0.63) and bumped locked transitive dependencies past known advisories: urllib3 2.7.0, python-dotenv 1.2.2, and Pygments 2.20.0. No open security alerts remain.

## [0.3.0] - 2026-07-20

### Added

- Signed OCI/ECR registry backend: push and pull SIFs as OCI artifacts with `oras`, signed on push and verified on pull with cosign (fail-closed — a SIF that fails verification is removed). Configured via `SIFTER_REGISTRIES`, `SIFTER_SIGNING_KEY`, and `SIFTER_VERIFY_KEY`.

### Changed

- Renamed the distribution to `sifter-build` to avoid a PyPI name clash; the import package and `sifter` CLI are unchanged. Existing installs should `uv tool uninstall sifter` before reinstalling `sifter-build`, so the old tool environment isn't left alongside the new one.
- `sifter build` no longer pushes to the remote registry — publish explicitly with `sifter push`.

### Deprecated

- The S3 registry backend, which cannot sign or verify images. Use an OCI/ECR registry instead.

## [0.2.0] - 2026-03-23

### Added

- **Public Python API** (`sifter.api`): 16 functions matching all CLI commands, returning structured dataclasses instead of Rich output. Enables programmatic use by downstream packages.
- New data models: `ContainerInfo`, `CacheInfo`, `TransferResult`, `RunResult`, `BuildJobResult`, `BuildResult`
- `sifter latest` CLI command for finding the latest container matching a prefix
- `submit_build()` function for executing a pre-planned build without recomputing the DAG
- Exception types (`SLURMError`, `StorageError`, `ManifestError`) exported from top-level package

### Changed

- All CLI commands now delegate to `api.py` — CLI is a thin display wrapper
- Version is read from `pyproject.toml` via `importlib.metadata` (no longer hardcoded)
- `_get_config()` uses `model_copy()` for manifest path override instead of manual env var reconstruction
- `BuildJobResult.action` is now `Literal["build", "pull", "tag"]` instead of `str`

### Removed

- `ContainerStatus` enum (BUILDING/LOCAL) — unused
- `BuildAction.WAIT` and `waits_required()` — duplicate builds are harmless with content-addressed hashing
- `SLURMClient.get_build_jobs()` — no longer needed without WAIT detection
- ~850 lines of duplicated build logic from `cli.py`

## [0.1.0] - 2025-02-03

### Added

- Initial release
- DAG-based build planning with automatic dependency resolution
- Content-based caching with two-layer storage (local + S3)
- SLURM job submission with dependency chaining
- Manifest-driven builds via `sifter.yaml`
- Ad-hoc builds from definition files
- Commands: `build`, `ls`, `status`, `logs`, `run`, `push`, `pull`, `cache`, `rm`, `update`
