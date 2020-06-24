"""
Tests for config_parser.py
"""

import pytest
import pathlib

from pydantic import ValidationError

from foca.config.config_parser import ConfigParser
from foca.models.config import Config

DIR = pathlib.Path(__file__).parent.parent
TEST_FILE = str(DIR / "test_files/conf_valid.yaml")
TEST_FILE_INVALID = str(DIR / "test_files/conf_invalid_log_level.yaml")
TEST_DICT = {}
TEST_CONFIG_INSTANCE = Config()


def test_config_parser_valid_config_file():
    """Test valid YAML parsing"""
    conf = ConfigParser(TEST_FILE)
    assert type(conf.config.dict()) == type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_config_parser_invalid_config_file():
    """Test valid YAML parsing"""
    with pytest.raises(ValidationError):
        ConfigParser(TEST_FILE_INVALID)


def test_config_parser_invalid_file_path():
    """Test yaml class, passing invalid YAML file path"""
    conf = ConfigParser(TEST_FILE)
    with pytest.raises(OSError):
        assert conf.parse_yaml("") is not None
