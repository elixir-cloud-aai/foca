"""Tests for `foca.py` module.
"""
import pathlib
import pytest

from connexion import App
from pydantic import ValidationError
from yaml import YAMLError

from foca.foca import foca

DIR = pathlib.Path(__file__).parent / "test_files"
EMPTY_CONF = str(DIR / "empty_conf.yaml")
INVALID_CONF = str(DIR / "invalid_conf.yaml")


def test_foca_output():
    """Ensure foca() returns a Connexion app instance; defaults only."""
    app = foca()
    assert isinstance(app, App)


def test_foca_invalid_structure():
    """Test foca(); invalid configuration file structure."""
    with pytest.raises(YAMLError):
        foca(INVALID_CONF)

