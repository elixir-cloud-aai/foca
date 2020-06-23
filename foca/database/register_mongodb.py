"""Register MongoDB with a Flask app."""

import logging
import os

from flask import Flask
from flask_pymongo import PyMongo
from foca.models.config import MongoConfig

# Get logger instance
logger = logging.getLogger(__name__)


def register_mongodb(
    app: Flask,
    conf: MongoConfig,
) -> MongoConfig:
    """
    Instantiates a MongoDB database, initializes collections and adds the
    database and collections to a Flask app.

    Args:
        app: Flask application object.
        conf: MongoDB configuration object.

    Returns:
        Flask application with updated config: `config['database']['database']`
            contains the database object; `config['database']['collections']`
            contains a dictionary of collection objects.
    """
    # Iterate over databases
    if conf.dbs is not None:
        for db_name, db_conf in conf.dbs.items():

            # Instantiate PyMongo client
            mongo = create_mongo_client(
                app=app,
                host=conf.host,
                port=conf.port,
                db=db_name,
            )

            # Add database
            db_conf.client = mongo.db

            # Add collections
            if db_conf.collections is not None:
                for coll_name, coll_conf in db_conf.collections.items():

                    coll_conf.client = mongo.db[coll_name]
                    logger.info(
                        f"Added database collection '{coll_name}'."
                    )

                    # Add indices
                    if coll_conf.indexes is not None:
                        for index in coll_conf.indexes:
                            if index.keys is not None:
                                coll_conf.client.create_index(**index.dict())

    return conf


def create_mongo_client(
        app: Flask,
        host: str = 'mongodb',
        port: int = 27017,
        db: str = 'database',
) -> PyMongo:
    """Register MongoDB database with Flask app.

    Optionally, basic authorization can be set by environment variables.

    Args:
        app: Flask application object.
        host: Host at which the MongoDB database is exposed.
        port: Port at which the MongoDB database is exposed.
        db: Name of the database to be accessed/created.

    Returns:
        Client for the MongoDB database specified by `host`, `port` and `db`.
    """
    if os.environ.get('MONGO_USERNAME') != '':
        auth = '{username}:{password}@'.format(
            username=os.environ.get('MONGO_USERNAME'),
            password=os.environ.get('MONGO_PASSWORD'),
        )
    else:
        auth = ''

    app.config['MONGO_URI'] = 'mongodb://{auth}{host}:{port}/{db}'.format(
        host=os.environ.get('MONGO_HOST', host),
        port=os.environ.get('MONGO_PORT', port),
        db=os.environ.get('MONGO_DBNAME', db),
        auth=auth
    )

    mongo = PyMongo(app)
    logger.info(
        (
            "Registered database '{db}' at URI '{host}':'{port}' with Flask "
            'application.'
        ).format(
            db=os.environ.get('MONGO_DBNAME', db),
            host=os.environ.get('MONGO_HOST', host),
            port=os.environ.get('MONGO_PORT', port)
        )
    )
    return mongo
