"""Tests for the OCI / ECR remote-registry backends."""

from __future__ import annotations

import email.message
import http.client
import os
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from sifter.oci import ECRRegistry, OCIRegistry, RegistryError, registry_for, repo_and_tag
from sifter.registry import RemoteRegistry
from sifter.signing import SigningError


class TestFilenameBridge:
    @pytest.mark.parametrize(
        ("filename", "repo", "tag"),
        [
            ("vllm-0.14.0_0.0.1.sif", "vllm-0.14.0", "0.0.1"),
            ("base_1.sif", "base", "1"),
            ("pytorch_2.9.1.sif", "pytorch", "2.9.1"),
        ],
    )
    def test_filename_splits_on_last_underscore(self, filename: str, repo: str, tag: str) -> None:
        assert repo_and_tag(filename) == (repo, tag)

    @pytest.mark.parametrize("bad", ["nounderscore.sif", "_1.sif", "base_.sif"])
    def test_degenerate_filenames_are_rejected(self, bad: str) -> None:
        with pytest.raises(RegistryError, match="OCI ref"):
            repo_and_tag(bad)


class TestProtocol:
    def test_oci_registry_satisfies_remote_registry_protocol(self) -> None:
        assert isinstance(OCIRegistry("reg.example/team"), RemoteRegistry)

    def test_ecr_registry_satisfies_remote_registry_protocol(self) -> None:
        assert isinstance(ECRRegistry("123.dkr.ecr.eu-west-2.amazonaws.com/team"), RemoteRegistry)


class TestRegistryFactory:
    def test_ecr_hosts_get_the_ecr_backend_others_the_generic_one(self) -> None:
        assert type(registry_for("123.dkr.ecr.eu-west-2.amazonaws.com/team")) is ECRRegistry
        assert type(registry_for("registry.example.com/team")) is OCIRegistry

    def test_scheme_prefixed_ecr_host_still_resolves_to_ecr(self) -> None:
        assert type(registry_for("https://123.dkr.ecr.eu-west-2.amazonaws.com/x")) is ECRRegistry

    @pytest.mark.parametrize(
        "host",
        [
            "attacker.execute-api.eu-west-2.amazonaws.com/collect",
            "attacker.s3.eu-west-2.amazonaws.com/collect",
        ],
    )
    def test_non_ecr_aws_hosts_never_receive_an_ecr_login_token(self, host: str) -> None:
        # A project may choose its registry. Treating every AWS service hostname as
        # ECR would pipe a fresh ECR token into `oras login` for an attacker-owned
        # API Gateway (or other AWS endpoint).
        assert type(registry_for(host)) is OCIRegistry

    def test_options_are_forwarded_to_the_backend(self) -> None:
        reg = registry_for("reg.example/team", verify=False, oras_bin="/opt/oras")
        assert reg.verify is False
        assert reg.oras_bin == "/opt/oras"


class TestURI:
    def test_uri_is_the_scheme_less_oras_ref(self) -> None:
        reg = OCIRegistry("oras://reg.example/team")
        assert reg.uri("vllm-0.14.0_0.0.1.sif") == "reg.example/team/vllm-0.14.0:0.0.1"


class TestPushCommand:
    def test_push_pushes_a_bare_ref_and_relative_file_then_signs(self) -> None:
        reg = OCIRegistry("reg.example/team", signing_key="awskms:///alias/k")
        cmd = reg.generate_push_command(Path("/scratch/base_1.sif"), "base_1.sif")
        # bare ref (no oras:// scheme — the oras CLI rejects it), and a relative
        # basename pushed from the SIF's own dir (oras rejects absolute paths).
        assert "oras://" not in cmd
        assert cmd.startswith("cd /scratch && oras push reg.example/team/base:1 base_1.sif")
        # sign is chained with && after a successful push, never before.
        assert "oras push reg.example/team/base:1 base_1.sif && " in cmd
        assert "cosign sign --key awskms:///alias/k --yes reg.example/team/base:1" in cmd

    def test_push_attaches_provenance_after_signature(self) -> None:
        reg = OCIRegistry("reg.example/team", signing_key="awskms:///alias/k")
        cmd = reg.generate_push_command(
            Path("/scratch/base_1.sif"), "base_1.sif", Path("/scratch/base.provenance.json")
        )
        assert "cosign sign" in cmd
        assert "cosign attest" in cmd
        assert cmd.index("cosign sign") < cmd.index("cosign attest")

    def test_push_omits_cosign_when_signing_off(self) -> None:
        reg = OCIRegistry("reg.example/team", sign=False)
        cmd = reg.generate_push_command(Path("/scratch/base_1.sif"), "base_1.sif")
        assert "oras push reg.example/team/base:1 base_1.sif" in cmd
        assert "cosign" not in cmd

    def test_push_with_signing_on_but_no_key_refuses(self) -> None:
        reg = OCIRegistry("reg.example/team", signing_key=None, sign=True)
        with pytest.raises(SigningError, match="signing key"):
            reg.generate_push_command(Path("/scratch/base_1.sif"), "base_1.sif")


class TestPullCommand:
    def test_pull_verifies_and_fails_closed_removing_the_sif(self) -> None:
        reg = OCIRegistry("reg.example/team", verify_key="cosign.pub")
        cmd = reg.generate_pull_command("base_1.sif", Path("/scratch/base_1.sif"))
        assert "oras://" not in cmd
        assert cmd.startswith("oras pull reg.example/team/base:1 --output /scratch")
        assert "cosign verify --key cosign.pub reg.example/team/base:1" in cmd
        # a failed verify removes exactly the pulled SIF and errors
        assert "rm -f /scratch/base_1.sif; exit 1" in cmd

    def test_pull_omits_cosign_when_verify_off(self) -> None:
        reg = OCIRegistry("reg.example/team", verify=False)
        cmd = reg.generate_pull_command("base_1.sif", Path("/scratch/base_1.sif"))
        assert "oras pull reg.example/team/base:1 --output /scratch" in cmd
        assert "cosign" not in cmd

    def test_pull_with_verify_on_but_no_key_refuses(self) -> None:
        reg = OCIRegistry("reg.example/team", verify=True, verify_key=None)
        with pytest.raises(SigningError, match="verify key"):
            reg.generate_pull_command("base_1.sif", Path("/scratch/base_1.sif"))


class TestGeneratedCommandsExecute:
    """Run generated commands through bash with fake oras/cosign on PATH.

    Substring/exact assertions can't catch a command that real tooling rejects
    (an `oras://` scheme, an absolute push path) — these execute the shell.
    """

    _FAKE_BIN = Path(__file__).parent / "fixtures" / "fake_bin"

    def _fake_bins(self, d: Path, cosign_exit: int = 0, cosign_version: str = "v3.1.0") -> None:
        # Fake oras/cosign live in their own files under tests/fixtures/fake_bin/
        # (readable and runnable on their own). cosign's reported version and exit
        # code are supplied at run time via the environment (see _run).
        for name in ("oras", "cosign"):
            dest = d / name
            dest.write_text((self._FAKE_BIN / name).read_text())
            dest.chmod(0o755)
        self._cosign_env = {
            "FAKE_COSIGN_VERSION": cosign_version,
            "FAKE_COSIGN_EXIT": str(cosign_exit),
        }

    def _run(self, cmd: str, path_dir: Path) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{path_dir}:{os.environ['PATH']}",
            **getattr(self, "_cosign_env", {}),
        }
        return subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False
        )

    def test_generated_commands_are_valid_bash(self) -> None:
        reg = ECRRegistry(
            "123.dkr.ecr.eu-west-2.amazonaws.com/team",
            signing_key="awskms:///alias/k",
            verify_key="cosign.pub",
        )
        for cmd in (
            reg.generate_push_command(Path("/scratch/base_1.sif"), "base_1.sif"),
            reg.generate_pull_command("base_1.sif", Path("/scratch/base_1.sif")),
        ):
            check = subprocess.run(
                ["bash", "-n"], input=cmd, capture_output=True, text=True, check=False
            )
            assert check.returncode == 0, check.stderr

    def test_pull_leaves_the_sif_when_verify_passes(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        self._fake_bins(bindir, cosign_exit=0)
        dest = tmp_path / "images" / "base_1.sif"
        dest.parent.mkdir()
        reg = OCIRegistry("reg.example/team", verify_key="cosign.pub", oras_bin="oras")
        result = self._run(reg.generate_pull_command("base_1.sif", dest), bindir)
        assert result.returncode == 0, result.stderr
        assert dest.exists()

    def test_pull_removes_the_sif_when_verify_fails(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        self._fake_bins(bindir, cosign_exit=1)  # bad signature
        dest = tmp_path / "images" / "base_1.sif"
        dest.parent.mkdir()
        reg = OCIRegistry("reg.example/team", verify_key="cosign.pub", oras_bin="oras")
        result = self._run(reg.generate_pull_command("base_1.sif", dest), bindir)
        assert result.returncode != 0
        assert not dest.exists()  # fail-closed: unverified SIF removed

    def test_pull_removes_the_sif_when_cosign_is_too_old(self, tmp_path: Path) -> None:
        # A cosign that predates OCI-1.1 referrers can't verify our signatures.
        # The version floor must fail *closed*: pull, then delete the SIF — not
        # abort mid-pipeline and leave an unverified image on disk.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        self._fake_bins(bindir, cosign_exit=0, cosign_version="v2.2.0")
        dest = tmp_path / "images" / "base_1.sif"
        dest.parent.mkdir()
        reg = OCIRegistry("reg.example/team", verify_key="cosign.pub", oras_bin="oras")
        result = self._run(reg.generate_pull_command("base_1.sif", dest), bindir)
        assert result.returncode != 0
        assert not dest.exists()


class TestExistsPolarity:
    def _probe_inconclusive(self):
        return patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("u", 401, "unauth", email.message.Message(), None),
        )

    @pytest.mark.parametrize(
        "stderr",
        [
            "Error: reg.example/team/base:1: MANIFEST_UNKNOWN: manifest unknown",
            "response: NAME_UNKNOWN: repository not found",
            # Real AWS ECR phrasings observed in the wild:
            "name unknown: The repository with name 'users/x/base' "
            "does not exist in the registry with id '123456789012'",
            'failed to fetch the content of "reg/base:1": reg/base:1: not found',
        ],
    )
    def test_structured_absence_is_false(self, stderr: str) -> None:
        result = subprocess.CompletedProcess(["oras"], 1, stdout="", stderr=stderr)
        with self._probe_inconclusive(), patch("subprocess.run", return_value=result):
            assert OCIRegistry("reg.example/team").exists("base_1.sif") is False

    @pytest.mark.parametrize(
        "stderr",
        [
            'error getting credentials - err: exec: "docker-credential-ecr-login": not found',
            "unexpected status 404 from https://auth.example/token",
            "dial tcp 10.0.0.1:443: connect: connection refused",
            "x509: certificate signed by unknown authority",
        ],
    )
    def test_undeterminable_probe_raises_not_absent(self, stderr: str) -> None:
        result = subprocess.CompletedProcess(["oras"], 1, stdout="", stderr=stderr)
        with (
            self._probe_inconclusive(),
            patch("subprocess.run", return_value=result),
            pytest.raises(RegistryError, match="could not determine"),
        ):
            OCIRegistry("reg.example/team").exists("base_1.sif")

    def test_probe_timeout_raises_not_absent(self) -> None:
        with (
            self._probe_inconclusive(),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["oras"], 30)),
            pytest.raises(RegistryError, match="timed out"),
        ):
            OCIRegistry("reg.example/team").exists("base_1.sif")

    def test_oras_missing_raises_not_absent(self) -> None:
        with (
            self._probe_inconclusive(),
            patch("subprocess.run", side_effect=FileNotFoundError()),
            pytest.raises(RegistryError, match="oras not found"),
        ):
            OCIRegistry("reg.example/team").exists("base_1.sif")

    def test_head_probe_malformed_url_is_inconclusive_not_a_leak(self) -> None:
        # A control char in the tag makes urllib raise InvalidURL (a subclass of
        # HTTPException, not URLError); the HEAD probe must swallow it and fall
        # through to the oras probe, never surface it as an uncaught traceback.
        absent = subprocess.CompletedProcess(["oras"], 1, stdout="", stderr="not found")
        with (
            patch("urllib.request.urlopen", side_effect=http.client.InvalidURL("control char")),
            patch("subprocess.run", return_value=absent),
        ):
            assert OCIRegistry("reg.example/team").exists("base_1.sif") is False

    def test_head_200_is_present_without_consulting_oras(self) -> None:
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        with (
            patch("urllib.request.urlopen", return_value=_Resp()),
            patch("subprocess.run", side_effect=AssertionError("oras must not run on HEAD 200")),
        ):
            assert OCIRegistry("reg.example/team").exists("base_1.sif") is True


class TestECR:
    @pytest.mark.parametrize(
        ("host", "region"),
        [
            ("123.dkr.ecr.eu-west-2.amazonaws.com/team", "eu-west-2"),
            ("123.dkr.ecr.us-east-1.amazonaws.com/x", "us-east-1"),
            ("123.dkr.ecr-fips.us-gov-west-1.amazonaws.com/x", "us-gov-west-1"),
            ("123.dkr.ecr.cn-north-1.amazonaws.com.cn/x", "cn-north-1"),
        ],
    )
    def test_region_parsed_positionally_from_hostname(self, host: str, region: str) -> None:
        assert ECRRegistry(host).ecr_region() == region

    @pytest.mark.parametrize(
        "host",
        ["registry.example.com/team", "123.dkr.ecr.amazonaws.com/x"],
    )
    def test_unparseable_host_yields_no_region(self, host: str) -> None:
        assert ECRRegistry(host).ecr_region() is None

    @pytest.mark.parametrize(
        "host",
        [
            "1.dkr.ecr.eastus.amazonaws.com/x",  # no hyphen
            "1.dkr.ecr.us-east.amazonaws.com/x",  # no numeric zone suffix
            "1.dkr.ecr.US-EAST-1.amazonaws.com/x",  # uppercase, not a canonical region
            "1.dkr.ecr.x-$(id)-1.amazonaws.com/x",  # injection-shaped label
        ],
    )
    def test_non_region_label_after_ecr_is_rejected(self, host: str) -> None:
        # Tighter than a bare hyphen check: a label that is not AWS-region-shaped
        # must not be passed through as --region.
        assert ECRRegistry(host).ecr_region() is None

    def test_login_pipes_token_via_stdin_never_on_argv(self) -> None:
        cmd = ECRRegistry("123.dkr.ecr.eu-west-2.amazonaws.com/team").login_command()
        assert cmd == (
            "aws ecr get-login-password --region eu-west-2 | "
            "oras login --username AWS --password-stdin 123.dkr.ecr.eu-west-2.amazonaws.com"
        )

    def test_ecr_transfer_commands_self_authenticate_first(self) -> None:
        reg = ECRRegistry(
            "123.dkr.ecr.eu-west-2.amazonaws.com/team",
            signing_key="awskms:///alias/k",
            verify_key="cosign.pub",
        )
        push = reg.generate_push_command(Path("/scratch/base_1.sif"), "base_1.sif")
        pull = reg.generate_pull_command("base_1.sif", Path("/scratch/base_1.sif"))
        assert push.startswith("aws ecr get-login-password --region eu-west-2 | oras login")
        assert pull.startswith("aws ecr get-login-password --region eu-west-2 | oras login")

    def test_unparseable_ecr_host_omits_region_no_placeholder(self) -> None:
        cmd = ECRRegistry("123.dkr.ecr.amazonaws.com/x").login_command()
        assert "--region" not in cmd
        assert "<region>" not in cmd

    def test_region_from_hostname_cannot_inject_shell(self, tmp_path: Path) -> None:
        # The region is parsed from an untrusted hostname (SIFTER_REGISTRIES /
        # project sifter.yaml). A crafted region-label must be a passive arg to
        # `aws`, never a command substitution the shell evaluates.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name in ("aws", "oras"):
            (bindir / name).write_text("#!/bin/bash\nexit 0\n")
            (bindir / name).chmod(0o755)
        # The payload runs in cwd (no '/' — that would split host from path) and
        # spells its own space, since a literal one is refused before this point.
        host = "1.dkr.ecr.x-$(touch${IFS}pwned)-1.amazonaws.com/x"
        cmd = ECRRegistry(host).login_command()
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
        subprocess.run(["bash", "-c", cmd], env=env, check=False, cwd=tmp_path)
        assert not (tmp_path / "pwned").exists()

    def test_push_creates_the_ecr_repository_before_pushing(self) -> None:
        # ECR does not auto-create repositories on push; the command must, after
        # logging in and before pushing (the login supplies the create's creds).
        reg = ECRRegistry("123.dkr.ecr.eu-west-2.amazonaws.com/team", sign=False)
        cmd = reg.generate_push_command(Path("/scratch/base_1.sif"), "base_1.sif")
        assert "aws ecr create-repository --region eu-west-2 --repository-name team/base" in cmd
        assert (
            cmd.index("get-login-password")
            < cmd.index("create-repository")
            < cmd.index("oras push")
        )

    def test_pull_does_not_create_a_repository(self) -> None:
        reg = ECRRegistry("123.dkr.ecr.eu-west-2.amazonaws.com/team", verify=False)
        cmd = reg.generate_pull_command("base_1.sif", Path("/scratch/base_1.sif"))
        assert "create-repository" not in cmd

    def test_exists_probe_authenticates_before_probing(self) -> None:
        # ECR's registry API needs auth even to probe; a fresh/expired host must
        # log in first, or an absent tag reads as "cannot determine" and blocks push.
        reg = ECRRegistry("123.dkr.ecr.eu-west-2.amazonaws.com/team")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[0] == "bash":  # the login
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "team/base:1: not found")

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    "u", 401, "unauth", email.message.Message(), None
                ),
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            assert reg.exists("base_1.sif") is False
        assert calls[0][0] == "bash" and "get-login-password" in calls[0][2]
        assert calls[1][:3] == ["oras", "manifest", "fetch"]
