"""Tests of the hashed requirements of the image (`docker/requirements/`)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
REQUIREMENTS = ROOT / "docker/requirements"


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
    assert _get_python_versions()
    assert len(re.findall(r"^FROM python:", text, re.M)) == len(_get_python_versions())


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


def test_files_in_build_context() -> None:
    """The files are in the allowlist of the build context."""
    assert "!docker/requirements/" in (ROOT / ".dockerignore").read_text().split()
