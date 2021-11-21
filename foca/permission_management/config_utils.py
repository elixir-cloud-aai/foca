"""Methods to manage permission management configuration"""

import logging
from pkg_resources import resource_filename
from connexion import App

from foca import api
from foca.models.config import Config, DBConfig, SpecConfig

logger = logging.getLogger(__name__)


def _create_permission_config(config: Config) -> Config:
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
    logger.info(f"{resource_filename('foca.permission_management', 'api/permission-endpoint-specs.yaml')}")
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