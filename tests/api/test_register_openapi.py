"""Tests for the registration of OpenAPI specifications with a Connexion app
instance.
"""

from copy import deepcopy
from pathlib import Path

from connexion import App
from connexion.exceptions import InvalidSpecification
import pytest
from yaml import YAMLError

from foca.api.register_openapi import register_openapi
from foca.models.config import SpecConfig

# Define mock data
DIR = Path(__file__).parent.parent / "test_files"
PATH_SPECS_2_YAML_ORIGINAL = str(DIR / "openapi_2_petstore.original.yaml")
PATH_SPECS_2_YAML_MODIFIED = str(DIR / "openapi_2_petstore.modified.yaml")
PATH_SPECS_2_JSON_ORIGINAL = str(DIR / "openapi_2_petstore.original.json")
PATH_SPECS_2_YAML_ADDITION = str(DIR / "openapi_2_petstore.addition.yaml")
PATH_SPECS_3_YAML_ORIGINAL = str(DIR / "openapi_3_petstore.original.yaml")
PATH_SPECS_3_YAML_MODIFIED = str(DIR / "openapi_3_petstore.modified.yaml")
PATH_SPECS_INVALID_JSON = str(DIR / "invalid.json")
PATH_SPECS_INVALID_YAML = str(DIR / "invalid.openapi.yaml")
PATH_NOT_FOUND = str(DIR / "does/not/exist.yaml")
OPERATION_FIELDS_2 = {"x-swagger-router-controller": "controllers"}
OPERATION_FIELDS_2_NO_RESOLVE = {"x-swagger-router-controller": YAMLError}
OPERATION_FIELDS_3 = {"x-openapi-router-controller": "controllers"}
SECURITY_FIELDS_2 = {"x-apikeyInfoFunc": "controllers.validate_token"}
SECURITY_FIELDS_3 = {"x-bearerInfoFunc": "controllers.validate_token"}
APPEND = {
    "info": {
        "version": "1.0.0",
        "title": "Swagger Petstore",
        "license": {
            "name": "MIT"
        }
    }
}
CONNEXION_CONFIG = {
    "strict_validation": True,
    "validate_responses": False,
    "options": {
        "swagger_ui": True,
        "serve_spec": True,
    }
}
SPEC_CONFIG_2 = {
    "path": PATH_SPECS_2_YAML_ORIGINAL,
    "path_out": PATH_SPECS_2_YAML_MODIFIED,
    "append": [APPEND],
    "add_operation_fields": OPERATION_FIELDS_2,
    "add_security_fields": SECURITY_FIELDS_2,
    "disable_auth": False,
    "connexion": CONNEXION_CONFIG,
}
SPEC_CONFIG_3 = {
    "path": PATH_SPECS_3_YAML_ORIGINAL,
    "path_out": PATH_SPECS_3_YAML_MODIFIED,
    "append": [APPEND],
    "add_operation_fields": OPERATION_FIELDS_3,
    "add_security_fields": SECURITY_FIELDS_3,
    "disable_auth": False,
    "connexion": CONNEXION_CONFIG,
}
SPEC_CONFIG_2_JSON = deepcopy(SPEC_CONFIG_2)
SPEC_CONFIG_2_JSON['path'] = PATH_SPECS_2_JSON_ORIGINAL
SPEC_CONFIG_2_LIST = deepcopy(SPEC_CONFIG_2)
SPEC_CONFIG_2_LIST['path'] = [PATH_SPECS_2_YAML_ORIGINAL]
SPEC_CONFIG_2_MULTI = deepcopy(SPEC_CONFIG_2_LIST)
SPEC_CONFIG_2_MULTI['path'].append(PATH_SPECS_2_YAML_ADDITION)
SPEC_CONFIG_2_DISABLE_AUTH = deepcopy(SPEC_CONFIG_2)
SPEC_CONFIG_2_DISABLE_AUTH['disable_auth'] = True
SPEC_CONFIG_3_DISABLE_AUTH = deepcopy(SPEC_CONFIG_3)
SPEC_CONFIG_3_DISABLE_AUTH['disable_auth'] = True


class TestRegisterOpenAPI:

    def test_openapi_2_yaml(self):
        """Successfully register OpenAPI 2 YAML specs with Connexion app."""
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_openapi_3_yaml(self):
        """Successfully register OpenAPI 3 YAML specs with Connexion app."""
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_3)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_openapi_2_json(self):
        """Successfully register OpenAPI 2 JSON specs with Connexion app."""
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_JSON)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_openapi_2_json_and_3_yaml(self):
        """Successfully register both OpenAPI2 JSON and OpenAPI3 YAML specs
        with Connexion app.
        """
        app = App(__name__)
        spec_configs = [
            SpecConfig(**SPEC_CONFIG_2_JSON),
            SpecConfig(**SPEC_CONFIG_3),
        ]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_openapi_2_invalid(self):
        """Registration failing because of invalid OpenAPI 2 spec file."""
        app = App(__name__)
        spec_configs = [SpecConfig(path=PATH_SPECS_INVALID_YAML)]
        with pytest.raises(InvalidSpecification):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_2_json_invalid(self):
        """Registration failing because of invalid JSON spec file."""
        app = App(__name__)
        spec_configs = [SpecConfig(path=PATH_SPECS_INVALID_JSON)]
        with pytest.raises(YAMLError):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_not_found(self):
        """Registration failing because spec file is unavailable."""
        app = App(__name__)
        spec_configs = [SpecConfig(path=PATH_NOT_FOUND)]
        with pytest.raises(OSError):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_2_list(self):
        """Successfully register OpenAPI 2 JSON specs with Connexion app;
        specs provided as list.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_LIST)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_openapi_2_fragments(self):
        """Successfully register OpenAPI 2 JSON specs with Connexion app;
        specs provided as multiple fragments.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_MULTI)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_register_openapi_spec_cannot_serialize(self):
        """Registration failing because modified specs cannot be serialized."""
        app = App(__name__)
        spec_configs = [SpecConfig(
            path=PATH_SPECS_2_YAML_ORIGINAL,
            path_out=PATH_SPECS_2_YAML_MODIFIED,
            add_operation_fields=OPERATION_FIELDS_2_NO_RESOLVE,
        )]
        with pytest.raises(YAMLError):
            register_openapi(app=app, specs=spec_configs)

    def test_register_openapi_spec_cannot_write(self):
        """Registration failing because modified specs cannot be written."""
        app = App(__name__)
        spec_configs = [SpecConfig(
            path=PATH_SPECS_2_YAML_ORIGINAL,
            path_out=PATH_NOT_FOUND,
        )]
        with pytest.raises(OSError):
            register_openapi(app=app, specs=spec_configs)

    def test_openapi_2_yaml_no_auth(self):
        """Successfully register OpenAPI 2 YAML specs with Connexion app;
        no security definitions/fields.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_2_DISABLE_AUTH)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)

    def test_openapi_3_yaml_no_auth(self):
        """Successfully register OpenAPI 3 YAML specs with Connexion app;
        no security schemes/fields.
        """
        app = App(__name__)
        spec_configs = [SpecConfig(**SPEC_CONFIG_3_DISABLE_AUTH)]
        res = register_openapi(app=app, specs=spec_configs)
        assert isinstance(res, App)
