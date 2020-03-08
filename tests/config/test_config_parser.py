"""
Tests for config_parser.py
"""

import pytest


from archetype_flask_connexion.config.config_parser import YAMLConfigParser

"""
Test for update_from_yaml function.
"""
def test_update_from_yaml(monkeypatch):
    instance = YAMLConfigParser()
    config_path = ["tests/config/sample1.yaml"]
    config_var = []

    def mock_update(*args, **kwargs):
        return

    monkeypatch.setattr(YAMLConfigParser, "update", mock_update)
    res = instance.update_from_yaml(config_path, config_var)
    assert isinstance(res, str)
