"""Tests for config.py."""

from pathlib import Path
import sys

from pydantic import ValidationError
import pytest

from foca.models.config import (
    Config,
    CollectionConfig,
    DBConfig,
    ExceptionConfig,
    IndexConfig,
    MongoConfig,
    SpecConfig,
)

DIR = Path(__file__).parent / "test_files"
EXCEPTIONS_NO_DICT = []
EXCEPTIONS_NOT_NESTED = {'a': 'b'}
EXCEPTIONS_NOT_EXC = {'a': {'status': 400, 'title': 'Bad Request'}}
REQUIRED_MEMBERS = [['title'], ['status']]
MEMBER_TITLE = ['title']
MEMBER_STATUS = ['status']
MEMBER_NA = ['some', 'field']
MEMBERS_NA = [
    MEMBER_NA,
    MEMBER_NA + ['more']
]
MODULE_NA = "some.unavailable.module"
MODULE_WITHOUT_EXCEPTIONS = "foca.foca.exceptions"
MODULE_PATCH_NO_DICT = 'some.path.EXCEPTIONS_NO_DICT'
MODULE_PATCH_NOT_NESTED = 'some.path.EXCEPTIONS_NOT_NESTED'
MODULE_PATCH_NOT_EXC = 'some.path.EXCEPTIONS_NOT_EXC'
PATH = str(DIR / "openapi_2_petstore.yaml")
PATH_MODIFIED = str(DIR / "openapi_2_petstore.modified.yaml")
PATH_ADDITION = str(DIR / "openapi_2_petstore.addition.yaml")
INDEX_CONFIG = {
    'keys': {'last_name': -1},
    'options': {
        'name': 'indexLastName',
        'unique': True,
        'background': False,
        'sparse': False
    }
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
SPEC_CONFIG_LIST_NO_OUT = {
    'path': ['path1', 'path2']
}


def test_config_empty():
    """Test basic creation of the main config model."""
    res = Config()
    assert isinstance(res, Config)


def test_exception_config_empty():
    """Test basic creation of the ExceptionConfig model."""
    res = ExceptionConfig()
    assert isinstance(res, ExceptionConfig)


def test_exception_config_extension_members_selected():
    """Test creation of the ExceptionConfig model with selected extension
    members."""
    res = ExceptionConfig(
        extension_members=MEMBERS_NA,
    )
    assert isinstance(res, ExceptionConfig)


def test_exception_config_extension_members_all():
    """Test creation of the ExceptionConfig model with any extension
    members."""
    res = ExceptionConfig(
        extension_members=True,
    )
    assert isinstance(res, ExceptionConfig)


def test_exception_config_without_exceptions_dict():
    """Test creation of the ExceptionConfig model; exceptions dictionary is
    unavailable."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            exceptions=MODULE_WITHOUT_EXCEPTIONS,
        )


def test_exception_config_without_exceptions_dict_module_na():
    """Test creation of the ExceptionConfig model; module that should contain
    exceptions dictionary is unavailable."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            exceptions=MODULE_NA,
        )


def test_exception_config_with_wrong_exceptions_type(monkeypatch):
    """Test creation of the ExceptionConfig model; exceptions object is not
    of dictionary type."""
    monkeypatch.setattr(
        'importlib.import_module',
        lambda *args, **kwargs: sys.modules[__name__]
    )
    with pytest.raises(ValidationError):
        ExceptionConfig(
            exceptions=MODULE_PATCH_NO_DICT,
        )


def test_exception_config_with_optional_status_member():
    """Test creation of the ExceptionConfig model; status member is not among
    required members."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            extension_members=True,
            status_member=['sdf', 'asd'],
        )


def test_exception_config_with_forbidden_public_members():
    """Test creation of the ExceptionConfig model; public members are not
    subset of allowed members."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            extension_members=False,
            public_members=MEMBERS_NA,
        )


def test_exception_config_with_forbidden_private_members():
    """Test creation of the ExceptionConfig model; private members are not
    subset of allowed members."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            extension_members=False,
            private_members=MEMBERS_NA,
        )


def test_exception_config_with_conflicting_public_and_private_members():
    """Test creation of the ExceptionConfig model; both public and private
    member filters active."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            extension_members=True,
            public_members=MEMBERS_NA,
            private_members=MEMBERS_NA,
        )


def test_exception_config_status_member_not_integer():
    """Test creation of the ExceptionConfig model; status member is not or
    cannot be cast to type integer."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            status_member=MEMBER_TITLE,
        )


def test_exception_config_with_wrong_exception_values(monkeypatch):
    """Test creation of the ExceptionConfig model; exceptions object is not
    a dictionary of dictionaries."""
    monkeypatch.setattr(
        'importlib.import_module',
        lambda *args, **kwargs: sys.modules[__name__]
    )
    with pytest.raises(ValidationError):
        ExceptionConfig(
            exceptions=MODULE_PATCH_NOT_NESTED,
        )


def test_exception_config_with_wrong_exception_keys(monkeypatch):
    """Test creation of the ExceptionConfig model; exceptions object is not
    a dictionary of exceptions."""
    monkeypatch.setattr(
        'importlib.import_module',
        lambda *args, **kwargs: sys.modules[__name__]
    )
    with pytest.raises(ValidationError):
        ExceptionConfig(
            exceptions=MODULE_PATCH_NOT_EXC,
        )


def test_exception_config_missing_required_members():
    """Test creation of the ExceptionConfig model; required members are
    missing."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            required_members=REQUIRED_MEMBERS + MEMBERS_NA
        )


def test_exception_config_forbidden_extension_members():
    """Test creation of the ExceptionConfig model; not all provided members
    allowed by extension member settings."""
    with pytest.raises(ValidationError):
        ExceptionConfig(
            required_members=[MEMBER_STATUS],
            extension_members=False,
        )


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
    assert Path(res.path[0]).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_in():
    """Test creation of the SpecConfig model with relative input file path."""
    res = SpecConfig(**SPEC_CONFIG_REL_IN)
    assert isinstance(res, SpecConfig)
    assert Path(res.path[0]).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_out():
    """Test creation of the SpecConfig model with relative output file path."""
    res = SpecConfig(**SPEC_CONFIG_REL_OUT)
    assert isinstance(res, SpecConfig)
    assert Path(res.path[0]).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_io():
    """Test creation of the SpecConfig model with relative input and output
    file paths."""
    res = SpecConfig(**SPEC_CONFIG_REL_IO)
    assert isinstance(res, SpecConfig)
    assert Path(res.path[0]).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_no_out():
    """Test creation of the SpecConfig model with valid data."""
    res = SpecConfig(**SPEC_CONFIG_NO_OUT)
    assert isinstance(res, SpecConfig)
    assert Path(res.path[0]).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_rel_in_no_out():
    """Test creation of the SpecConfig model with valid data."""
    res = SpecConfig(**SPEC_CONFIG_REL_IN_NO_OUT)
    assert isinstance(res, SpecConfig)
    assert Path(res.path[0]).is_absolute()
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_spec_config_no_in():
    """Test creation of the SpecConfig model with valid data."""
    with pytest.raises(ValidationError):
        SpecConfig(**SPEC_CONFIG_NO_IN)


def test_spec_config_list_no_out():
    """Test creation of SpecConfig model with valid list data in path"""
    res = SpecConfig(**SPEC_CONFIG_LIST_NO_OUT)
    assert isinstance(res, SpecConfig)
    assert res.path_out is not None
    assert Path(res.path_out).is_absolute()


def test_SpecConfig_full():
    """Test SpecConfig instantiation; full example"""
    res = SpecConfig(**SPEC_CONFIG)
    assert res.path_out == SPEC_CONFIG['path_out']


def test_SpecConfig_minimal():
    """Test SpecConfig instantiation; minimal example"""
    res = SpecConfig(path=PATH)
    assert res.path_out == PATH_MODIFIED


def test_SpecConfig_merge():
    """Test SpecConfig instantiation; multiple config files"""
    res = SpecConfig(path=[PATH, PATH_ADDITION])
    assert res.path_out == PATH_MODIFIED


def test_SpecConfig_extra_arg():
    """Test SpecConfig instantiation; extra argument."""
    with pytest.raises(ValidationError):
        SpecConfig(non_existing=PATH)
