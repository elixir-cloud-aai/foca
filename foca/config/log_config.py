"""Function for configuring logging."""

import logging
import os
from logging.config import dictConfig
from typing import Optional

from foca.config.config_parser import YAMLConfigParser


# Get logger instance
logger = logging.getLogger(__name__)


def configure_logging(
    config_var: Optional[str] = None,
    default_path: str = os.path.abspath(
        os.path.join(
            os.path.dirname(
                os.path.realpath(__file__)
            ),
            'log_config.yaml'
        )
    ),
    fallback_level: int = logging.DEBUG
) -> None:
    """Configures base logger."""
    # Create parser instance
    config = YAMLConfigParser()

    # Get config from variable if defined
    if config_var and os.environ.get(config_var):
        paths = config.update_from_yaml(
            config_vars=[config_var],
        )
        dictConfig(config)

    # Otherwise get config from default config file
    else:
        try:
            paths = config.update_from_yaml(
                config_paths=[default_path],
                config_vars=[config_var],
            )
            dictConfig(config)

        # Fall back to logging default if default config file is inaccessible/
        # not found
        except (FileNotFoundError, PermissionError):
            logger.warning((
                'No custom logging config found. Falling back to default '
                'config.'
            ))
            logging.basicConfig(level=fallback_level)

        else:
            logger.info("Logging config loaded from '{paths}'.".format(
                paths=paths,
            ))
