# Sifter

Sifter builds and maintains a matrix of Apptainer/Singularity images on
SLURM clusters — e.g. three vLLM versions across two PyTorch/CUDA bases,
each layer built on the last. The matrix is declared once in `sifter.yaml`;
one command submits every variant to SLURM in dependency order.

Sifter is a thin wrapper over Apptainer/Singularity and SLURM for
reproducible HPC container builds. You declare a chain of builds in `sifter.yaml`, and sifter
submits them as a single **SLURM dependency chain** — dependencies build before
their dependents, and a failure stops everything downstream. One `.def` file can
be parameterised into many independently-tagged variants — a **build matrix** —
by reusing it across builds with different `args`. The manifest is the source of
truth for what gets built and how.

On top of the build engine, sifter adds a **docker-style workflow** for
Apptainer images — `build`, `run`, `status`, `ls`, `push`, `pull` — and
publishes them to a **signed OCI/ECR registry**: every push is cosign-signed and
every pull is verified, fail-closed.

## Prerequisites

- **Apptainer** or **SingularityCE** — the `singularity` command must be on your
  `PATH`. Sifter shells out to it for every build (`singularity build
  --fakeroot`) and run (`singularity exec` / `shell`).
- **SLURM** (`sbatch`, `squeue`, `sacct`, `scontrol`) — builds and transfers
  always run as batch jobs; `sifter run` uses SLURM only with `--slurm`.
- A **scratch directory** — set `SCRATCH` (or `SCRATCHDIR`). Sifter keeps its
  local registry, cache, logs, and build staging under `$SCRATCH/sifter/`.
- **For a signed OCI/ECR registry** (recommended, optional):
  [`oras`](https://oras.land) for OCI push/pull,
  [`cosign`](https://github.com/sigstore/cosign) **≥ 3.0.0** for signing and
  verification, and the **`aws` CLI** when the registry is Amazon ECR. Not
  needed for local-only or shared-filesystem registries.

## Install

Sifter installs as a `uv` tool. The distribution is `sifter-build`; the CLI and
import package are both `sifter`. Python 3.10+.

```bash
# Recommended: install the CLI as an isolated tool
uv tool install "git+https://github.com/AI-Safety-Institute/sifter.git"

# Or add it to a project
uv add "sifter-build @ git+https://github.com/AI-Safety-Institute/sifter.git"

sifter version        # print the installed version
sifter update         # reinstall from the latest git commit
```

The legacy S3 backend needs an extra: `uv tool install "sifter-build[s3] @
git+https://github.com/AI-Safety-Institute/sifter.git"`. S3 is deprecated (see
[Registries](#registries)) — new setups should use OCI/ECR, which needs no extra.

## Quickstart

The commands below use the manifest in [`examples/`](examples/), which defines a
small build matrix: two base images from one `base.def`, and two apps from one
`app.def`, each app chained onto its base.

```bash
cd examples/
export SCRATCH=/tmp/sifter-demo    # if not already set by your cluster
```

```yaml
# examples/sifter.yaml (excerpt) — the build key IS the tag: <name>_<version>
builds:
  base-hello_0.0.1:
    steps:
      - path: definitions/base/base.def
        args: { BASE_MESSAGE: "Hello from base", PKG_NAME: "curl" }

  app-hello_0.0.1:
    base: base-hello_0.0.1          # declares the dependency
    steps:
      - path: definitions/app/app.def
        args: { APP_MESSAGE: "Hello from app" }
```

### (a) Build a single image

You address a build by its **name** — the part before the last `_`, *not* the
full `name_version` tag. Every `sifter build` submits an sbatch job and prints
the job id; preview first with `--dry-run`:

```bash
sifter build base-hello --dry-run     # show the plan, submit nothing
sifter build base-hello               # submit the build job
```

The built image is cached by content hash and tagged locally as
`base-hello_0.0.1.sif` in your local registry. Building never publishes to a
remote registry — that is always an explicit `sifter push` (see below).

### (b) Chain builds declaratively

A dependent build names its parent with `base:` (a full `name_version` tag). Its
definition uses the `{{ BASE_IMAGE }}` placeholder, which sifter rewrites to the
parent SIF's path at build time — Apptainer never sees the placeholder:

```
# app.def
Bootstrap: localimage
From: {{ BASE_IMAGE }}
```

Building the dependent resolves the whole chain and submits it as a SLURM
dependency chain (`--dependency=afterok`, `--kill-on-invalid-dep=yes`):

```bash
sifter build app-hello --dry-run
# base-hello
# └── [BUILD] base-hello (g70357a467c41.sif)
#     └── [BUILD] app-hello (g7d5caba771ed.sif) → app-hello_0.0.1
```

### (c) Build a matrix of variants from one `.def`

One `.def` can back several builds, each passing different `args:` — producing
independently-tagged variants of a single recipe. Here `base.def` yields
`base-hello` and `base-howdy`, and `app.def` yields `app-hello` and
`app-howdy`. `--all` plans and submits the entire matrix at once:

```bash
sifter build --all --dry-run
# base-hello
# └── [BUILD] base-hello (g70357a467c41.sif) → base-hello_0.0.1
#     └── [BUILD] app-hello (g7d5caba771ed.sif) → app-hello_0.0.1
# base-howdy
# └── [BUILD] base-howdy (g345f018ad302.sif) → base-howdy_0.0.1
#     └── [BUILD] app-howdy (g8846abc267c0.sif) → app-howdy_0.0.1
```

Each variant is an ordinary build with its own name and tag, so a downstream
build can select one with `base: base-howdy_0.0.1`.

**Ad-hoc builds** (no manifest, no `args`) are handy for one-offs. A single
definition, or several chained with `--steps` (each step becomes the next one's
base):

```bash
sifter build definitions/adhoc/base.def --tag demo            # → base_demo.sif
sifter build --steps definitions/adhoc/base.def,definitions/adhoc/app.def \
  --tag demo --ad-hoc-name myapp                              # → myapp_demo.sif
```

### (d) Inspect status and list images

```bash
sifter status                 # active sifter SLURM jobs
sifter status --all           # + completed/failed from the last 7 days
sifter status --since 24h     # custom window
sifter status --id 123456     # one job, with its log path

sifter logs 123456            # last 100 lines of a job's log
sifter logs 123456 -f         # follow

sifter ls                     # local images, grouped by name
sifter ls --name app-hello    # filter by name
```

### (e) Run an image

`sifter run` launches a container with GPU support (`--nv`), a clean environment
(`--cleanenv`), and your scratch directory bind-mounted. It resolves a bare
name/tag against the local registry, or takes a path to a `.sif`. Runs locally
by default, or as an sbatch job with `--slurm`:

```bash
sifter run app-hello_0.0.1                          # run the %runscript
sifter run app-hello_0.0.1 python -c "print('hi')"  # exec a command
sifter run -i app-hello_0.0.1                       # interactive shell
sifter run -e HF_HOME=/data/hf app-hello_0.0.1 python train.py
sifter run --slurm --gpus 1 --time 02:00:00 app-hello_0.0.1 python train.py
```

### (f) Push and pull (signed)

Configure a registry (see [Configuration](#configuration)), then publish tagged
images to it. A SIF named `<name>_<version>.sif` maps to the OCI ref
`<registry>/<name>:<version>`. Transfers run as SLURM jobs.

```bash
sifter push app-hello_0.0.1.sif           # push one image (signed)
sifter push --all                         # push every manifest image present locally
sifter push app-hello_0.0.1.sif --force   # overwrite an existing tag

sifter pull app-hello_0.0.1.sif           # pull into the local registry (verified)
```

**Every OCI push is signed and every OCI pull is verified, fail-closed** — a
push with no signing key, or a pull whose signature doesn't verify, errors
rather than proceeding (a bad pull deletes the downloaded SIF instead of leaving
it on disk). Tag mutability is the registry's own policy; sifter doesn't refuse
a re-push, but won't clobber a present tag without `--force`.

> Whole-registry listing (`sifter ls --remote`) is supported for the
> filesystem and S3 backends. OCI registries are addressed by `name:tag`, so
> there is no whole-registry listing for them — use `sifter ls` for local
> images.

### (g) Signing & verification

For manifest-built images, a signed OCI push also attaches a **SLSA v1 provenance
attestation**. Cosign binds the attestation to the subject image digest; the
predicate records the Sifter content hash, definition-file digests, dependency
identity, Sifter version and source revision
when available. Build-argument values are deliberately omitted so credentials or
other secret arguments cannot be published as provenance.

Trust is established by cosign signatures using OCI-1.1 *referrer* signatures
(the cosign v3 default — hence the ≥ 3.0.0 floor; v2 can neither produce nor read
them). Both gates default **on** and fail closed, and disable independently only
on an explicit falsy value — a typo can never silently turn a guard off:

```bash
export SIFTER_SIGNING_KEY="awskms:///alias/<your-cosign-key>"  # sign-on-push
export SIFTER_VERIFY_KEY="awskms:///alias/<your-cosign-key>"   # verify-on-pull

export SIFTER_SIGN=0     # push unsigned  (dev / airgapped only)
export SIFTER_VERIFY=0   # pull unverified (dev / airgapped only)
```

Both gates apply to the OCI/ECR path only. The shared-filesystem and (deprecated)
S3 backends cannot be cosigned at all: they ignore `SIFTER_SIGN`/`SIFTER_VERIFY`
rather than refusing to run, so images there are trusted on the strength of
filesystem or bucket permissions alone. The configuration summary says so
whenever one of them is the active backend; use OCI/ECR wherever signatures
matter.

For Amazon ECR, sifter logs in non-interactively via the `aws` CLI (region read
from the hostname) and creates the repository on first push. A private
*generic* OCI registry needs a prior `oras login`.

## Configuration

Sifter reads local paths from environment variables and resolves its registry
from YAML config files or environment. Both YAML sections resolve in the same
order — user config, then project config, then `SIFTER_*`, later winning — but not
on the same terms:

| Section | A project file may set | Merge |
| --- | --- | --- |
| `oci:` | `registries`, `probe_timeout` | per key |
| `registry:` | every key | recursive |

`./sifter.yaml` arrives with the repository, so the `oci:` section treats it as
untrusted: every key it sets there beyond those two — `oras_bin`, `sign`,
`verify`, `signing_key` and `verify_key` among them — is ignored. The run names
what was dropped on stderr, where it does not disturb `sifter latest`'s bare path
or `sifter run`'s piped output. Otherwise cloning a repo and running
`sifter pull` in it would let that repo choose which binary sifter executes,
switch signature verification off, or have signatures checked against a key its
author holds. Set those from `SIFTER_*` or your user config.

Within the allowlist the project does win over your config, which is the point of
`registries`: a repo says where its own images live, and a stray default sitting in
your user config shouldn't quietly publish every project you clone to it. It may
not empty the setting, though — an emptied `registries` would drop the whole
signed path onto the unsigned `registry:` section below it, so a project file
offering no registries is treated as a project file that said nothing.

The `registry:` section stays fully project-settable: the shared-filesystem and
S3 backends have no signing to weaken. A repo does choose the path or bucket
sifter reads and writes there, so treat one you didn't write accordingly.

### Registries

Point sifter at a **signed OCI/ECR registry** entirely from the environment
(a dev environment typically sets these for you). Registries are HTTPS only —
the presence probe and every `oras`/`cosign` call assume it, so an `http://` one
is refused; for local work use a shared-filesystem registry instead:

```bash
# Comma- or whitespace-separated. The first entry is the push + pull target.
export SIFTER_REGISTRIES="123456789012.dkr.ecr.eu-west-2.amazonaws.com/<namespace>"
export SIFTER_SIGNING_KEY="awskms:///alias/<your-cosign-key>"
export SIFTER_VERIFY_KEY="awskms:///alias/<your-cosign-key>"
```

…or in `~/.config/sifter/config.yaml` under an `oci:` section. Keys naming a
binary or a signing decision belong here, in **your** config, and nowhere else:

```yaml
oci:
  registries:
    - 123456789012.dkr.ecr.eu-west-2.amazonaws.com/<namespace>
  signing_key: "awskms:///alias/<your-cosign-key>"
  verify_key: "awskms:///alias/<your-cosign-key>"
  # sign: true / verify: true are the defaults
```

A repository's own `./sifter.yaml` may carry the two project-settable keys, to
point sifter at where that project's images live:

```yaml
oci:
  registries:
    - 123456789012.dkr.ecr.eu-west-2.amazonaws.com/<namespace>
  probe_timeout: 30
```

A **shared filesystem** (NFS) registry needs no signing tooling — and cannot use
any: images there are unsigned whatever the sign/verify gates say, so the
configuration summary says so whenever it is the active backend.

```yaml
registry:
  registry_type: filesystem
  registry_path: /shared/containers/registry
  # registry_cache_path: /shared/containers/cache   # optional; default: sibling of registry_path
```

With **no registry** configured, sifter is local-only: build, run, and inspect
work; `push`/`pull` report that no remote is configured.

#### S3 (legacy, deprecated)

> The S3 backend **cannot sign or verify** images and emits a deprecation
> warning. Migrate to OCI/ECR for signed, verifiable containers. It also needs
> the `s3` install extra.

```bash
export SIFTER_REGISTRY='{"registry_type": "s3", "s3_bucket": "my-bucket", "s3_prefix": "sifter"}'
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `SCRATCH` / `SCRATCHDIR` | Base for `$SCRATCH/sifter/{registry,cache,logs,staging}` |
| `SIFTER_DIST` | Override local registry dir (tagged `.sif` files) |
| `SIFTER_CACHE` | Override content-addressed cache dir |
| `SIFTER_LOGS` | Override SLURM logs dir |
| `SIFTER_STAGING` | Override build staging dir |
| `SIFTER_REGISTRIES` | OCI/ECR registries (list); first is the push + pull target |
| `SIFTER_SIGNING_KEY` | cosign key for sign-on-push (e.g. an IAM-gated KMS key) |
| `SIFTER_VERIFY_KEY` | cosign key/ref for verify-on-pull |
| `SIFTER_SIGN` / `SIFTER_VERIFY` | Fail-safe-ON gates; only an explicit falsy value disables |
| `SIFTER_ORAS_BIN` | Path to the `oras` binary (default: `oras`) |
| `SIFTER_PROBE_TIMEOUT` | Seconds bounding the registry presence probe; 1–600 (default: 30) |
| `SIFTER_REGISTRY` | Legacy single S3/filesystem registry as JSON (deprecated) |
| `SIFTER_SLURM_PARTITION` | SLURM partition for build/transfer/run jobs (default: `workq`) |
| `SIFTER_SLURM_CPUS` | `--cpus-per-task` for build jobs (default: `144`) |
| `SIFTER_SLURM_GPUS` | Default `--gpus` for `sifter run --slurm` (default: `1`) |
| `SIFTER_SLURM_MEM` | `--mem` for build jobs; `0` requests all node memory (default: `0`) |

> **Note:** the `SIFTER_SLURM_*` defaults are tuned for Isambard-class GH200
> nodes (4 GPUs, 72 ARM cores x2 SMT per node) and should be overridden for
> other clusters. They are passed to `sbatch` as command-line flags, which
> override the `#SBATCH` directives in the bundled job scripts.

## Manifest reference

```yaml
builds:
  # The key is the tag: <name>_<version>. The name (before the last "_") is what
  # you pass to `sifter build`; it must not contain an underscore.
  vllm-0.14.0_0.0.5:
    base: pytorch-2.9_0.0.5      # optional; a full name_version tag of another build
    steps:                       # one or more .def files, applied in order
      - path: definitions/vllm.def
        args:                    # substituted into the definition's {{ ARG }} placeholders
          VLLM_VERSION: "0.14.0"
```

- **`base`** — a dependency, referenced by its full `name_version` tag. Sifter
  builds it first and wires it in via `{{ BASE_IMAGE }}`.
- **`steps`** — a list of definition files built in sequence; each takes optional
  `args`.
- **Build matrix** — reuse one `.def` across multiple build keys with different
  `args` to produce independently-tagged variants.

See [`examples/`](examples/) for a complete, runnable manifest and definitions.

## CLI reference

| Command | What it does |
|---------|--------------|
| `sifter build [NAME]` | Plan and submit a build (or `--all`); `--dry-run`, `--tag`/`--steps`/`--ad-hoc-name` for ad-hoc, `--base`, `--manifest`, `--reservation`, `--yes` |
| `sifter ls` | List local images (`--remote` for filesystem/S3 backends; `--name`, `--tag`) |
| `sifter latest` | Print the path of the latest image matching `--prefix` (composable in scripts: the path is the only thing on stdout, notices go to stderr); `--version` |
| `sifter status` | Show sifter SLURM jobs (`--all`, `--since`, `--id`) |
| `sifter logs JOB_ID` | Show a job's log (`-f` follow, `-n` tail) |
| `sifter run CONTAINER [CMD…]` | Run a container locally or `--slurm`; `-i`, `-e KEY=val`, `--gpus`, `--time` |
| `sifter push FILE.sif` | Publish to the remote registry (or `--all` for the whole manifest); `--force`, `--manifest` |
| `sifter pull FILE.sif` | Fetch (and verify) an image from the remote registry |
| `sifter cache` | Show cache stats, or `--purge` the content cache |
| `sifter rm` | Remove local images by `--name` prefix or `--all` |
| `sifter update` / `sifter version` | Reinstall from git / print version |

### Python API

Every CLI command has a matching function in `sifter.api`, returning structured
dataclasses instead of Rich output — handy for downstream automation:

```python
import sifter

plan = sifter.build(name="app-hello", dry_run=True)
print(f"Would build {plan.builds_count} container(s)")
result = sifter.submit_build(plan)

for c in sifter.list_containers(name="app-hello"):
    print(c.filename, c.size_bytes)

sifter.push_container("app-hello_0.0.1.sif")
sifter.pull_container("app-hello_0.0.1.sif")
```

See the docstrings in [`src/sifter/api.py`](src/sifter/api.py) for the full
surface.

## How it works

Under the docker-style surface, sifter is content-addressed. Each build's inputs
(definition, sorted args, version, and dependency hashes) are hashed; the image
is cached as `<hash>.sif` and a requested target is *also* copied to the local
registry under its human-readable `name_version.sif` tag. Identical inputs
produce an identical hash, so a rebuild that would produce the same content is
skipped and the cached image is tagged instead; changing a base image cascades
new hashes to everything built on it. Publishing to a remote registry is never
automatic — it is always an explicit `sifter push`.

The whole plan is expressed as a DAG and submitted to SLURM in one shot with
`afterok` dependencies, so sifter exits immediately and lets the scheduler run
the chain; `--kill-on-invalid-dep` ensures a failed dependency cancels its
dependents rather than building on a missing base.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and the
architecture guide. In short: `uv sync --all-extras`, then
`uv run prek run --all-files` to lint, type-check, and test.

Please report a security problem privately rather than in an issue or pull
request — [SECURITY.md](SECURITY.md) says how.

## Acknowledgements

Sifter is an orchestration layer: it invokes these external tools as separate
processes rather than bundling them (install them separately — see
[Prerequisites](#prerequisites)).

- [Apptainer](https://apptainer.org/) / SingularityCE — the container runtime
  sifter builds and runs with ([3-clause BSD](https://github.com/apptainer/apptainer/blob/main/LICENSE.md)).
- [ORAS](https://oras.land/) — the OCI client sifter uses to push and pull SIFs
  as OCI artifacts ([Apache-2.0](https://github.com/oras-project/oras/blob/main/LICENSE)).
- [cosign](https://github.com/sigstore/cosign) (Sigstore) — signs OCI pushes and
  verifies pulls ([Apache-2.0](https://github.com/sigstore/cosign/blob/main/LICENSE)).
- [SLURM](https://slurm.schedmd.com/) — the workload manager sifter submits build
  and transfer jobs to ([GPL-2.0](https://slurm.schedmd.com/licenses.html)).
- [AWS CLI](https://github.com/aws/aws-cli) — authenticates to Amazon ECR
  ([Apache-2.0](https://github.com/aws/aws-cli)).
