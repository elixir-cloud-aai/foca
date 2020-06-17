"""
Tests for config_parser.py
"""

import pytest
from foca.config.config_parser import YAMLConfigParser
from foca.models.config import Config


TEST_FILE = "tests/test_files/conf_valid_full.yaml"
TEST_DICT = {}
TEST_CONFIG_INSTACE = Config()


def test_yaml_class():
    """Test yaml class, passing invalid YAML file path"""
    conf = YAMLConfigParser(TEST_FILE)
    with pytest.raises(Exception):
        assert conf.parse_yaml("") is not None


def test_yaml_parser():
    """Test valid YAML parsing"""
    conf = YAMLConfigParser(TEST_FILE)
    conf_dict = conf.parse_yaml(TEST_FILE)
    assert type(conf_dict) == type(TEST_DICT)
    assert type(conf.config) == type(TEST_CONFIG_INSTACE)
