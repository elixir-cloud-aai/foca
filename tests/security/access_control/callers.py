"""Callers and apps for tests of access control (and its users)."""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any

import mongomock
from flask import Flask
from flask.globals import request_ctx

from foca.models.config import Config
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter

ISSUER = "https://login.example.org/oidc/"
HEADER = "X-On-Behalf-Of"
ADMINS = "urn:example:group:admins"
TASK = "/tasks/1"
# Claims of validated tokens
ALICE = {"iss": ISSUER, "sub": "alice", "groups": ["devs"]}
BOB = {"iss": ISSUER, "sub": "bob", "groups": ["ops/team"]}
CAROL = {"iss": ISSUER, "sub": "carol", "groups": [ADMINS]}
DAVE = {"iss": ISSUER, "sub": "dave"}
CONQUEST = {"iss": ISSUER, "sub": "conquest", "client_id": "conquest"}
PROTES = {"iss": ISSUER, "sub": "protes", "client_id": "protes"}
NO_SUBJECT = {"iss": ISSUER}


def model_path(model_file: str = RESOURCE_ROLE_MODEL_FILE) -> str:
    """Get the path of a model file.

    Args:
        model_file: Absolute path, or name of a model file of FOCA.

    Returns:
        Path of the model file.
    """
    if Path(model_file).is_absolute():
        return model_file
    with resource_path(ACCESS_CONTROL_BASE_PATH, model_file) as model:
        return str(model)


def create_app(
    model_file: str = RESOURCE_ROLE_MODEL_FILE, **access_control: Any
) -> Flask:
    """Create app with service clients, groups and in-memory policies."""
    app = Flask(__name__)
    app.config.foca = Config.model_validate(  # type: ignore[attr-defined]  # dynamic attribute
        {
            "security": {
                "auth": {
                    "issuers": [ISSUER],
                    "audience": ["foca"],
                    "service_clients": [
                        {"client_id": "conquest", "on_behalf_of": True},
                        {"client_id": "protes"},
                    ],
                    "on_behalf_of_header": HEADER,
                },
                "access_control": {
                    "enabled": True,
                    "model": model_path(model_file),
                    "claim_groups": "groups",
                    "group_mapping": {ADMINS: "admin"},
                    "keep_group_subjects": True,
                    **access_control,
                },
            }
        }
    )
    app.config["CASBIN_MODEL"] = model_path(model_file)
    app.config["casbin_adapter"] = Adapter(
        collection=mongomock.MongoClient()["access_control_db"]["policy_rules"]
    )
    return app


@contextmanager
def as_caller(
    app: Flask,
    claims: dict[str, Any] | None,
    headers: dict[str, str] | None = None,
) -> Iterator[None]:
    """Request context with token info as set by Connexion."""
    with app.test_request_context("/tasks", headers=headers):
        request_ctx.connexion_context = (  # type: ignore[attr-defined]  # set by Connexion
            {} if claims is None else {"token_info": {"claims": claims}}
        )
        yield
