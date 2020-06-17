import yaml
import logging
from typing import Dict

from foca.models.config import Config

# Get logger instance
logger = logging.getLogger(__name__)


class YAMLConfigParser():
    """Config class for database config handling"""

    def __init__(self, config) -> None:
        """Initialise config structure"""
        self.config = Config(**self.parse_yaml(config))

    @staticmethod
    def parse_yaml(conf: str) -> Dict:
        try:
            with open(conf) as config_file:
                return yaml.safe_load(config_file)
        except Exception as e:
            logger.error(
                "Invalid YAML file."
            )
            raise e
