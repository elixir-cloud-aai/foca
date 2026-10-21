"""Tests for the package version."""

import importlib
import importlib.metadata
from importlib.metadata import PackageNotFoundError, version
from typing import NoReturn

import pytest

import foca


def test_version() -> None:
    """Version is taken from the package metadata."""
    assert foca.__version__ == version("foca")


def test_version_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version is unknown if the package is not installed."""

    def not_found(name: str) -> NoReturn:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_found)
    try:
        importlib.reload(foca)
        assert foca.__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(foca)
    assert foca.__version__ == version("foca")
