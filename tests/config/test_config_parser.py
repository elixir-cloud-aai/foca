"""
Tests for config_parser.py
"""

import pytest
from foca.config.config_parser import YAMLConfigParser
from foca.config.config_parser import get_conf
from foca.config.config_parser import get_conf_type

CONFIG_PATH_OK = ["tests/config/sample1.yaml"]
CONFIG_PATH_MISSING = []
CONFIG_PATH_INVALID = ["tests/config/samplex.yaml"]


"""
Test that update_from_yaml return a string object
"""


def test_update_from_yaml_should_returns_str(monkeypatch):

    instance = YAMLConfigParser()
    print(instance)
    config_path = CONFIG_PATH_OK
    config_var = CONFIG_PATH_MISSING

    monkeypatch.setattr(YAMLConfigParser, "update", lambda *args, **kwargs: [])
    res = instance.update_from_yaml(config_path, config_var)
    assert isinstance(res, str)


"""
update_from_yaml should return FileNotFoundError
when file path is incorrect
"""


def test_update_from_yaml_should_return_FileNotFoundError():

    with pytest.raises(FileNotFoundError):
        instance = YAMLConfigParser()
        config_path = CONFIG_PATH_INVALID
        config_var = []
        instance.update_from_yaml(config_path, config_var)


"""
get_conf should return correct key value on call
"""


def test_get_conf():

    myDict = {
        "config1": {
            "config3": "val3"
        },
        "config2": "val2"
    }
    arg1 = "config1"
    arg3 = "config3"
    arg2 = "config2"
    res1 = get_conf_type(myDict, arg1, arg3)
    res2 = get_conf(myDict, arg2)
    assert res1 == "val3"
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


"""
invert_types is false and
val is not an instance of types
then return System Error
"""


def test_invert_types_false_returns_system_exit():
    myDict = {
            "config1": "val1",
            "config2": "val2"
            }
    with pytest.raises(SystemExit):
        get_conf_type(
            myDict, "config1",
            types=(dict, list),
            invert_types=False,
            touchy=True)


"""
invert_types is false and
val is an instance of types
then return System Error
"""


def test_invert_types_true_returns_system_exit():
    myDict = {
        "config1": {
            "config3": "val3",
            "config4": "val4"
        },
        "config2": "val2"
    }

    with pytest.raises(SystemExit):
        get_conf_type(
            myDict, "config1",
            types=(dict, list),
            invert_types=True,
            touchy=True
        )
