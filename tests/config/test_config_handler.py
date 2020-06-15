from foca.config.config_handler import Config, InvalidConfig
from pydantic import ValidationError
import pytest

TEMP_CONFIG1 = {'database': {'host': 'host1', 'port': 1, 'name': 'conf1'}}
TEMP_CONFIG2 = {'database': {'port': 2, 'name': 'conf2'}}
TEMP_CONFIG3 = {'database': {'host': 'host3', 'name': 'conf3'}}
TEMP_CONFIG4 = {'database': {'host': 'host4', 'port': 4}}
TEMP_CONFIG5 = {'db': {'host': 'h1', 'port': 1, 'name': 'c1'}}
TEMP_CONFIG6 = {'database': {'host': 1, 'port': 'abc', 'name': 1}}


def test_valid_conf_object():
    """Test for valid config format"""
    conf = Config(TEMP_CONFIG1)
    assert conf.database == TEMP_CONFIG1['database']


def test_invalid_conf_object():
    """Test for invalid config object passed"""
    with pytest.raises(InvalidConfig):
        conf = Config(TEMP_CONFIG5)
        assert conf.database == TEMP_CONFIG5['db']

    with pytest.raises(ValidationError):
        conf = Config(TEMP_CONFIG2)
        assert conf.database == TEMP_CONFIG2['database']

    with pytest.raises(ValidationError):
        conf = Config(TEMP_CONFIG3)
        assert conf.database == TEMP_CONFIG3['database']

    with pytest.raises(ValidationError):
        conf = Config(TEMP_CONFIG4)
        assert conf.database == TEMP_CONFIG4['database']

    with pytest.raises(ValidationError):
        conf = Config(TEMP_CONFIG6)
        assert conf.database == TEMP_CONFIG6['database']
