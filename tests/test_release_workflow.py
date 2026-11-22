"""Tests of the release checks of the release workflow (`release.yml`).

The checks are shell scripts in steps of the workflow. The tests run them
as GitHub runs them (`bash --noprofile --norc -eo pipefail`), with stand-ins
for `gh`, `curl` and `docker` first in `PATH`, and in a temporary Git
repository for the checks of the tag.
"""

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
BASH = shutil.which("bash") or "bash"
GIT = shutil.which("git") or "git"

# Git without the user's configuration (e.g., signing), with an identity
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.org",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.org",
}

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git") and shutil.which("jq")),
    reason="needs bash, git and jq",
)

# Stand-ins for the commands that the checks call. Each prints the value of
# an environment variable and exits with the status of another one, and
# records its arguments, so that the tests control and inspect the calls
GH = """#!/usr/bin/env bash
echo "$*" >> "${CALLS}"
printf '%s\\n' "${GH_OUTPUT:-}"
exit "${GH_STATUS:-0}"
"""
# `curl --output /dev/null --write-out '%{http_code}' <url>`: the status of
# PyPI (`pypi.org`) or Docker Hub (`hub.docker.com`), the latter's body
# written to the file after `--output`
CURL = """#!/usr/bin/env bash
echo "$*" >> "${CALLS}"
url="${*: -1}"
output=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--output" ]]; then output="$2"; fi
    shift
done
case "${url}" in
    https://pypi.org/*) printf '%s' "${PYPI_STATUS}" ;;
    https://hub.docker.com/*)
        if [[ -n "${output}" && "${output}" != /dev/null ]]; then
            printf '%s' "${HUB_BODY:-}" > "${output}"
        fi
        printf '%s' "${HUB_STATUS}" ;;
    *) exit 7 ;;
esac
"""
# `docker buildx imagetools inspect <ref> --format ...`: the manifest of the
# tag in `DOCKER_MANIFESTS` (`<tag>=<digest>` per line), else "not found"
# (or the error in `DOCKER_ERROR`)
DOCKER = """#!/usr/bin/env bash
echo "$*" >> "${CALLS}"
ref=""
for arg in "$@"; do
    if [[ "${arg}" == docker.io/* ]]; then ref="${arg}"; fi
done
while IFS='=' read -r tag digest; do
    if [[ -n "${tag}" && "${ref}" == "${tag}" ]]; then
        printf '{"digest":"%s"}\\n' "${digest}"
        exit 0
    fi
done <<< "${DOCKER_MANIFESTS:-}"
echo "ERROR: ${DOCKER_ERROR:-${ref}: not found}" >&2
exit 1
"""


@functools.cache
def _steps(job: str) -> list[dict[str, Any]]:
    """Get the steps of a job of the release workflow.

    Args:
        job: ID of the job.

    Returns:
        The steps, in order.
    """
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]


def _step(job: str, name: str) -> dict[str, Any]:
    """Get a step of a job of the release workflow.

    Args:
        job: ID of the job.
        name: Name of the step.

    Returns:
        The step.
    """
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1, f"{job}: {name}"
    return matches[0]


def _names(job: str) -> list[str]:
    """Get the names of the steps of a job of the release workflow.

    Args:
        job: ID of the job.

    Returns:
        The names, in order.
    """
    return [step.get("name", "") for step in _steps(job)]


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """Directory with the stand-ins for `gh`, `curl` and `docker`.

    Args:
        tmp_path: Temporary directory of the test.

    Returns:
        The directory.
    """
    path = tmp_path / "bin"
    path.mkdir()
    for command, script in (("gh", GH), ("curl", CURL), ("docker", DOCKER)):
        (path / command).write_text(script)
        (path / command).chmod(0o755)
    return path


def _run(
    step: dict[str, Any],
    env: dict[str, str],
    bin_dir: Path,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the script of a step, as GitHub runs it.

    Args:
        step: The step.
        env: Environment variables of the step, values already resolved.
        bin_dir: Directory with the stand-ins, first in `PATH`.
        cwd: Working directory.

    Returns:
        The completed process; `GITHUB_OUTPUT` is the file `output` next to
        `bin_dir`, and `CALLS` (the calls of the stand-ins) the file `calls`.
    """
    script = bin_dir.parent / "step.sh"
    script.write_text(step["run"])
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(bin_dir.parent),
        "TMPDIR": str(bin_dir.parent),
        "GITHUB_OUTPUT": str(bin_dir.parent / "output"),
        "CALLS": str(bin_dir.parent / "calls"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        **env,
    }
    return subprocess.run(  # noqa: S603 (script of the workflow)
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestReleaseTag:
    """`prepare`, "Check release tag and commit"."""

    STEP: ClassVar[tuple[str, str]] = ("prepare", "Check release tag and commit")

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        """Git repository with a commit on `origin/main` and tags of it.

        Tags: `v1.2.0` (annotated), `v1.3.0` (lightweight).

        Args:
            tmp_path: Temporary directory of the test.

        Returns:
            The repository's directory.
        """
        path = tmp_path / "repo"
        path.mkdir()
        for args in (
            ["init", "--quiet"],
            ["commit", "--quiet", "--allow-empty", "-m", "release"],
            ["update-ref", "refs/remotes/origin/main", "HEAD"],
            ["tag", "-a", "v1.2.0", "-m", "v1.2.0"],
            ["tag", "v1.3.0"],
        ):
            subprocess.run(  # noqa: S603 (fixed arguments)
                [GIT, *args], cwd=path, env=GIT_ENV, check=True
            )
        return path

    def _env(self, tag: str, verified: str) -> dict[str, str]:
        """Environment of the step.

        Args:
            tag: Name of the tag.
            verified: What the stand-in for `gh` prints.

        Returns:
            The environment.
        """
        return {
            "TAG": tag,
            "GH_TOKEN": "token",
            "REPOSITORY": "owner/foca",
            "GH_OUTPUT": verified,
        }

    def test_signed(self, repo: Path, bin_dir: Path) -> None:
        """An annotated tag with a signature that GitHub verified passes."""
        result = _run(_step(*self.STEP), self._env("v1.2.0", "true"), bin_dir, repo)
        assert result.returncode == 0, result.stderr
        outputs = (bin_dir.parent / "output").read_text()
        assert "version=1.2.0\n" in outputs
        tag_sha = subprocess.run(  # noqa: S603 (fixed arguments)
            [GIT, "rev-parse", "refs/tags/v1.2.0"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        calls = (bin_dir.parent / "calls").read_text()
        assert (
            f"api repos/owner/foca/git/tags/{tag_sha} --jq .verification.verified"
            in calls
        )

    def test_not_on_main(self, repo: Path, bin_dir: Path) -> None:
        """A signed tag of a commit that is not on `main` fails."""
        # `origin/main` moves to an unrelated commit; HEAD stays on the tag
        for args in (
            ["checkout", "--quiet", "--orphan", "other"],
            ["commit", "--quiet", "--allow-empty", "-m", "other"],
            ["update-ref", "refs/remotes/origin/main", "HEAD"],
            ["checkout", "--quiet", "v1.2.0"],
        ):
            subprocess.run(  # noqa: S603 (fixed arguments)
                [GIT, *args], cwd=repo, env=GIT_ENV, check=True
            )
        result = _run(_step(*self.STEP), self._env("v1.2.0", "true"), bin_dir, repo)
        assert result.returncode != 0
        assert "is not on 'main'" in result.stdout
        assert not (bin_dir.parent / "output").exists()

    @pytest.mark.parametrize("verified", ["false", "null", ""])
    def test_unverified(self, repo: Path, bin_dir: Path, verified: str) -> None:
        """A tag without a signature that GitHub verified fails."""
        result = _run(_step(*self.STEP), self._env("v1.2.0", verified), bin_dir, repo)
        assert result.returncode != 0
        assert "no signature verified by GitHub" in result.stdout + result.stderr
        assert not (bin_dir.parent / "output").exists()

    def test_lookup_fails(self, repo: Path, bin_dir: Path) -> None:
        """A failed lookup of the signature fails."""
        env = {**self._env("v1.2.0", "true"), "GH_STATUS": "1"}
        result = _run(_step(*self.STEP), env, bin_dir, repo)
        assert result.returncode != 0
        assert not (bin_dir.parent / "output").exists()

    def test_lightweight(self, repo: Path, bin_dir: Path) -> None:
        """A lightweight tag fails, before any call of the API."""
        result = _run(_step(*self.STEP), self._env("v1.3.0", "true"), bin_dir, repo)
        assert result.returncode != 0
        assert "is not annotated" in result.stdout + result.stderr
        assert not (bin_dir.parent / "calls").exists()

    def test_no_release_version(self, repo: Path, bin_dir: Path) -> None:
        """A tag that is no release version fails, before any call of the API."""
        result = _run(_step(*self.STEP), self._env("v1.2", "true"), bin_dir, repo)
        assert result.returncode != 0
        assert not (bin_dir.parent / "calls").exists()


class TestVersionNotReleased:
    """`prepare`, "Check that the version is not released yet"."""

    STEP: ClassVar[tuple[str, str]] = (
        "prepare",
        "Check that the version is not released yet",
    )

    def _env(self, pypi: str, hub: str, body: str = "") -> dict[str, str]:
        """Environment of the step.

        Args:
            pypi: HTTP status of PyPI.
            hub: HTTP status of Docker Hub.
            body: Response of Docker Hub.

        Returns:
            The environment.
        """
        return {
            "VERSION": "1.2.0",
            "PACKAGE": "foca",
            "HUB_NAMESPACE": "elixircloud",
            "HUB_REPOSITORY": "foca",
            "PYPI_STATUS": pypi,
            "HUB_STATUS": hub,
            "HUB_BODY": body,
        }

    def _body(self, *names: str) -> str:
        """Response of Docker Hub with tags.

        Args:
            names: Names of the tags.

        Returns:
            The response.
        """
        results = ",".join(f'{{"name":"{name}"}}' for name in names)
        return f'{{"count":{len(names)},"next":null,"results":[{results}]}}'

    def test_order(self) -> None:
        """The check runs after the version is known, before the build."""
        names = _names("prepare")
        assert (
            names.index("Check release tag and commit")
            < names.index(self.STEP[1])
            < names.index("Build distributions")
        )

    @pytest.mark.parametrize(
        "names", [(), ("1.2.0rc1", "11.2.0", "1.2.0.post1", "1.2.0-pyfoo-x")]
    )
    def test_new(self, bin_dir: Path, names: tuple[str, ...]) -> None:
        """A version neither on PyPI nor on Docker Hub passes."""
        result = _run(
            _step(*self.STEP),
            self._env("404", "200", self._body(*names)),
            bin_dir,
            bin_dir.parent,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        calls = (bin_dir.parent / "calls").read_text()
        assert "https://pypi.org/pypi/foca/1.2.0/json" in calls
        assert (
            "https://hub.docker.com/v2/namespaces/elixircloud/repositories/foca/"
            "tags?name=1.2.0&page_size=100"
        ) in calls

    def test_no_repository(self, bin_dir: Path) -> None:
        """Without a Docker Hub repository (e.g., in a fork), nothing exists."""
        result = _run(
            _step(*self.STEP), self._env("404", "404"), bin_dir, bin_dir.parent
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_on_pypi(self, bin_dir: Path) -> None:
        """A version on PyPI fails."""
        result = _run(
            _step(*self.STEP),
            self._env("200", "200", self._body()),
            bin_dir,
            bin_dir.parent,
        )
        assert result.returncode != 0
        assert "already on PyPI" in result.stdout

    @pytest.mark.parametrize("tag", ["1.2.0", "1.2.0-py3.13"])
    def test_on_docker_hub(self, bin_dir: Path, tag: str) -> None:
        """A version with an image tag on Docker Hub fails."""
        result = _run(
            _step(*self.STEP),
            self._env("404", "200", self._body("1.2.0rc1", tag)),
            bin_dir,
            bin_dir.parent,
        )
        assert result.returncode != 0
        assert tag in result.stdout
        assert "already on Docker Hub" in result.stdout

    @pytest.mark.parametrize(
        ("pypi", "hub", "body"),
        [
            ("503", "200", ""),
            ("000", "200", ""),
            ("404", "429", ""),
            ("404", "200", "no json"),
            ("404", "200", '{"next":"https://hub.docker.com/next","results":[]}'),
        ],
    )
    def test_lookup_fails(self, bin_dir: Path, pypi: str, hub: str, body: str) -> None:
        """Failed lookups, and more tags than one page, fail."""
        result = _run(
            _step(*self.STEP), self._env(pypi, hub, body), bin_dir, bin_dir.parent
        )
        assert result.returncode != 0


class TestImageTags:
    """`release-image`, "Check image tags"."""

    STEP: ClassVar[tuple[str, str]] = ("release-image", "Check image tags")
    IMAGE: ClassVar[str] = "docker.io/elixircloud/foca"

    def _env(
        self, default: str, manifests: str = "", error: str = ""
    ) -> dict[str, str]:
        """Environment of the step.

        Args:
            default: Whether the image is that of the default Python version.
            manifests: Existing tags (`<tag>=<digest>` per line).
            error: Error of the registry, instead of "not found".

        Returns:
            The environment.
        """
        env = {
            "IMAGE_NAME": self.IMAGE,
            "DIGEST": DIGEST,
            "PY_VERSION": "3.13",
            "DEFAULT": default,
            "VERSION": "1.2.0",
            "DOCKER_MANIFESTS": manifests,
        }
        if error:
            env["DOCKER_ERROR"] = error
        return env

    def test_order(self) -> None:
        """The check runs before the image is attested, signed and tagged."""
        names = _names("release-image")
        index = names.index(self.STEP[1])
        assert names.index("Set up Docker Buildx") < index
        assert index < names.index("Attest build provenance")
        assert index < names.index("Sign image")
        assert index < names.index("Tag image")

    @pytest.mark.parametrize("default", ["true", ""])
    def test_new(self, bin_dir: Path, default: str) -> None:
        """Tags that don't exist yet pass."""
        result = _run(_step(*self.STEP), self._env(default), bin_dir, bin_dir.parent)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = (bin_dir.parent / "calls").read_text()
        assert f"{self.IMAGE}:1.2.0-py3.13" in calls
        assert (f"{self.IMAGE}:1.2.0 " in calls) == (default == "true")

    def test_same_digest(self, bin_dir: Path) -> None:
        """Tags of this run's digest pass (a re-run of failed jobs)."""
        manifests = f"{self.IMAGE}:1.2.0-py3.13={DIGEST}\n{self.IMAGE}:1.2.0={DIGEST}"
        result = _run(
            _step(*self.STEP), self._env("true", manifests), bin_dir, bin_dir.parent
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("tag", ["1.2.0-py3.13", "1.2.0"])
    def test_other_digest(self, bin_dir: Path, tag: str) -> None:
        """A tag of another digest fails."""
        manifests = f"{self.IMAGE}:{tag}={OTHER_DIGEST}"
        result = _run(
            _step(*self.STEP), self._env("true", manifests), bin_dir, bin_dir.parent
        )
        assert result.returncode != 0
        assert OTHER_DIGEST in result.stdout

    @pytest.mark.parametrize(
        "error",
        [
            "toomanyrequests: rate limit",
            # "not found", but not of the tag itself (e.g., of a blob)
            "docker.io/elixircloud/foca@sha256:0: not found",
        ],
    )
    def test_registry_fails(self, bin_dir: Path, error: str) -> None:
        """A failed lookup (other than the tag "not found") fails."""
        result = _run(
            _step(*self.STEP),
            self._env("true", error=error),
            bin_dir,
            bin_dir.parent,
        )
        assert result.returncode != 0
