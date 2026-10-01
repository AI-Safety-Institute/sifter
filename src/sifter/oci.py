"""OCI / ECR remote-registry backends (oras client + cosign signing).

Implements the :class:`~sifter.registry.RemoteRegistry` protocol against an OCI
distribution registry, storing each SIF as an OCI artifact via the standalone
``oras`` client. ``ECRRegistry`` adds AWS ECR's non-interactive login.

Filename bridge: sifter's local SIFs are ``<name>_<version>.sif`` (a build's
``tag``); the OCI ref is ``<registry>/<name>:<version>`` — the last ``_`` in
the stem is the repo/tag split, matching how builds parse their own keys.

Registry ops run as SLURM transfer jobs, so ``generate_*_command`` returns a
shell command (no scheme prefix — the ``oras`` CLI takes bare ``host/repo:tag``,
and rejects an absolute source path, so push ``cd``s into the SIF's directory).
Push chains ``oras push`` with ``cosign sign``; pull chains ``oras pull`` with
a fail-closed ``cosign verify`` that deletes the SIF on a bad signature. ECR
commands self-authenticate (``aws ecr get-login-password | oras login``).

``exists`` probes in-process: a cheap unauthenticated HEAD (trusted only when
it says *present*), then an authenticated ``oras manifest fetch``. Presence is
decided loudly — a structured ``NAME_UNKNOWN`` / ``MANIFEST_UNKNOWN`` is absent,
but an auth/network/timeout failure raises rather than being read as "absent"
(which would rebuild a multi-hour image that actually exists).
"""

from __future__ import annotations

import http.client
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sifter import signing
from sifter.models import parse_filename
from sifter.storage import ContainerFile, StorageError
from sifter.text import reject_control_characters

_SCHEMES = ("oras://", "https://", "http://")

# AWS region and private ECR registry hostname shapes. A repository chooses its
# registry, so matching a generic ``*.amazonaws.com`` host as ECR would mint an
# ECR token and hand it to an attacker-owned AWS endpoint via ``oras login``.
_AWS_REGION = r"[a-z]{2}-[a-z]+(?:-[a-z]+)*-\d+"
_AWS_REGION_RE = re.compile(rf"^{_AWS_REGION}$")
_ECR_HOST_RE = re.compile(rf"^\d+\.dkr\.ecr(?:-fips)?\.{_AWS_REGION}\.amazonaws\.com(?:\.cn)?$")

DEFAULT_ORAS_BIN = "oras"
DEFAULT_PROBE_TIMEOUT = 30
# The unauthenticated HEAD is a best-effort fast path, so cap it: raising
# probe_timeout must not stretch this leg, and lowering it still bounds it.
_HEAD_PROBE_CAP_SECONDS = 10

_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)

# Best-effort "the artifact/repo is not there" markers in oras/registry stderr,
# matched case-insensitively. Real registries phrase absence several ways — ECR
# says "name unknown" / "does not exist in the registry" for a missing repo and
# "not found" for a missing tag. This is a heuristic, not a guarantee: a stray
# response that merely contains one of these phrases (e.g. in help text) could be
# read as absent. It is only consulted after _AUTH_ERROR_TOKENS below rules out an
# auth/network/TLS failure — the case that must never be mistaken for absence.
_ABSENCE_TOKENS = (
    "not found",
    "manifest unknown",
    "manifest_unknown",
    "name unknown",
    "name_unknown",
    "repository name not known",
    "does not exist in the registry",
)


def _is_absence(stderr: str) -> bool:
    """True if ``stderr`` definitively says the artifact/repo is absent."""
    lower = stderr.lower()
    return any(token in lower for token in _ABSENCE_TOKENS)


# Auth/credential failures. Checked *before* absence because they can share words
# with it — a missing credential helper reports "...ecr-login: not found", which
# is an auth problem, not an absent image, and must raise rather than plan a
# rebuild of something that already exists.
_AUTH_ERROR_TOKENS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication",
    "authorization",
    "expired",
    "credentials",
)


def _is_auth_error(stderr: str) -> bool:
    """True if ``stderr`` looks like an auth/credential failure (not an absence)."""
    lower = stderr.lower()
    return any(token in lower for token in _AUTH_ERROR_TOKENS)


class RegistryError(StorageError):
    """An OCI/ECR registry operation failed."""


def repo_and_tag(filename: str) -> tuple[str, str]:
    """Split a ``<name>_<version>.sif`` filename into its OCI ``(repo, tag)``.

    Reuses the build-tag grammar (last ``_`` splits name from version; a
    ``+`` build-metadata suffix is dropped) so a SIF and its OCI ref never
    disagree on the tag.
    """
    try:
        name, tag, _ = parse_filename(filename)
    except ValueError as e:
        raise RegistryError(
            f"cannot map {filename!r} to an OCI ref: expected <name>_<version>.sif"
        ) from e
    return name, tag


def _leading_scheme(url: str) -> str:
    """Return the URL scheme ``url`` begins with, or ``""`` if it has none."""
    lowered = url.lower()
    return next((scheme for scheme in _SCHEMES if lowered.startswith(scheme)), "")


def _strip_scheme(url: str) -> str:
    return url[len(_leading_scheme(url)) :]


def normalise_registry(url: str) -> str:
    """Return ``url`` as the oras commands and the config summary use it.

    The written form is not the form that reaches argv — a scheme is stripped
    first — so anything unusable has to be rejected against the stripped value,
    not the string someone typed. Nothing with a scheme still on it survives,
    which is what makes this a no-op on its own output: config loading and
    backend construction both call it and have to agree on the answer.

    Raises:
        ValueError: for plain HTTP (the HEAD probe and every generated
            oras/cosign command assume HTTPS), an empty name, a name behind more
            than one scheme, a name with whitespace or a control character inside
            it, or one oras would read as a command-line flag instead of a
            reference.
    """
    # YAML block scalars and comma-separated env lists both leave padding behind,
    # and a padded name reaches oras as a different name than the one printed back.
    trimmed = url.strip()
    if _leading_scheme(trimmed) == "http://":
        raise ValueError(
            f"plain-HTTP registries are not supported: {url!r}. "
            "Use an HTTPS registry, or a filesystem registry for local dev."
        )
    normalised = _strip_scheme(trimmed).rstrip("/")
    if not normalised:
        raise ValueError(f"registry name is empty: {url!r}")
    if any(char.isspace() for char in normalised):
        raise ValueError(
            f"registry name contains whitespace: {normalised!r}. "
            "It would reach oras and cosign as a name nobody wrote."
        )
    reject_control_characters("registry name", normalised)
    if scheme := _leading_scheme(normalised):
        raise ValueError(
            f"registry name is behind a second {scheme!r}: {url!r}. "
            "Give a host and path behind at most one scheme."
        )
    if normalised.startswith("-"):
        raise ValueError(
            f"registry names cannot start with '-': {normalised!r}. "
            "It would reach oras and cosign as a command-line flag."
        )
    return normalised


def _is_ecr_host(url: str) -> bool:
    """True if ``url``'s host is an AWS ECR endpoint (needs ECR login)."""
    host = _strip_scheme(url).split("/", 1)[0]
    return _ECR_HOST_RE.fullmatch(host) is not None


def registry_for(url: str, **opts: Any) -> OCIRegistry:
    """Build the right backend for ``url``: ``ECRRegistry`` for ECR, else ``OCIRegistry``.

    ``opts`` are forwarded to the backend constructor (signing keys, gates,
    ``oras_bin``, ``probe_timeout``), so ECR host detection lives in one place.
    """
    cls = ECRRegistry if _is_ecr_host(url) else OCIRegistry
    return cls(url, **opts)


class OCIRegistry:
    """Generic OCI-distribution registry backed by the ``oras`` client."""

    def __init__(
        self,
        url: str,
        *,
        signing_key: str | None = None,
        verify_key: str | None = None,
        sign: bool = True,
        verify: bool = True,
        oras_bin: str = DEFAULT_ORAS_BIN,
        probe_timeout: int = DEFAULT_PROBE_TIMEOUT,
    ) -> None:
        self.url = normalise_registry(url)
        self.signing_key = signing_key
        self.verify_key = verify_key
        self.sign = sign
        self.verify = verify
        self.oras_bin = oras_bin
        self.probe_timeout = probe_timeout

    def preflight(self) -> None:
        """Fail fast on the submitting host if the OCI toolchain is unusable."""
        signing.preflight(sign=self.sign, verify=self.verify, oras_bin=self.oras_bin)

    def _ref(self, filename: str) -> str:
        repo, tag = repo_and_tag(filename)
        return f"{self.url}/{repo}:{tag}"

    def _repo_path(self, repo: str) -> str:
        """Registry-relative repository path ``<namespace>/<repo>`` (namespace optional)."""
        _, _, path = self.url.partition("/")
        return f"{path}/{repo}" if path else repo

    def _auth_prefix(self) -> str:
        """Shell that must succeed before an oras op (empty for a generic registry)."""
        return ""

    def _push_prelude(self, filename: str) -> str:  # noqa: ARG002 — hook, filename used by ECR
        """Shell to run after auth and before ``oras push`` (empty generically)."""
        return ""

    def _ensure_probe_auth(self) -> None:
        """Hook: authenticate the in-process presence probe (no-op generically)."""
        return None

    # -- presence -----------------------------------------------------------

    def _manifest_probe(self, filename: str) -> bool | None:
        """Cheap unauthenticated HEAD. True only on 200; None otherwise.

        A 404 is NOT trusted as absence — a wrong/non-registry host answers 404
        too. Only a positive 200 short-circuits; everything else falls through
        to the authenticated ``oras`` probe.
        """
        repo, tag = repo_and_tag(filename)
        host = self.url.split("/", 1)[0]
        url = f"https://{host}/v2/{self._repo_path(repo)}/manifests/{tag}"
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"Accept": _MANIFEST_ACCEPT})
            with urllib.request.urlopen(
                req, timeout=min(_HEAD_PROBE_CAP_SECONDS, self.probe_timeout)
            ):
                return True
        # A malformed URL (e.g. a control char in the tag) raises InvalidURL/ValueError,
        # not URLError — treat any probe failure as inconclusive and fall through.
        except (urllib.error.URLError, OSError, http.client.InvalidURL, ValueError):
            return None

    def exists(self, filename: str) -> bool:
        if self._manifest_probe(filename):
            return True
        ref = self._ref(filename)
        self._ensure_probe_auth()
        try:
            result = subprocess.run(
                [self.oras_bin, "manifest", "fetch", ref],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.probe_timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RegistryError(
                f"presence probe for {ref} timed out after {self.probe_timeout}s"
            ) from e
        except FileNotFoundError as e:
            raise RegistryError(
                f"oras not found on PATH ({self.oras_bin!r}); install oras or set SIFTER_ORAS_BIN"
            ) from e
        if result.returncode == 0:
            return True
        # Auth failures win over absence: they can share words ("not found") but
        # mean "cannot determine", so must raise rather than read as absent.
        if not _is_auth_error(result.stderr) and _is_absence(result.stderr):
            return False
        raise RegistryError(
            f"could not determine whether {ref} exists (oras exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    def list_files(
        self,
        prefix: str | None = None,  # noqa: ARG002 — RemoteRegistry protocol signature
        include_dev: bool = False,  # noqa: ARG002
    ) -> list[ContainerFile]:
        # Listing an OCI registry requires a repository name; sifter references
        # images by <name>:<tag>, so there is no whole-registry listing here.
        raise RegistryError(
            "listing is not supported for OCI registries; use `sifter ls` for local"
        )

    def uri(self, filename: str) -> str:
        return self._ref(filename)

    # -- transfer commands (run as SLURM jobs) ------------------------------

    def generate_pull_command(self, filename: str, local_path: Path) -> str:
        ref = self._ref(filename)
        dest = Path(local_path)
        pull = (
            f"{shlex.quote(self.oras_bin)} pull {shlex.quote(ref)} "
            f"--output {shlex.quote(str(dest.parent))}"
        )
        if self.verify:
            # Fail closed: a bad/missing signature removes the pulled SIF so an
            # unverified artifact is never left on disk for a later run to reuse.
            check = signing.verify_command(ref, self.verify_key)
            pull = f"{pull} && ({check} || (rm -f {shlex.quote(str(dest))}; exit 1))"
        return f"{self._auth_prefix()}{pull}"

    def generate_push_command(
        self, local_path: Path, filename: str, provenance_path: Path | None = None
    ) -> str:
        ref = self._ref(filename)
        src = Path(local_path)
        # oras rejects an absolute source path; push the bare basename from its dir.
        push = f"cd {shlex.quote(str(src.parent))} && {shlex.quote(self.oras_bin)} push {shlex.quote(ref)} {shlex.quote(src.name)}"
        if self.sign:
            push = f"{push} && {signing.sign_command(ref, self.signing_key)}"
            if provenance_path is not None:
                push = f"{push} && {signing.attest_command(ref, provenance_path, self.signing_key)}"
        return f"{self._auth_prefix()}{self._push_prelude(filename)}{push}"

    @property
    def description(self) -> str:
        return f"oras://{self.url}"


class ECRRegistry(OCIRegistry):
    """AWS ECR registry — inherits all oras plumbing, adds ECR login + region."""

    def ecr_region(self) -> str | None:
        """AWS region from the ECR hostname, or None if unparseable.

        ECR hostnames are ``<account>.dkr.ecr[-fips].<region>.amazonaws.com[.cn]``;
        the region is the label after ``ecr``. ECR tokens are region-scoped, so
        a hostname region always wins; None means "let the AWS CLI default
        decide" — a copy-pasteable command must never carry a placeholder.
        """
        host = self.url.split("/", 1)[0]
        labels = host.split(".")
        for i, label in enumerate(labels[:-1]):
            if label in ("ecr", "ecr-fips") and i + 1 < len(labels):
                nxt = labels[i + 1]
                # The label after ecr must look like an AWS region (eu-west-2,
                # us-gov-west-1, cn-north-1, …); anything else is a malformed host,
                # so we omit --region rather than pass an unvalidated value to the
                # shell (defence-in-depth on top of shlex.quote at the use site).
                return nxt if _AWS_REGION_RE.match(nxt) else None
        return None

    def _region_flag(self) -> str:
        """`` --region <r>`` for the ECR host, or empty when the region is unknown."""
        region = self.ecr_region()
        return f" --region {shlex.quote(region)}" if region else ""

    def login_command(self) -> str:
        """Non-interactive ECR login: the token is piped via stdin, never on argv."""
        host = shlex.quote(self.url.split("/", 1)[0])
        return (
            f"aws ecr get-login-password{self._region_flag()} | "
            f"{shlex.quote(self.oras_bin)} login --username AWS --password-stdin {host}"
        )

    def _auth_prefix(self) -> str:
        return f"{self.login_command()} && "

    def _ensure_probe_auth(self) -> None:
        # ECR's registry API needs auth even to probe. The transfer commands
        # self-login on the compute node; the in-process probe runs on the
        # invoking host, so it must log in too — otherwise a missing/expired
        # token reads as "cannot determine" and blocks push/pull pre-flight. A
        # login failure is left for the probe itself to surface (loudly).
        subprocess.run(
            ["bash", "-c", self.login_command()], capture_output=True, text=True, check=False
        )

    def _push_prelude(self, filename: str) -> str:
        # ECR does not auto-create repositories on push; create it first (idempotent).
        repo, _ = repo_and_tag(filename)
        create = (
            f"aws ecr create-repository{self._region_flag()} "
            f"--repository-name {shlex.quote(self._repo_path(repo))} >/dev/null 2>&1"
        )
        return f"{{ {create} || true; }} && "
