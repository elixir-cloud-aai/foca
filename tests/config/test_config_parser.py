"""
Tests for config_parser.py
"""

import pytest

from pydantic import ValidationError

from foca.config.config_parser import ConfigParser
from foca.models.config import Config


TEST_CONFIG_INSTANCE = Config()
TEST_DICT = {}
TEST_FILE = "tests/test_files/conf_valid.yaml"
TEST_FILE_INVALID = "tests/test_files/conf_invalid_log_level.yaml"
TEST_FILE_INVALID_LOG = "tests/test_files/conf_log_invalid.yaml"


def test_config_parser_valid_config_file():
    """Test valid YAML parsing."""
    conf = ConfigParser(TEST_FILE)
    assert type(conf.config.dict()) == type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_config_parser_invalid_config_file():
    """Test invalid YAML parsing."""
    with pytest.raises(ValidationError):
        ConfigParser(TEST_FILE_INVALID)


def test_config_parser_invalid_file_path():
    """Test invalid file path."""
    conf = ConfigParser(TEST_FILE)
    with pytest.raises(OSError):
        assert conf.parse_yaml("") is not None


def test_config_parser_invalid_log_config():
    """Test invalid log config YAML."""
    conf = ConfigParser(TEST_FILE_INVALID_LOG)
    assert type(conf.config.dict()) == type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_merge_yaml_with_no_args():
    """Test merge_yaml with no arguments"""
    empty_list = []
    res = ConfigParser.merge_yaml(*empty_list)
    assert res is None
