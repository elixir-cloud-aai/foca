"""Tests for the package version."""

import importlib
import importlib.metadata
from importlib.metadata import PackageNotFoundError, version

import foca


def test_version():
    """Version is taken from the package metadata."""
    assert foca.__version__ == version("foca")


def test_version_not_installed(monkeypatch):
    """Version is unknown if the package is not installed."""

    def not_found(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_found)
    try:
        importlib.reload(foca)
        assert foca.__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(foca)
    assert foca.__version__ == version("foca")
