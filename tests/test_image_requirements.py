"""Tests of the hashed requirements of the image (`docker/requirements/`)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
REQUIREMENTS = ROOT / "docker/requirements"
SCRIPT = "scripts/compile-image-requirements.sh"


def _get_python_versions() -> set[str]:
    """Get the Python versions of the stages of the Dockerfile.

    Returns:
        Python versions.
    """
    text = (ROOT / "docker/Dockerfile").read_text()
    return set(
        re.findall(
            r"^FROM python:([0-9.]+)-slim-trixie@sha256:[0-9a-f]{64} "
            r"AS python-\1-slim-trixie$",
            text,
            re.M,
        )
    )


def test_all_stages_by_digest() -> None:
    """All base images are given by digest."""
    text = (ROOT / "docker/Dockerfile").read_text()
    versions = _get_python_versions()
    assert versions
    assert len(re.findall(r"^FROM python:", text, re.M)) == len(versions)


def _get_base_stage() -> str:
    """Get the stage `base` of the Dockerfile.

    Returns:
        Text of the stage, without its `FROM` line.
    """
    text = (ROOT / "docker/Dockerfile").read_text()
    return text.partition(" AS base\n")[2].partition(" AS builder\n")[0]


def _get_upgrade_run() -> str:
    """Get the command of the step of the stage `base` that upgrades packages.

    Returns:
        Command of the `RUN` instruction with `apt-get upgrade`, up to its last
        line (not continued with a backslash), without the comments that follow.
    """
    run = next(
        (run for run in _get_base_stage().split("\nRUN ") if "apt-get upgrade" in run),
        "",
    )
    assert run, "No step with `apt-get upgrade` in the stage `base`"
    return re.split(r"(?<!\\)\n", run, maxsplit=1)[0]


def test_base_stage_upgrades_os_packages() -> None:
    """Both later stages get the OS updates, also with pinned base images."""
    base_stage = _get_base_stage()
    run = _get_upgrade_run()
    # The update comes first and fails the step if an index can't be fetched,
    # which would otherwise leave the packages as they are, without an error
    assert re.match(
        r"apt-get update --error-on=any && \\\s+"
        r"(DEBIAN_FRONTEND=noninteractive \\\s+)?apt-get upgrade\b",
        run,
    )
    # Also updates that need new packages, which would be kept back otherwise
    assert re.search(r"\bapt-get upgrade\b[^&]*\s--with-new-pkgs\b", run)
    # Fails if updates are still kept back (e.g., as they need removals), cf.
    # `test_kept_back_guard`
    assert re.search(r"--force-confold && \\\s+simulation=", run)
    # Without prompts, of debconf and of dpkg for changed configuration files
    assert "DEBIAN_FRONTEND=noninteractive" in run
    assert "--force-confdef" in run
    assert "--force-confold" in run
    assert "rm -rf /var/lib/apt/lists/*" in run
    # All packages, not single ones, which would need a change per advisory
    assert "--only-upgrade" not in base_stage


@pytest.mark.parametrize(
    ("status", "output", "passes"),
    [
        (0, "Conf foo (1.0 Debian:13)\n", True),
        (0, "Inst foo [1.0] (1.1 Debian-Security:13)\n", False),
        # The simulation itself fails, e.g. as a lock is held
        (100, "", False),
    ],
)
def test_kept_back_guard(
    tmp_path: Path, status: int, output: str, passes: bool
) -> None:
    """The build fails if updates are kept back, or if they can't be listed.

    The step runs in `/bin/sh` (dash in Debian), without `pipefail`, so the
    check must not depend on the exit status of a pipe's first command. Runs
    the guard of the step with an `apt-get` stub that prints `output` and
    exits with `status`.
    """
    run = _get_upgrade_run()
    guard = run.partition("--force-confold && \\")[2].rpartition(" && \\")[0]
    assert "apt-get -s dist-upgrade" in guard
    stub = tmp_path / "apt-get"
    stub.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\nexit {status}\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(  # noqa: S603  # command from the Dockerfile
        ["/bin/sh", "-c", guard],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is passes, result.stderr


def test_each_version_has_requirements() -> None:
    """The files of the requirements are those of the stages."""
    assert {
        path.stem.removeprefix("py") for path in REQUIREMENTS.glob("py*.txt")
    } == _get_python_versions()
    assert (REQUIREMENTS / "build.txt").is_file()


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("ci.yml", "push"),
        ("ci.yml", "scan"),
        ("release.yml", "push"),
    ],
)
def test_matrix_has_stages(workflow: str, job: str) -> None:
    """Workflows only build images that the Dockerfile has stages for."""
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    versions = jobs[job]["strategy"]["matrix"]["python-version"]
    assert set(versions) == _get_python_versions()


@pytest.mark.parametrize("path", sorted(REQUIREMENTS.glob("*.txt")), ids=str)
def test_all_hashed(path: Path) -> None:
    """All requirements have hashes."""
    blocks = re.split(r"\n(?=[A-Za-z0-9])", path.read_text().strip())
    assert blocks
    for block in blocks:
        assert re.match(r"[A-Za-z0-9._\[\]-]+==", block), block
        assert "--hash=sha256:" in block, block


def test_dependencies_installed_despite_build_tools() -> None:
    """Dependencies that are also build tools still reach the final image."""
    text = (ROOT / "docker/Dockerfile").read_text()
    build_tools = (REQUIREMENTS / "build.txt").read_text()
    pins = re.findall(r"^([a-z0-9._-]+)==", build_tools, re.M)
    shared = {
        pin
        for path in REQUIREMENTS.glob("py*.txt")
        for pin in re.findall(r"^([a-z0-9._-]+)==", path.read_text(), re.M)
    } & set(pins)
    # On the command that installs them for the final image
    install = next(run for run in text.split("RUN ") if "--prefix" in run)
    assert not shared or "--ignore-installed" in install


def test_files_in_build_context() -> None:
    """The files are in the allowlist of the build context."""
    assert "!docker/requirements/" in (ROOT / ".dockerignore").read_text().split()


# Stands in for `uv pip compile`: keeps the output file, with another pin if
# `FAKE_PIN` is set
_FAKE_UV = """#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output-file" ]; then
        output="$2"
    fi
    shift
done
if [ -n "${FAKE_PIN:-}" ]; then
    sed -i "s/^addict==.* /addict==${FAKE_PIN} /" "${output}"
fi
"""


class TestCheck:
    """Tests of `scripts/compile-image-requirements.sh --check`."""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        """Create a copy of the files of the repository that the script needs.

        Args:
            tmp_path: Temporary directory.

        Returns:
            Path of the copy.
        """
        (tmp_path / "scripts").mkdir()
        shutil.copy(ROOT / SCRIPT, tmp_path / SCRIPT)
        shutil.copy(ROOT / "pyproject.toml", tmp_path)
        shutil.copytree(REQUIREMENTS, tmp_path / "docker/requirements")
        uv = tmp_path / "bin/uv"
        uv.parent.mkdir()
        uv.write_text(_FAKE_UV)
        uv.chmod(0o755)
        return tmp_path

    @staticmethod
    def _check(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
        """Run the check.

        Args:
            repo: Path of the copy of the repository.
            **env: Further environment variables.

        Returns:
            Result of the script.
        """
        return subprocess.run(  # noqa: S603 (script of the repository)
            ["/bin/sh", str(repo / SCRIPT), "--check"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{repo / 'bin'}:{os.environ['PATH']}",
                "TMPDIR": str(repo),
                **env,
            },
        )

    def test_fits(self, repo: Path) -> None:
        """Pins that stay as they are pass."""
        result = self._check(repo)
        assert result.returncode == 0, result.stderr

    def test_stale(self, repo: Path) -> None:
        """Pins that change fail, naming the file."""
        result = self._check(repo, FAKE_PIN="0.0.1")
        assert result.returncode == 1
        assert "Stale: docker/requirements/py3.13.txt" in result.stderr

    def test_missing(self, repo: Path) -> None:
        """Missing files fail."""
        (repo / "docker/requirements/py3.13.txt").unlink()
        result = self._check(repo)
        assert result.returncode == 1
        assert "Missing: docker/requirements/py3.13.txt" in result.stderr

    def test_invalid_argument(self, repo: Path) -> None:
        """Unknown arguments are refused."""
        result = subprocess.run(  # noqa: S603 (script of the repository)
            ["/bin/sh", str(repo / SCRIPT), "--other"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
