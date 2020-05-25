import os

import logging
from typing import Dict

from flask import Flask
from flask_pymongo import PyMongo

from foca.config.config_parser import get_conf

# Get logger instance
logger = logging.getLogger(__name__)


def create_mongo_client(
    app: Flask,
    config: Dict,
):
    """Register MongoDB uri and credentials."""
    if os.environ.get('MONGO_USERNAME') != '':
        auth = '{username}:{password}@'.format(
            username=os.environ.get('MONGO_USERNAME'),
            password=os.environ.get('MONGO_PASSWORD'),
        )
    else:
        auth = ''

    app.config['MONGO_URI'] = 'mongodb://{auth}{host}:{port}/{dbname}'.format(
        host=os.environ.get('MONGO_HOST', get_conf(
            config, 'database', 'host')),
        port=os.environ.get('MONGO_PORT', get_conf(
            config, 'database', 'port')),
        dbname=os.environ.get('MONGO_DBNAME', get_conf(
            config, 'database', 'name')),
        auth=auth
    )

    """Instantiate MongoDB client."""
    mongo = PyMongo(app)
    logger.info(
        (
            "Registered database '{name}' at URI '{uri}':'{port}' with Flask "
            'application.'
        ).format(
            name=os.environ.get('MONGO_DBNAME', get_conf(
                config, 'database', 'name')),
            uri=os.environ.get('MONGO_HOST', get_conf(
                config, 'database', 'host')),
            port=os.environ.get('MONGO_PORT', get_conf(
                config, 'database', 'port'))
        )
    )
    return mongo
