"""Parser for YAML-based app configuration."""

import yaml
from typing import Dict

from foca.models.config import Config


class ConfigParser():
    """Parser for FOCA config files.

    Args:
        config_file: Path to config file in YAML format.

    Attributes:
        config_file: Path to config file in YAML format.
    """

    def __init__(self, config_file) -> None:
        """Constructor method."""
        self.config = Config(**self.parse_yaml(config_file))

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
