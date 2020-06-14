import logging
from typing import (Dict)
from pydantic import BaseModel

# Get logger instance
logger = logging.getLogger(__name__)


config = {}  # input from foca


class Config(Dict):
    """ Main Config class """

    def __init__(self) -> None:
        self._config = config

    def get_property(
        self,
        property_name,
    ):
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
        database_config = self.get_property('database')
        return dict(databaseConfig(**database_config))

    @property
    def server(self):
        server_config = self.get_property('server')
        return dict(serverConfig(**server_config))


class databaseConfig(BaseModel):
    host: str
    port: int
    name: str


class serverConfig(BaseModel):
    host: str
    port: int
    debug: bool
    environment: str
    testing: bool
    use_reloader: bool


class InvalidConfig(Exception):
    """Raised when the Config file has errors"""
    pass
