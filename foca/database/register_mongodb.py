"""Register MongoDB with a Flask app."""

import os

import logging

from connexion import App
from flask_pymongo import PyMongo
from foca.models.config import DBConfig

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
    conf = app.config['FOCA'].db

    # Instantiate PyMongo client
    mongo = create_mongo_client(
        app=app,
        conf=conf,
    )

    # Add database
    db = mongo.db[conf.name]

    # Add database collections
    registered_collections = {}
    if conf.collections is not None:
        for name, index_models in conf.collections.items():
            registered_collections[name] = mongo.db[name]
            if index_models.key is not None:
                registered_collections[name].create_indexes(index_models.key,
                                                            index_models
                                                            .dict().pop("key"))
            logger.debug(f"Added database collection '{name}'.")
    else:
        registered_collections = None

    # Add database and collections to app config
    conf.database = db
    if registered_collections is not None:
        for coll_name, reg_coll in registered_collections.items():
            conf.collections[coll_name].connection = reg_coll

    app.config["FOCA"].db = conf

    return app


def create_mongo_client(
        app: App,
        conf: DBConfig,
) -> PyMongo:
    """Register MongoDB database with Flask app.

    Optionally, basic authorization can be set by environment variables.

    Args:
        app: Flask application.
        conf: An instance of the DBConfig class, with at least the attributes
        `host`, `port` and database `name`. For further details, see
        :py:class:`foca.models.config.DBConfig()`

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
        host=os.environ.get('MONGO_HOST', conf.host),
        port=os.environ.get('MONGO_PORT', conf.port),
        dbname=os.environ.get('MONGO_DBNAME', conf.name),
        auth=auth
    )

    mongo = PyMongo(app)
    logger.info(
        (
            "Registered database '{name}' at URI '{uri}':'{port}' with Flask "
            'application.'
        ).format(
            name=os.environ.get('MONGO_DBNAME', conf.name),
            uri=os.environ.get('MONGO_HOST', conf.host),
            port=os.environ.get('MONGO_PORT', conf.port)
        )
    )
    return mongo
