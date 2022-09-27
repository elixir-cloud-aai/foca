"""
Tests for config_parser.py
"""

from pathlib import Path
from unittest import mock

from pydantic import (
    BaseModel,
    ValidationError,
)
import pytest

from foca.config.config_parser import ConfigParser
from foca.models.config import Config

DIR = Path(__file__).parent.parent / "test_files"
PATH = str(DIR / "openapi_2_petstore.original.yaml")
PATH_ADDITION = str(DIR / "openapi_2_petstore.addition.yaml")
TEST_CONFIG_INSTANCE = Config()
TEST_CONFIG_MODEL = 'tests.test_files.model_valid.CustomConfig'
TEST_CONFIG_MODEL_NOT_EXISTS = 'tests.test_files.model_valid.NotExists'
TEST_CONFIG_MODEL_MODULE_NOT_EXISTS = 'tests.test_files.not_a_module.NotExists'
TEST_DICT = {}
TEST_FILE = "tests/test_files/conf_valid.yaml"
TEST_FILE_CUSTOM_INVALID = "tests/test_files/conf_valid_custom_invalid.yaml"
TEST_FILE_INVALID = "tests/test_files/conf_invalid_log_level.yaml"
TEST_FILE_INVALID_YAML = "tests/test_files/conf_no_yaml.txt"
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


def test_config_parser_with_custom_config_model():
    """Test with valid custom config model class."""
    conf = ConfigParser(
        config_file=TEST_FILE,
        custom_config_model=TEST_CONFIG_MODEL,
    )
    assert isinstance(conf.config.custom.param, str)
    assert conf.config.custom.param == "STRING"


def test_process_yaml_valid_config_file():
    """Test process_yaml with valid YAML file."""
    result = ConfigParser.parse_yaml(TEST_FILE)
    assert isinstance(result, dict)


def test_process_yaml_invalid_config_file():
    """Test process_yaml with invalid YAML file."""
    with pytest.raises(ValueError):
        ConfigParser.parse_yaml(TEST_FILE_INVALID_YAML)


def test_process_yaml_missing_file():
    """Test process_yaml when file cannot be opened."""
    with mock.patch("foca.config.config_parser.open") as mock_open:
        mock_open.side_effect = OSError
        with pytest.raises(OSError):
            ConfigParser.parse_yaml(TEST_FILE)


def test_merge_yaml_with_no_args():
    """Test merge_yaml with no arguments."""
    empty_list = []
    res = ConfigParser.merge_yaml(*empty_list)
    assert res == {}


def test_merge_yaml_with_two_args():
    """Test merge_yaml with no arguments."""
    yaml_list = [PATH, PATH_ADDITION]
    res = ConfigParser.merge_yaml(*yaml_list)
    assert 'put' in res['paths']['/pets/{petId}']


def test_parse_custom_config_valid_model():
    """Test ``.parse_custom_config()`` with a valid model class."""
    conf = ConfigParser(config_file=TEST_FILE)
    result = conf.parse_custom_config(model=TEST_CONFIG_MODEL)
    assert isinstance(result, BaseModel)
    assert isinstance(result.param, str)
    assert result.param == "STRING"


def test_parse_custom_config_model_module_not_exists():
    """Test ``.parse_custom_config()`` when module does not exist."""
    conf = ConfigParser(config_file=TEST_FILE)
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL_MODULE_NOT_EXISTS)


def test_parse_custom_config_model_not_exists():
    """Test ``.parse_custom_config()`` when model class does not exist."""
    conf = ConfigParser(config_file=TEST_FILE)
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL_NOT_EXISTS)


def test_parse_custom_config_invalid():
    """Test ``.parse_custom_config()`` when model class does not exist."""
    conf = ConfigParser(config_file=TEST_FILE_CUSTOM_INVALID)
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL)
