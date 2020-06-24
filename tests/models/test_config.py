"""Tests for config.py."""

from pathlib import Path

from pydantic import ValidationError
import pytest

from foca.models.config import (
    Config,
    CollectionConfig,
    DBConfig,
    IndexConfig,
    MongoConfig,
    SpecConfig,
)

INDEX_CONFIG = {
    'keys': [('last_name', -1)],
    'name': 'indexLastName',
    'unique': True,
    'background': False,
    'sparse': False,
}
COLLECTION_CONFIG = {
    'indexes': [INDEX_CONFIG],
}
DB_CONFIG = {
    'collections': {
        'wes-col': COLLECTION_CONFIG,
    },
}
MONGO_CONFIG = {
    'host': 'mongodb',
    'port': 27017,
    'dbs': {
        'wes': DB_CONFIG,
    },
}
SPEC_CONFIG = {
    'path': '/my/abs/path',
    'path_out': '/my/abs/out/path',
}
SPEC_CONFIG_REL_IN = {
    'path': 'path',
    'path_out': '/my/abs/out/path',
}
SPEC_CONFIG_REL_OUT = {
    'path': '/my/abs/path',
    'path_out': 'path',
}
SPEC_CONFIG_REL_IO = {
    'path': '/my/abs/path',
    'path_out': '/my/abs/out/path',
}
SPEC_CONFIG_NO_OUT = {
    'path': '/my/abs/path',
}
SPEC_CONFIG_REL_IN_NO_OUT = {
    'path': 'path',
}
SPEC_CONFIG_NO_IN = {
    'path_out': '/my/abs/out/path',
}


def test_config_empty():
    """Test basic creation of the main config model."""
    res = Config()
    assert isinstance(res, Config)


def test_index_config_empty():
    """Test basic creation of the IndexConfig model."""
    res = IndexConfig()
    assert isinstance(res, IndexConfig)


def test_index_config_with_data():
    """Test creation of the IndexConfig model with data."""
    res = IndexConfig(**INDEX_CONFIG)
    assert isinstance(res, IndexConfig)


def test_collection_config_empty():
    """Test basic creation of the CollectionConfig model."""
    res = CollectionConfig()
    assert isinstance(res, CollectionConfig)


def test_collection_config_with_data():
    """Test creation of the CollectionConfig model with data."""
    res = CollectionConfig(**COLLECTION_CONFIG)
    assert isinstance(res, CollectionConfig)


def test_db_config_empty():
    """Test basic creation of the DBConfig model."""
    res = DBConfig()
    assert isinstance(res, DBConfig)


def test_db_config_with_data():
    """Test creation of the DBConfig model with data."""
    res = DBConfig(**DB_CONFIG)
    assert isinstance(res, DBConfig)


def test_mongo_config_empty():
    """Test basic creation of the MongoConfig model."""
    res = MongoConfig()
    assert isinstance(res, MongoConfig)


def test_mongo_config_with_data():
    """Test creation of the MongoConfig model with data."""
    res = MongoConfig(**MONGO_CONFIG)
    assert isinstance(res, MongoConfig)


def test_spec_config():
    """Test creation of the SpecConfig model with valid data."""
    res = SpecConfig(**SPEC_CONFIG)
    assert isinstance(res, SpecConfig)
    assert Path(res.path).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_in():
    """Test creation of the SpecConfig model with relative input file path."""
    res = SpecConfig(**SPEC_CONFIG_REL_IN)
    assert isinstance(res, SpecConfig)
    assert Path(res.path).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_out():
    """Test creation of the SpecConfig model with relative output file path."""
    res = SpecConfig(**SPEC_CONFIG_REL_OUT)
    assert isinstance(res, SpecConfig)
    assert Path(res.path).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_io():
    """Test creation of the SpecConfig model with relative input and output
    file paths."""
    res = SpecConfig(**SPEC_CONFIG_REL_IO)
    assert isinstance(res, SpecConfig)
    assert Path(res.path).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_no_out():
    """Test creation of the SpecConfig model with valid data."""
    res = SpecConfig(**SPEC_CONFIG_NO_OUT)
    assert isinstance(res, SpecConfig)
    assert Path(res.path).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_in_no_out():
    """Test creation of the SpecConfig model with valid data."""
    res = SpecConfig(**SPEC_CONFIG_REL_IN_NO_OUT)
    assert isinstance(res, SpecConfig)
    assert Path(res.path).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_no_in():
    """Test creation of the SpecConfig model with valid data."""
    with pytest.raises(ValidationError):
        SpecConfig(**SPEC_CONFIG_NO_IN)
