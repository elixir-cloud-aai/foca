"""Tests for config.py."""

from foca.models.config import (
    Config,
    CollectionConfig,
    DBConfig,
    IndexConfig,
    MongoConfig,
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
