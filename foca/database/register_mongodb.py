"""Register MongoDB database and collections."""

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
    """Register MongoDB databases and collections with Flask application
    instance.

    Args:
        app: Flask application instance.
        conf: :py:class:`foca.models.config.MongoConfig` instance describing
            databases and collections to be registered with `app`.

    Returns:
        Flask application instance with registered MongoDB databases and
        collections.
    """
    # Iterate over databases
    if conf.dbs is not None:
        for db_name, db_conf in conf.dbs.items():

            # Instantiate PyMongo client
            mongo = _create_mongo_client(
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

                    # Add indexes
                    if coll_conf.indexes is not None:
                        # Remove already created indexes if any
                        coll_conf.client.drop_indexes()
                        for index in coll_conf.indexes:
                            if index.keys is not None:
                                coll_conf.client.create_index(
                                    index.keys, **index.options)
                        logger.info(
                            f"Indexes created for collection '{coll_name}'."
                        )

    return conf


def _create_mongo_client(
        app: Flask,
        host: str = 'mongodb',
        port: int = 27017,
        db: str = 'database',
) -> PyMongo:
    """Create MongoDB client for Flask application instance.

    Args:
        app: Flask application instance.
        host: Host at which MongoDB database is exposed.
        port: Port at which MongoDB database is exposed.
        db: Name of database to be accessed/created.

    Returns:
        MongoDB client for Flask application instance.
    """
    auth = ''
    user = os.environ.get('MONGO_USERNAME')
    if user is not None and user != "":
        auth = '{username}:{password}@'.format(
            username=os.environ.get('MONGO_USERNAME'),
            password=os.environ.get('MONGO_PASSWORD'),
        )

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
