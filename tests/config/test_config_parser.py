"""
Tests for config_parser.py
"""

import pytest
from archetype_flask_connexion.config.config_parser import YAMLConfigParser

"""
Test that update_from_yaml return a string object
"""


def test_update_from_yaml_should_returns_str(monkeypatch):

    instance = YAMLConfigParser()
    config_path = ["tests/config/sample1.yaml"]
    config_var = []

    def mock_update(*args, **kwargs):
        return

    monkeypatch.setattr(YAMLConfigParser, "update", mock_update)
    res = instance.update_from_yaml(config_path, config_var)
    assert isinstance(res, str)


# update_from_yaml should return FileNotFoundError when file path is incorrect
def test_update_from_yaml_should_return_FileNotFoundError():

    with pytest.raises(FileNotFoundError):
        instance = YAMLConfigParser()
        config_path = ["tests/config/sample2.yaml"]
        config_var = []
        instance.update_from_yaml(config_path, config_var)
