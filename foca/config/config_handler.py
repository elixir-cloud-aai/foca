import logging
from typing import (Dict)

# Get logger instance
logger = logging.getLogger(__name__)


class Config(Dict):
    """ Main Config class """

    def __init__(self, config: Dict) -> None:
        self.config = config.get('foca')
        self._config = config.get('foca')

    def get_property(
        self,
        property_name,
    ):
        try:
            self._config = self._config.get(property_name)
        except (AttributeError, TypeError, KeyError, ValueError):
            logger.exception(
                (
                    'Invalid Config file.'
                    'Property {property_name} does not exist'
                ).format(
                    property_name=property_name
                )
            )
            raise InvalidConfig
        else:
            return self

    @property
    def value(self):
        value = self._config
        self._config = self.config
        return value

    @property
    def database(self):
        return self.get_property('database')

    @property
    def specs(self):
        return self.get_property('specs')

    @property
    def errors(self):
        return self.get_property('errors')

    @property
    def celery(self):
        return self.get_property('celery')

    @property
    def log(self):
        return self.get_property('log')

    @property
    def security(self):
        return self.get_property('security')

    @property
    def server(self):
        return self.get_property('server')

    @property
    def host(self):
        return self.get_property('host')

    @property
    def port(self):
        return self.get_property('port')

    @property
    def name(self):
        return self.get_property('name')


class InvalidConfig(Exception):
    """Raised when the Config file has errors"""
    pass
