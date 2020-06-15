import logging
from pydantic import BaseModel, ValidationError

# Get logger instance
logger = logging.getLogger(__name__)


class Config():
    """Config class for database config handling"""

    def __init__(self, config) -> None:
        """Initialise config structure"""
        self._config = config

    def get_property(
        self,
        property_name,
    ):
        """Extract config properties"""
        if property_name not in self._config.keys():
            logger.exception(
                (
                    'Invalid Config file.'
                    'Property {property_name} does not exist'
                ).format(
                    property_name=property_name
                )
            )
            raise InvalidConfig
        return self._config[property_name]

    @property
    def database(self):
        """Extract database config variables"""
        try:
            database_config = self.get_property('database')
            return dict(databaseConfig(**database_config))
        except ValidationError as e:
            logger.exception(
                'Invalid database config format, database fields missing.'
            )
            raise e


class databaseConfig(BaseModel):
    """Basic database class"""
    host: str
    port: int
    name: str


class InvalidConfig(Exception):
    """Raised when the Config file has errors"""
    pass
