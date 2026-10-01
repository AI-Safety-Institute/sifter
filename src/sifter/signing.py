"""Cosign signing/verification, as shell fragments for the transfer job.

Registry transfers run as SLURM jobs, so signing is emitted as shell — not run
in-process. Each fragment first enforces a cosign **v3** floor (sifter uses
OCI-1.1 referrer signatures, which cosign v2 can neither produce nor read), so
a too-old cosign on the compute node fails loudly rather than silently
producing signatures the verify step can't see. *Whether* to sign/verify is the
caller's decision (the OCI registry, from its gates); *how* lives here.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

from sifter.storage import StorageError


class SigningError(StorageError):
    """Signing/verification is misconfigured (e.g. no key). A StorageError so the
    CLI renders it like any other registry failure rather than a traceback."""


# Fail unless `cosign version` reports major >= 3. Kept as one shell expression
# so it can prefix the sign/verify fragments in the generated transfer command.
# The floor fails via a non-exiting `false`, not `exit`: on pull it runs inside
# the cleanup subshell, and an `exit` there would skip the `|| (rm)` that deletes
# the unverified SIF — so an old cosign must leave a falsy status, not terminate.
_COSIGN_V3_FLOOR = (
    "{ cosign version 2>&1 | grep -qE 'GitVersion:[[:space:]]*v?([3-9]|[1-9][0-9])[.]' "
    "|| { echo 'sifter: cosign >= 3.0.0 required (OCI-1.1 referrer signatures)' >&2; false; }; }"
)


def sign_command(ref: str, signing_key: str | None) -> str:
    """Shell to sign the OCI artifact at ``ref`` (scheme-less). Raises if no key."""
    if not signing_key or not signing_key.strip():
        raise SigningError(
            "cannot sign: no signing key configured (set SIFTER_SIGNING_KEY, "
            "or SIFTER_SIGN=0 to push unsigned — dev/airgapped only)"
        )
    return f"{_COSIGN_V3_FLOOR} && cosign sign --key {shlex.quote(signing_key)} --yes {shlex.quote(ref)}"


def attest_command(ref: str, predicate_path: Path, signing_key: str | None) -> str:
    """Shell to attach a signed SLSA provenance attestation to ``ref``."""
    if not signing_key or not signing_key.strip():
        raise SigningError(
            "cannot attest provenance: no signing key configured (set SIFTER_SIGNING_KEY)"
        )
    return (
        f"{_COSIGN_V3_FLOOR} && cosign attest "
        f"--predicate {shlex.quote(str(predicate_path))} "
        f"--type https://slsa.dev/provenance/v1 "
        f"--key {shlex.quote(signing_key)} --yes {shlex.quote(ref)}"
    )


def verify_command(ref: str, verify_key: str | None) -> str:
    """Shell to verify the OCI artifact at ``ref`` (scheme-less). Raises if no key."""
    if not verify_key or not verify_key.strip():
        raise SigningError(
            "cannot verify: no verify key configured (set SIFTER_VERIFY_KEY, "
            "or SIFTER_VERIFY=0 to skip verification — dev/airgapped only)"
        )
    return f"{_COSIGN_V3_FLOOR} && cosign verify --key {shlex.quote(verify_key)} {shlex.quote(ref)}"


_COSIGN_MIN_MAJOR = 3


def preflight(*, sign: bool, verify: bool, oras_bin: str = "oras") -> None:
    """Fast client-side check of the OCI toolchain before a transfer is submitted.

    Runs on the submitting host so a missing or too-old tool fails immediately
    with a clear message, rather than only surfacing inside the SLURM job. The
    inline cosign floor in the generated shell remains the authoritative guard on
    the compute node (whose PATH can differ); this is the fast-feedback layer.
    """
    if shutil.which(oras_bin) is None:
        raise SigningError(
            f"oras client not found on PATH ({oras_bin!r}) — install oras "
            "(https://oras.land) to push or pull OCI artifacts."
        )
    if not (sign or verify):
        return
    if shutil.which("cosign") is None:
        raise SigningError(
            "cosign not found on PATH — sifter needs cosign >= 3.0.0 to sign/verify "
            "(or set SIFTER_SIGN=0 / SIFTER_VERIFY=0 for unsigned dev/airgapped use)."
        )
    major = _cosign_major()
    if major is not None and major < _COSIGN_MIN_MAJOR:
        raise SigningError(
            f"cosign >= 3.0.0 required (OCI-1.1 referrer signatures); found major version {major}."
        )


def _cosign_major() -> int | None:
    """Best-effort major version from ``cosign version``; None if undeterminable
    (the generated-shell floor still enforces the real check at run time)."""
    try:
        result = subprocess.run(
            ["cosign", "version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"GitVersion:\s*v?(\d+)\.", f"{result.stdout}\n{result.stderr}")
    return int(match.group(1)) if match else None
