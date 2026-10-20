"""Tests for config.py."""

import doctest
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError
from werkzeug.exceptions import NotFound

import foca.models.config
from foca.models.config import (
    AccessControlConfig,
    AuthConfig,
    CollectionConfig,
    Config,
    DBConfig,
    ExceptionConfig,
    IndexConfig,
    MongoConfig,
    SecurityConfig,
    SpecConfig,
)

DIR = Path(__file__).parent / "test_files"
PATH = str(DIR / "openapi_2_petstore.yaml")
PATH_ADDITION = str(DIR / "openapi_2_petstore.addition.yaml")
INDEX_CONFIG = {
    "keys": {"last_name": -1},
    "options": {
        "name": "indexLastName",
        "unique": True,
        "background": False,
        "sparse": False,
    },
}
COLLECTION_CONFIG = {
    "indexes": [INDEX_CONFIG],
}
DB_CONFIG = {
    "collections": {
        "wes-col": COLLECTION_CONFIG,
    },
}
MONGO_CONFIG = {
    "host": "mongodb",
    "port": 27017,
    "dbs": {
        "wes": DB_CONFIG,
    },
}
SPEC_CONFIG = {
    "path": "/my/abs/path",
}
SPEC_CONFIG_REL = {
    "path": "path",
}
SPEC_CONFIG_LIST = {"path": ["path1", "/my/abs/path2"]}


def test_config_empty():
    """Test basic creation of the main config model."""
    res = Config()
    assert isinstance(res, Config)


class PetNotFound(NotFound):
    pass


PROBLEMS = {
    NotFound: {"title": "Not Found", "status": 404},
    PetNotFound: {"title": "Pet Not Found", "status": 404, "detail": "x"},
}


@pytest.fixture
def problems_module(monkeypatch):
    """Register module `problems_module` with mappings as attributes."""
    module = ModuleType("problems_module")
    module.valid = PROBLEMS
    module.not_dict = [NotFound]
    module.not_exception = {"a": {"title": "A", "status": 400}}
    module.not_class = {NotFound(): {"title": "A", "status": 400}}
    module.problem_not_dict = {NotFound: "Not Found"}
    module.no_status = {NotFound: {"title": "Not Found"}}
    module.status_str = {NotFound: {"title": "Not Found", "status": "404"}}
    module.status_bool = {NotFound: {"title": "Not Found", "status": True}}
    module.status_200 = {NotFound: {"title": "OK", "status": 200}}
    module.no_title = {NotFound: {"status": 404}}
    monkeypatch.setitem(sys.modules, "problems_module", module)
    return module


def test_exception_config_empty():
    """Test basic creation of the ExceptionConfig model."""
    res = ExceptionConfig()
    assert res.mapping is None
    assert res.problems == {}
    assert res.public_members is None
    assert res.media_type == "application/problem+json"


@pytest.mark.parametrize(
    "media_type",
    [
        "application/json",
        "application/problem+json",
        "application/vnd.example.error+json",
        "Application/JSON",
    ],
)
def test_exception_config_media_type(media_type):
    """Test creation of the ExceptionConfig model with a media type."""
    res = ExceptionConfig(media_type=media_type)
    assert res.media_type == media_type.lower()


@pytest.mark.parametrize(
    "media_type",
    [
        "",
        "json",
        "application/",
        "/json",
        "application/xml",
        "application/problem+xml",
        "text/plain",
        "application/jsonx",
        "application/json; charset=utf-8",
        "application/json\nX-Injected: 1",
        " application/json",
        "application/x+y/json",
        1,
    ],
)
def test_exception_config_invalid_media_type(media_type):
    """Test creation of the ExceptionConfig model with invalid media types."""
    with pytest.raises(ValidationError, match="media_type"):
        ExceptionConfig(media_type=media_type)


def test_exception_config_mapping(problems_module):
    """Test creation of the ExceptionConfig model with a mapping."""
    res = ExceptionConfig(
        mapping="problems_module.valid",
        public_members=["title", "status"],
        logging="minimal",
    )
    assert res.problems is PROBLEMS
    assert res.public_members == ["title", "status"]


@pytest.mark.parametrize(
    "mapping",
    [
        "some.unavailable.module.problems",
        "problems_module.unavailable",
        "problems",
        "problems_module.not_dict",
        "problems_module.not_exception",
        "problems_module.not_class",
        "problems_module.problem_not_dict",
        "problems_module.no_status",
        "problems_module.status_str",
        "problems_module.status_bool",
        "problems_module.status_200",
        "problems_module.no_title",
    ],
)
def test_exception_config_invalid_mapping(problems_module, mapping):
    """Test creation of the ExceptionConfig model with invalid mappings."""
    with pytest.raises(ValidationError):
        ExceptionConfig(mapping=mapping)


@pytest.mark.parametrize(
    "field",
    [
        {"exceptions": "foca.errors.exceptions.exceptions"},
        {"required_members": [["title"], ["status"]]},
        {"status_member": ["status"]},
        {"private_members": [["detail"]]},
        {"public_members": [["title"]]},
    ],
)
def test_exception_config_removed_fields(field):
    """Test creation of the ExceptionConfig model with removed fields."""
    with pytest.raises(ValidationError):
        ExceptionConfig(**field)


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
    assert res.path == [Path(SPEC_CONFIG["path"])]


def test_spec_config_rel():
    """Test creation of the SpecConfig model with relative file path."""
    res = SpecConfig(**SPEC_CONFIG_REL)
    assert res.path == [Path(SPEC_CONFIG_REL["path"]).resolve()]


def test_spec_config_list():
    """Test creation of SpecConfig model with list of paths."""
    res = SpecConfig(**SPEC_CONFIG_LIST)
    assert all(Path(path).is_absolute() for path in res.path)
    assert res.path[1] == Path("/my/abs/path2")


def test_spec_config_no_path():
    """Test creation of the SpecConfig model without path."""
    with pytest.raises(ValidationError):
        SpecConfig()


def test_spec_config_path_out_removed():
    """Test creation of the SpecConfig model with removed `path_out`."""
    with pytest.raises(ValidationError):
        SpecConfig(path=PATH, path_out="/my/abs/out/path")


def test_SpecConfig_merge():
    """Test SpecConfig instantiation; multiple config files"""
    res = SpecConfig(path=[PATH, PATH_ADDITION])
    assert res.path == [Path(PATH), Path(PATH_ADDITION)]


def test_SpecConfig_extra_arg():
    """Test SpecConfig instantiation; extra argument."""
    with pytest.raises(ValidationError):
        SpecConfig(non_existing=PATH)


@pytest.mark.parametrize("prefix", ["user:", "u/", "user:group:", "groups:"])
def test_AccessControlConfig_subject_prefix(prefix):
    """Test AccessControlConfig instantiation; valid subject prefixes."""
    res = AccessControlConfig(subject_prefix=prefix)
    assert res.subject_prefix == prefix


@pytest.mark.parametrize("prefix", ["", "group:", "group:x", "g", "group"])
def test_AccessControlConfig_subject_prefix_invalid(prefix):
    """Test AccessControlConfig instantiation; subject prefixes that may
    collide with role names or group subjects."""
    with pytest.raises(ValidationError):
        AccessControlConfig(subject_prefix=prefix)


@pytest.mark.parametrize(
    "charset,length",
    [("abcdef", 8), ("0123456789", 6), ("0123456789", 64), ("a-._~", 9)],
)
def test_AccessControlConfig_ids(charset, length):
    """Test AccessControlConfig instantiation; valid identifier settings."""
    res = AccessControlConfig(id_charset=charset, id_length=length)
    assert (res.id_charset, res.id_length) == (charset, length)


@pytest.mark.parametrize(
    "charset,length",
    [
        ("", 6),
        ("ab/", 6),
        ("a b", 6),
        ("äb", 6),
        ("ab", 3),
        ("ab", 65),
        # too few different identifiers
        ("a", 64),
        ("aaaaaaaaaab", 19),
        ("0123456789", 5),
    ],
)
def test_AccessControlConfig_ids_invalid(charset, length):
    """Test AccessControlConfig instantiation; invalid identifier settings."""
    with pytest.raises(ValidationError):
        AccessControlConfig(id_charset=charset, id_length=length)


ISSUERS = ["https://login.example.org/oidc/"]


@pytest.mark.parametrize(
    "issuers,access_control",
    [
        # access control options without access control
        (ISSUERS, {"enforce": True}),
        (ISSUERS, {"api_specs": "specs.yaml"}),
        (ISSUERS, {"self_service": True}),
        (ISSUERS, {"policies": [["p", "*", "/pets", "GET"]]}),
        # access control without trusted issuers
        ([], {"enabled": True}),
        ([], {"enabled": True, "enforce": True}),
    ],
)
def test_SecurityConfig_access_control_invalid(issuers, access_control):
    """Test SecurityConfig instantiation; inconsistent access control."""
    with pytest.raises(ValidationError):
        SecurityConfig(
            auth=AuthConfig(issuers=issuers, audience=["my-app"]),
            access_control=AccessControlConfig(**access_control),
        )


@pytest.mark.parametrize(
    "issuers,access_control",
    [
        ([], {}),
        (ISSUERS, {}),
        (ISSUERS, {"enabled": True}),
        (
            ISSUERS,
            {
                "enabled": True,
                "enforce": True,
                "api_specs": "specs.yaml",
                "self_service": True,
                "policies": [["p", "*", "/pets", "GET"]],
            },
        ),
    ],
)
def test_SecurityConfig_access_control(issuers, access_control):
    """Test SecurityConfig instantiation; consistent access control."""
    res = SecurityConfig(
        auth=AuthConfig(issuers=issuers, audience=["my-app"]),
        access_control=AccessControlConfig(**access_control),
    )
    assert res.access_control.enabled is access_control.get("enabled", False)


def test_AuthConfig_required_removed():
    """Test AuthConfig instantiation; `required` is no longer supported."""
    with pytest.raises(ValidationError):
        AuthConfig(required=True)


def test_AccessControlConfig_default_model():
    """Test AccessControlConfig instantiation; default model."""
    model = Path(AccessControlConfig().model)
    assert model.name == "default_model.conf"
    assert model.is_file()


@pytest.mark.parametrize(
    "path", ["access_model.conf", "../../test/access_model.conf"]
)
def test_AccessControlConfig_relative_model(path):
    """Test AccessControlConfig instantiation; relative model paths are
    resolved against the working directory without config file."""
    assert AccessControlConfig(model=path).model == str(Path(path).resolve())


def test_AccessControlConfig_absolute_model():
    """Test AccessControlConfig instantiation; absolute model path."""
    path = "/test/access_model.conf"
    assert AccessControlConfig(model=path).model == path


def test_docstring_examples():
    """Test that the examples in the docstrings of the config models run."""
    results = doctest.testmod(foca.models.config)
    assert results.attempted > 0
    assert results.failed == 0
