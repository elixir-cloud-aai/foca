"""Methods to manage permission management configuration"""

import logging
import casbin_pymongo_adapter
from pkg_resources import resource_filename
from connexion import App
from flask_authz import CasbinEnforcer

from foca.models.config import Config, DBConfig, SpecConfig

logger = logging.getLogger(__name__)


def _create_permission_config(
    config: Config
) -> Config:
    """Check and update FOCA configuration for permission management.

    Args:
        config: Application configuration.

    Returns:
        Updated application configuration.
    """
    if config.access is not None:
        if config.access.enable:
            # Add index for permission management
            if config.db is not None:
                if config.db.dbs is not None:
                    config.db.dbs = {
                        'access_db': DBConfig(collections=None, client=None)
                    }
                else:
                    # TODO: add check so user cannot enter access_db as the db name.
                    config.db.dbs['access_db'] = DBConfig(collections=None, client=None)

    return config


def _register_permission_specs(
    app: App
):
    spec = SpecConfig(
        path=str(resource_filename("foca.permission_management", "api/permission-endpoint-specs.yaml")),
        add_operation_fields={
            "x-openapi-router-controller": "foca.permission_management.permission_server"
        },
        connexion={
            "strict_validation": True,
            "validate_responses": False,
            "options": {
                "swagger_ui": True,
                "serve_spec": True
            }
        }
    )
    
    app.add_api(
        specification=spec.path[0],
        **spec.dict().get('connexion', {}),
    )
    return app


def _register_casbin_enforcer(
    app: App,
    policy_path: str,
    owner_headers: set,
    user_headers: set,
    db_name: str,
    db_host: str,
    db_port: int
) -> App:
    # Set up Casbin model config
    app.app.config['CASBIN_MODEL'] = policy_path
    # Set headers where owner for enforcement policy should be located
    app.app.config['CASBIN_OWNER_HEADERS'] = owner_headers
    # Set headers for user name
    app.app.config['CASBIN_USER_NAME_HEADERS'] = user_headers
    # Set up Casbin Adapter
    adapter = casbin_pymongo_adapter.Adapter(f"mongodb://{db_host}:{db_port}/", db_name)
    casbin_enforcer = CasbinEnforcer(app.app, adapter)
    app.app.config['casbin_enforcer'] = casbin_enforcer
    return app