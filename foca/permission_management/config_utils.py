"""Methods to manage permission management configuration"""
from foca.models.config import Config, DBConfig

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
        # if config.api.specs:

    return config
