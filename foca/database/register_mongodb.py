"""Register MongoDB with a Flask app."""

import os

import logging

from connexion import App
from flask_pymongo import PyMongo
from typing import Mapping


# Get logger instance
logger = logging.getLogger(__name__)


def register_mongodb(app: App) -> App:
    """
    Instantiates a MongoDB database, initializes collections and adds the
    database and collections to a Connexion app.

    Args:
        app: Connexion app.

    Returns:
        Flask application with updated config: `config['database']['database']`
            contains the database object; `config['database']['collections']`
            contains a dictionary of collection objects.
    """
    config = app.config

    # Instantiate PyMongo client
    mongo = create_mongo_client(
        app=app,
        config=config,
    )

    # Add database
    # db = mongo.db[config['FOCA'].db.name]

    # Add database collections
    registered_collections = {}
    if config['FOCA'].db.collections is not None:
        for name, index_models in config['FOCA'].db.collections.items():
            registered_collections[name] = mongo.db[name]
            if index_models:
                registered_collections[name].create_indexes(index_models)
            logger.debug(f"Added database collection '{name}'.")
    else:
        registered_collections = None

    # Add database and collections to app config
    # config['FOCA'].db.database = db
    config['FOCA'].db.collections = registered_collections
    app.config = config

    return app


def create_mongo_client(
    app: App,
    config: Mapping,
) -> PyMongo:
    """Register MongoDB database with Flask app.

    Optionally, basic authorization can be set by environment variables.

    Args:
        app: Flask application.
        config: Mapping of configuration parameters with a key `database` and
            a nested mapping of database configuration parameters, with at
            least the keys `host`, `port` and database `name`.

    Returns:
        MongoDB client.
    """
    if os.environ.get('MONGO_USERNAME') != '':
        auth = '{username}:{password}@'.format(
            username=os.environ.get('MONGO_USERNAME'),
            password=os.environ.get('MONGO_PASSWORD'),
        )
    else:
        auth = ''

    app.config['MONGO_URI'] = 'mongodb://{auth}{host}:{port}/{dbname}'.format(
        host=os.environ.get('MONGO_HOST', config['FOCA'].db.host),
        port=os.environ.get('MONGO_PORT', config['FOCA'].db.port),
        dbname=os.environ.get('MONGO_DBNAME', config['FOCA'].db.name),
        auth=auth
    )

    mongo = PyMongo(app)
    logger.info(
        (
            "Registered database '{name}' at URI '{uri}':'{port}' with Flask "
            'application.'
        ).format(
            name=os.environ.get('MONGO_DBNAME', config['FOCA'].db.name),
            uri=os.environ.get('MONGO_HOST', config['FOCA'].db.host),
            port=os.environ.get('MONGO_PORT', config['FOCA'].db.port)
        )
    )
    return mongo
