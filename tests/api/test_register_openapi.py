"""Tests for `register_openapi.py` module.
"""
import pathlib
import pytest

from connexion import App
from connexion.exceptions import InvalidSpecification
from pydantic import ValidationError
from yaml import YAMLError

from foca.api.register_openapi import register_openapi
from foca.models.config import SpecConfig

DIR = pathlib.Path(__file__).parent.parent / "test_files"
PATH_SPECS_2_YAML = str(DIR / "openapi_2_petstore.yaml")
PATH_SPECS_2_YAML_MODIFIED = str(DIR / "openapi_2_petstore.modified.yaml")
PATH_SPECS_2_JSON = str(DIR / "openapi_2_petstore.yaml")
PATH_ADD_SPECS_2_YAML = str(DIR / "openapi_add.yaml")
PATH_SPECS_3_YAML = str(DIR / "openapi_3_petstore.yaml")
PATH_SPECS_3_YAML_MODIFIED = str(DIR / "openapi_3_petstore.modified.yaml")
PATH_SPECS_INVALID_JSON = str(DIR / "invalid.json")
PATH_SPECS_INVALID_OPENAPI = str(DIR / "invalid_openapi_2.yaml")
PATH_SPECS_NOT_EXIST = str(DIR / "does/not/exist.yaml")
OPERATION_FIELDS = {"x-swagger-router-controller": "controllers"}
OPERATION_FIELDS_NOT_SERIALIZABLE = {
    "x-swagger-router-controller": InvalidSpecification
}
SPEC_CONFIG = {
    "path": PATH_SPECS_2_YAML,
    "path_out": PATH_SPECS_2_YAML_MODIFIED,
    "append": [
        {
            "securityDefinitions": {
                "jwt": {
                    "type": "apiKey",
                    "name": "Authorization",
                    "in": "header",
                }
            }
        },
    ],
    "add_operation_fields": OPERATION_FIELDS,
    "connexion": {
        "strict_validation": True,
        "validate_responses": False,
        "options": {
            "swagger_ui": True,
            "swagger_json": True,
        }
    }
}

SPEC_CONFIG_2 = {
    "path": [PATH_SPECS_2_YAML, PATH_ADD_SPECS_2_YAML],
    "path_out": PATH_SPECS_2_YAML_MODIFIED,
    "append": [
        {
            "securityDefinitions": {
                "jwt": {
                    "type": "apiKey",
                    "name": "Authorization",
                    "in": "header",
                }
            }
        },
    ],
    "add_operation_fields": OPERATION_FIELDS,
    "connexion": {
        "strict_validation": True,
        "validate_responses": False,
        "options": {
            "swagger_ui": True,
            "swagger_json": True,
        }
    }
}


SPEC_CONFIG_3 = {
    "path": [PATH_SPECS_2_YAML],
    "path_out": PATH_SPECS_2_YAML_MODIFIED,
    "add_operation_fields": OPERATION_FIELDS,
    "connexion": {
        "strict_validation": True,
        "validate_responses": False,
        "options": {
            "swagger_ui": True,
            "swagger_json": True,
        }
    }
}


def test_SpecConfig_class():
    """Test SpecConfig instantiation; full example"""
    res = SpecConfig(**SPEC_CONFIG)
    assert res.path_out == SPEC_CONFIG['path_out']


def test_SpecConfig_class_minimal():
    """Test SpecConfig instantiation; minimal example"""
    res = SpecConfig(path=PATH_SPECS_2_YAML)
    assert res.path_out == PATH_SPECS_2_YAML_MODIFIED


def test_SpecConfig_class_extra_arg():
    """Test SpecConfig instantiation; extra argument."""
    with pytest.raises(ValidationError):
        SpecConfig(non_existing=PATH_SPECS_2_YAML)


def test_register_openapi_spec_yaml_2():
    """Successfully register OpenAPI specs with Connexion app."""
    app = App(__name__)
    spec_configs = [SpecConfig(**SPEC_CONFIG)]
    res = register_openapi(app=app, specs=spec_configs)
    assert isinstance(res, App)


def test_register_openapi_spec_yaml_2_modified():
    """Successfully register modified OpenAPI specs with Connexion app."""
    app = App(__name__)
    spec_configs = [SpecConfig(path=PATH_SPECS_2_YAML_MODIFIED)]
    res = register_openapi(app=app, specs=spec_configs)
    assert isinstance(res, App)


def test_register_openapi_spec_yaml_3_and_json_2():
    """Successfully register both OpenAPI2 and OpenAPI3 specs with Connexion
    app."""
    app = App(__name__)
    spec_configs = [
        SpecConfig(path=PATH_SPECS_3_YAML_MODIFIED),
        SpecConfig(
            path=PATH_SPECS_2_JSON,
            add_operation_fields=OPERATION_FIELDS,
        ),
    ]
    res = register_openapi(app=app, specs=spec_configs)
    assert isinstance(res, App)


def test_register_openapi_spec_invalid_yaml_json():
    """Registering OpenAPI specs fails because of invalid file format."""
    app = App(__name__)
    spec_configs = [SpecConfig(path=PATH_SPECS_INVALID_JSON)]
    with pytest.raises(YAMLError):
        register_openapi(app=app, specs=spec_configs)


def test_register_openapi_spec_file_not_found():
    """Registering OpenAPI specs fails because the spec file is not available.
    """
    app = App(__name__)
    spec_configs = [SpecConfig(path=PATH_SPECS_NOT_EXIST)]
    with pytest.raises(OSError):
        register_openapi(app=app, specs=spec_configs)


def test_register_openapi_spec_no_operation_id():
    """Registering OpenAPI specs fails because of invalid OpenAPI format"""
    app = App(__name__)
    spec_configs = [SpecConfig(
        path=PATH_SPECS_INVALID_OPENAPI,
        add_operation_fields=OPERATION_FIELDS,
    )]
    with pytest.raises(InvalidSpecification):
        register_openapi(app=app, specs=spec_configs)


def test_register_openapi_spec_cannot_serialize():
    """Registering OpenAPI specs fails because the modified specs cannot be
    serialized."""
    app = App(__name__)
    app = App(__name__)
    spec_configs = [SpecConfig(
        path=PATH_SPECS_2_YAML_MODIFIED,
        add_operation_fields=OPERATION_FIELDS_NOT_SERIALIZABLE,
    )]
    with pytest.raises(YAMLError):
        register_openapi(app=app, specs=spec_configs)


def test_register_openapi_spec_cannot_write():
    """Registering OpenAPI specs fails because modified specs cannot be
    written.
    """
    app = App(__name__)
    spec_configs = [SpecConfig(
        path=PATH_SPECS_2_YAML,
        path_out=PATH_SPECS_NOT_EXIST,
        add_operation_fields=OPERATION_FIELDS,
    )]
    with pytest.raises(OSError):
        register_openapi(app=app, specs=spec_configs)


def test_register_two_openapi_spec_yaml_2():
    """Successfully register two or more OpenAPI specs with Connexion app."""
    app = App(__name__)
    spec_configs = [SpecConfig(**SPEC_CONFIG_2)]
    res = register_openapi(app=app, specs=spec_configs)
    assert isinstance(res, App)


def test_list_single_openapi_spec_yaml_2():
    """Successfully register a single openAPI spec in a list"""
    app = App(__name__)
    spec_configs = [SpecConfig(**SPEC_CONFIG_3)]
    res = register_openapi(app=app, specs=spec_configs)
    assert isinstance(res, App)
