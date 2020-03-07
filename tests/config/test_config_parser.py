"""
Tests for config_parser.py
"""

import pytest


from archetype_flask_connexion.config.config_parser import YAMLConfigParser
import yaml
from addict import Dict
"""
Test for update_from_yaml function.

It should accept a config_path and config_var (environment variables)
"""
def test_update_from_yaml(mocker):
    instance = YAMLConfigParser()
    config_path = ["tests/config/sample1.yaml"]
    config_var = []
    mocker.patch('archetype_flask_connexion.config.config_parser.YAMLConfigParser.update')
    res = instance.update_from_yaml(config_path, config_var)
    assert isinstance(res, str)

