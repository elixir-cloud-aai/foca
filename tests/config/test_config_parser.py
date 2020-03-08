"""
Tests for config_parser.py
"""

from archetype_flask_connexion.config.config_parser import YAMLConfigParser

"""
Test that update_from_yaml return a string object
"""


def test_update_from_yaml_returns_str(monkeypatch):

    instance = YAMLConfigParser()
    config_path = ["tests/config/sample1.yaml"]
    config_var = []

    def mock_update(*args, **kwargs):
        return

    monkeypatch.setattr(YAMLConfigParser, "update", mock_update)
    res = instance.update_from_yaml(config_path, config_var)
    assert isinstance(res, str)
