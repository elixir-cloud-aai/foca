"""Parser for YAML-based app configuration."""

import logging
from logging.config import dictConfig
from typing import (Dict, Optional)

import yaml

from foca.models.config import (Config, LogConfig)

logger = logging.getLogger(__name__)


class ConfigParser():
    """Parser for FOCA config files.

    Args:
        config_file: Path to config file in YAML format.
        format_logs: Whether log formatting should be configured.

    Attributes:
        config_file: Path to config file in YAML format.
        format_logs: Whether log formatting should be configured.
    """

    def __init__(
        self,
        config_file: Optional[str] = None,
        format_logs: bool = True
    ) -> None:
        """Constructor method."""
        if config_file:
            self.config = Config(**self.parse_yaml(config_file))
        else:
            self.config = Config()
        if format_logs:
            self.configure_logging()
        logger.debug(f"Parsed config: {self.config.dict(by_alias=True)}")

    def configure_logging(self) -> None:
        """Configure logging."""
        try:
            dictConfig(self.config.log.dict(by_alias=True))
        except Exception as e:
            dictConfig(LogConfig().dict(by_alias=True))
            logger.warning(
                f"Failed to configure logging. Falling back to default "
                f"settings. Original error: {type(e).__name__}: {e}"
            )

    @staticmethod
    def parse_yaml(conf: str) -> Dict:
        """Load YAML file.

        Args:
            conf: Path to YAML file.

        Returns:
            Dictionary of YAML file contents.

        Raises:
            OSError: file cannot be accessed.
            yaml.YAMLError: file cannot not be parsed.
        """
        try:
            with open(conf) as config_file:
                try:
                    return yaml.safe_load(config_file)
                except yaml.YAMLError:
                    raise yaml.YAMLError(
                        f"file '{conf}' is not valid YAML"
                    )
        except OSError as e:
            raise OSError(
                f"file '{conf}' could not be read"
            ) from e
