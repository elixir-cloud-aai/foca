import os

import logging
from typing import (Iterable, Mapping)

from flask import Flask
from flask_pymongo import PyMongo
from pymongo.operations import IndexModel

from foca.config.config_parser import get_conf

# Get logger instance
logger = logging.getLogger(__name__)


def register_mongodb(
    app: Flask,
    db: str = "db",
    collections: Mapping[str, Iterable[IndexModel]] = {},
) -> Flask:
    """
    Instantiates a MongoDB database, initializes collections and adds the
    database and collections to a Flask app.

    Collections can optionally be registered with custom indexes.

    :param db: name of database to be registered
    :param collections: mappings of collections to be registered; keys will be
            used as collection names, values are passed to
            `flask_pymongo.wrappers.Collection.create_indexes`

    :returns: Flask app object with updated config;
            config['database']['database'] contains the database object;
            config['database']['collections'] contains a dictionary of
            collection objects
    """
    config = app.config

    # Instantiante PyMongo client
    mongo = create_mongo_client(
        app=app,
        config=config,
    )

    # Add database
    db = mongo.db[db]

    # Add database collections
    registered_collections = {}
    for name, index_models in collections.items():
        registered_collections[name] = mongo.db[name]
        if index_models:
            registered_collections[name].create_indexes(index_models)
        logger.debug(f"Added database collection '{name}'.")

    # Add database and collections to app config
    config['database']['database'] = db
    config['database']['collections'] = registered_collections
    app.config = config

    return app


def create_mongo_client(
    app: Flask,
    config: Mapping,
):
    """Register MongoDB database with Flask app."""
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
