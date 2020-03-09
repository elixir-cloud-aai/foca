"""
Tests for config_parser.py
"""

import pytest
from archetype_flask_connexion.config.config_parser import YAMLConfigParser
from archetype_flask_connexion.config.config_parser import get_conf
from archetype_flask_connexion.config.config_parser import get_conf_type


"""
Test that update_from_yaml return a string object
"""


def test_update_from_yaml_should_returns_str(monkeypatch):

    instance = YAMLConfigParser()
    print(instance)
    config_path = ["tests/config/sample1.yaml"]
    config_var = []

    def mock_update(*args, **kwargs):
        return

    monkeypatch.setattr(YAMLConfigParser, "update", mock_update)
    res = instance.update_from_yaml(config_path, config_var)
    assert isinstance(res, str)


"""
update_from_yaml should return FileNotFoundError
when file path is incorrect
"""


def test_update_from_yaml_should_return_FileNotFoundError():

    with pytest.raises(FileNotFoundError):
        instance = YAMLConfigParser()
        config_path = ["tests/config/sample2.yaml"]
        config_var = []
        instance.update_from_yaml(config_path, config_var)


"""
get_conf should return correct key value on call
"""


def test_get_conf():

    myDict = {
        "config1": "val1",
        "config2": "val2"
    }
    arg1 = "config1"
    arg2 = "config2"
    res1 = get_conf(myDict, arg1)
    res2 = get_conf(myDict, arg2)
    assert res1 == "val1"
    assert res2 == "val2"


"""
get_conf should raise KeyError when an illegal
value is provided for 'args' and touchy is false
"""


def test_get_conf_type_key_error():

    with pytest.raises(KeyError):
        myDict = {
            "config1": "val1",
            "config2": "val2"
        }
        arg1 = "config3"
        get_conf_type(myDict, arg1, touchy=False)


"""
get_conf_type should return SystemError
when touchy is false and any exception is raised
"""


def test_get_conf_type_system_error():
    myDict = {
        "config1": "val1",
        "config2": "val2"
        }
    with pytest.raises(SystemExit):
        arg1 = "config3"
        get_conf_type(myDict, arg1)
