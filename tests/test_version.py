"""Tests for the package version."""

from importlib.metadata import version

import foca


def test_version():
    """Version is taken from the package metadata."""
    assert foca.__version__ == version("foca")
