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
                logger.info(f"Permission db registered.")

    return config


def _register_permission_specs(
    app: App
):
    """Register open api specs for permission management.

    Args:
        app: Connexion app.

    Returns:
        Connexion app with updated permission specifications.
    """
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
    """Method to add casbin permission enforcer.

    Args:
        app: Connexion app.
        policy_path: Casbin user policy file path.
        owner_headers: Owner headers identifiers.
        user_headers: User headers identifiers.
        db_name: Permission db name.
        db_host: Permission db host.
        db_port: Permission db port.

    Returns:
        Update app with casbin configuration.
    """
    logger.info("Setting casbin policies.")
    app.app.config['CASBIN_MODEL'] = policy_path
    
    logger.info("Setting headers where owner for enforcement policy should be located.")
    app.app.config['CASBIN_OWNER_HEADERS'] = owner_headers
    
    logger.info("Setting headers for user name.")
    app.app.config['CASBIN_USER_NAME_HEADERS'] = user_headers
    
    logger.info("Setting up casbin enforcer.")
    adapter = casbin_pymongo_adapter.Adapter(f"mongodb://{db_host}:{db_port}/", db_name)
    casbin_enforcer = CasbinEnforcer(app.app, adapter)
    
    app.app.config['casbin_enforcer'] = casbin_enforcer
    return app