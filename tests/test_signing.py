"""Tests for cosign signing/verification shell fragments."""

from __future__ import annotations

from pathlib import Path

import pytest

from sifter import signing
from sifter.signing import SigningError
from sifter.storage import StorageError


def test_signing_error_is_a_storage_error() -> None:
    # So the CLI renders it like any other registry failure, not a traceback.
    assert issubclass(SigningError, StorageError)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_sign_command_without_a_real_key_refuses(key: str | None) -> None:
    with pytest.raises(SigningError, match="no signing key"):
        signing.sign_command("reg/x/app:1", key)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_verify_command_without_a_real_key_refuses(key: str | None) -> None:
    with pytest.raises(SigningError, match="no verify key"):
        signing.verify_command("reg/x/app:1", key)


def test_sign_command_emits_the_cosign_sign_invocation() -> None:
    cmd = signing.sign_command("reg/x/app:1", "awskms:///alias/k")
    assert "cosign sign --key awskms:///alias/k --yes reg/x/app:1" in cmd


def test_attest_command_emits_slsa_predicate() -> None:
    cmd = signing.attest_command(
        "reg/x/app:1", Path("/scratch/provenance.json"), "awskms:///alias/k"
    )
    assert "cosign attest" in cmd
    assert "--predicate /scratch/provenance.json" in cmd
    assert "--type https://slsa.dev/provenance/v1" in cmd
    assert "--key awskms:///alias/k --yes reg/x/app:1" in cmd


def test_verify_command_emits_the_cosign_verify_invocation() -> None:
    cmd = signing.verify_command("reg/x/app:1", "cosign.pub")
    assert "cosign verify --key cosign.pub reg/x/app:1" in cmd


class TestPreflight:
    """The fast client-side toolchain check (signing.preflight)."""

    def test_missing_oras_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signing.shutil, "which", lambda _b: None)
        with pytest.raises(SigningError, match="oras"):
            signing.preflight(sign=True, verify=True, oras_bin="oras")

    def test_signing_off_skips_the_cosign_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only oras on PATH, no cosign — fine when neither signing nor verifying.
        monkeypatch.setattr(
            signing.shutil, "which", lambda b: "/usr/bin/oras" if b == "oras" else None
        )
        signing.preflight(sign=False, verify=False, oras_bin="oras")  # no raise

    def test_missing_cosign_when_signing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            signing.shutil, "which", lambda b: "/usr/bin/oras" if b == "oras" else None
        )
        with pytest.raises(SigningError, match="cosign"):
            signing.preflight(sign=True, verify=False, oras_bin="oras")

    def test_old_cosign_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signing.shutil, "which", lambda _b: "/usr/bin/tool")
        monkeypatch.setattr(signing, "_cosign_major", lambda: 2)
        with pytest.raises(SigningError, match=r"3\.0\.0"):
            signing.preflight(sign=False, verify=True, oras_bin="oras")

    @pytest.mark.parametrize("major", [3, None])
    def test_current_or_unknown_cosign_passes(
        self, monkeypatch: pytest.MonkeyPatch, major: int | None
    ) -> None:
        # v3 is fine; an unparseable version defers to the runtime floor (no raise).
        monkeypatch.setattr(signing.shutil, "which", lambda _b: "/usr/bin/tool")
        monkeypatch.setattr(signing, "_cosign_major", lambda: major)
        signing.preflight(sign=True, verify=True, oras_bin="oras")  # no raise
