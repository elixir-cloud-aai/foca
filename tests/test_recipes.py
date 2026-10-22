"""Tests for configuration recipes of the documentation.

Recipe "require login for all reads except `/service-info`": an operator
overlay patches the app's API specification and adds a policy rule, without
restating the app's specs or policies.
"""

from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml
from flask import Flask

from foca import Foca
from foca.security.access_control import register_access_control
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.auth import _unauthorized
from tests.mock_data import MongoClients

ISSUER = "https://login.example.org/oidc/"
THIS_MODULE = __name__
TOKENS = {"dave": {"sub": "dave", "iss": ISSUER}}


def mock_token_info(token: str) -> dict[str, Any]:
    """Return token info or raise as FOCA's `validate_token()` does."""
    if token not in TOKENS:
        raise _unauthorized("JWT could not be validated")
    return {"jwt": token, "claims": TOKENS[token]}


def controller(*args: Any, **kwargs: Any) -> str:
    """Generic controller for the test API."""
    return "ok"


# Operations need distinct identifiers
get_service_info = list_items = create_item = get_item = controller


def _operation(operation_id: str, **extra: Any) -> dict[str, Any]:
    """Build an OpenAPI 3 Operation object."""
    return {
        "operationId": f"{THIS_MODULE}.{operation_id}",
        "responses": {
            "200": {
                "description": "OK",
                "content": {"application/json": {"schema": {"type": "string"}}},
            },
        },
        **extra,
    }


# App spec: reads are public, writes need a token
SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Recipe API", "version": "1.0.0"},
    "servers": [{"url": "/api"}],
    "security": [{"bearerAuth": []}],
    "paths": {
        "/service-info": {"get": _operation("get_service_info", security=[])},
        "/items": {
            "get": _operation("list_items", security=[]),
            "post": _operation("create_item"),
        },
        "/items/{id}": {
            "get": _operation(
                "get_item",
                security=[],
                parameters=[
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            ),
        },
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "x-bearerInfoFunc": f"{THIS_MODULE}.mock_token_info",
            },
        },
    },
}

# App config: named spec and policies
CONFIG = {
    "api": {"specs": {"items": {"path": "spec.yaml"}}},
    "security": {
        "auth": {"issuers": [ISSUER], "audience": ["foca"]},
        "access_control": {
            "enabled": True,
            "enforce": True,
            "model": str(
                register_access_control.files(ACCESS_CONTROL_BASE_PATH).joinpath(
                    RESOURCE_ROLE_MODEL_FILE
                )
            ),
            "policies": {"create-items": ["p", "*", "/api/items", "POST"]},
        },
    },
}

# Operator overlay of the recipe
OVERLAY = """
api:
  specs:
    items:
      patches:
        - paths:
            /items:
              get:
                security:
                  - bearerAuth: []
            /items/{id}:
              get:
                security:
                  - bearerAuth: []
security:
  access_control:
    policies:
      read-api: [p, "*", "/api/*", GET]
"""


def _create_app(tmp_path: Path, overlay: bool) -> Flask:
    """Create the app, with or without the operator overlay."""
    (tmp_path / "spec.yaml").write_text(yaml.safe_dump(SPEC))
    config_files = [tmp_path / "config.yaml"]
    config_files[0].write_text(yaml.safe_dump(CONFIG))
    if overlay:
        config_files.append(tmp_path / "overlay.yaml")
        config_files[1].write_text(OVERLAY)
    with mock.patch("foca.security.auth.validate_token", new=mock_token_info):
        return Foca(config_file=config_files).create_app().app


@pytest.mark.parametrize(
    "overlay,method,path,user,status",
    [
        # app defaults: reads are public
        (False, "get", "/api/items", None, 200),
        (False, "get", "/api/items/1", None, 200),
        (False, "post", "/api/items", None, 401),
        # recipe: reads need a token, except for service info
        (True, "get", "/api/service-info", None, 200),
        (True, "get", "/api/items", None, 401),
        (True, "get", "/api/items/1", None, 401),
        # any authenticated user may read
        (True, "get", "/api/items", "dave", 200),
        (True, "get", "/api/items/1", "dave", 200),
        # the app's policy rules are kept
        (True, "post", "/api/items", "dave", 200),
        # the rule is limited to the app's API, i.e., not the permissions API
        (True, "get", "/admin/access-control/permissions", "dave", 403),
    ],
)
def test_require_login_for_reads(
    mongo_clients: MongoClients,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overlay: bool,
    method: str,
    path: str,
    user: str | None,
    status: int,
) -> None:
    """Recipe: require login for all reads except `/service-info`."""
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    app = _create_app(tmp_path, overlay)
    headers = {} if user is None else {"Authorization": f"Bearer {user}"}
    response = getattr(app.test_client(), method)(path, headers=headers)
    assert response.status_code == status
