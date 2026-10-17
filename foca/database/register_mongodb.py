"""Register MongoDB database and collections."""

import logging
import os
from urllib.parse import quote_plus, urlsplit, urlunsplit

from flask import Flask
from flask_pymongo import PyMongo

from foca.models.config import DBConfig, MongoConfig

# Get logger instance
logger = logging.getLogger(__name__)


def register_mongodb(
    app: Flask,
    conf: MongoConfig,
) -> MongoConfig:
    """Register MongoDB databases and collections with Flask app.

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
            if db_conf.collections is not None and db_conf.client is not None:
                for coll_name, coll_conf in db_conf.collections.items():
                    coll_conf.client = db_conf.client[coll_name]
                    logger.info(f"Added database collection '{coll_name}'.")

                    # Add indexes
                    if (
                        coll_conf.indexes is not None
                        and coll_conf.client is not None
                    ):
                        # Remove already created indexes if any
                        coll_conf.client.drop_indexes()
                        for index in coll_conf.indexes:
                            if index.keys is not None:
                                coll_conf.client.create_index(
                                    index.keys, **index.options
                                )
                        logger.info(
                            f"Indexes created for collection '{coll_name}'."
                        )

    return conf


def add_new_database(
    app: Flask, conf: MongoConfig, db_conf: DBConfig, db_name: str
):
    """Register an additional db to database config.

    Args:
        app: Flask application instance.
        conf: :py:class:`foca.models.config.MongoConfig` instance describing
            databases and collections to be registered with `app`.
        db_conf: :py:class:`foca.models.config.DBConfig` instance describing
            new databases configuration to be registered with `app`.
        db_name: Name of the database being added.
    """
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
    if db_conf.collections is not None and db_conf.client is not None:
        for coll_name, coll_conf in db_conf.collections.items():
            coll_conf.client = db_conf.client[coll_name]
            logger.info(f"Added database collection '{coll_name}'.")


def _create_mongo_client(
    app: Flask,
    host: str = "mongodb",
    port: int = 27017,
    db: str = "database",
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
    uri = get_mongo_uri(host=host, port=port, db=db)
    app.config["MONGO_URI"] = uri
    mongo = PyMongo(app)
    logger.info(
        f"Registered database '{os.environ.get('MONGO_DBNAME', db)}' at URI "
        f"'{_redact_credentials(uri)}' with Flask application."
    )
    return mongo


def get_mongo_uri(
    host: str = "mongodb",
    port: int = 27017,
    db: str = "database",
) -> str:
    """Get MongoDB connection string for a database.

    If environment variable `MONGO_URI` is set, it is used as the connection
    string, with its database set to `db` (so that each database of the app
    is still accessed under its own name). A database in the path of
    `MONGO_URI` is kept as authentication database (`authSource`), unless
    `authSource` is set explicitly.

    Otherwise, the connection string is built from `host`, `port` and `db`,
    which may be overridden by environment variables `MONGO_HOST`,
    `MONGO_PORT` and `MONGO_DBNAME`, respectively; credentials are taken
    from environment variables `MONGO_USERNAME` and `MONGO_PASSWORD`.

    In both cases, environment variable `MONGO_DBNAME` overrides `db`.

    Args:
        host: Host at which MongoDB database is exposed.
        port: Port at which MongoDB database is exposed.
        db: Name of database to be accessed/created.

    Returns:
        MongoDB connection string.

    Raises:
        ValueError: Raised if `MONGO_URI` is not a MongoDB connection string.
    """
    db = os.environ.get("MONGO_DBNAME", db)
    uri = os.environ.get("MONGO_URI")
    if uri:
        return _set_uri_database(uri=uri, db=db)

    auth = ""
    user = os.environ.get("MONGO_USERNAME")
    if user:
        # An unset password is passed as 'None', as in previous versions;
        # PyMongo refuses to create clients with a username but no password
        password = str(os.environ.get("MONGO_PASSWORD"))
        auth = f"{quote_plus(user)}:{quote_plus(password)}@"
    host = os.environ.get("MONGO_HOST", host)
    port_str = os.environ.get("MONGO_PORT", str(port))
    return f"mongodb://{auth}{host}:{port_str}/{db}"


def _set_uri_database(uri: str, db: str) -> str:
    """Set database of MongoDB connection string.

    Args:
        uri: MongoDB connection string.
        db: Name of database to set.

    Returns:
        MongoDB connection string with database `db`. A previously set
        database is kept as authentication database, unless `authSource`
        is set.

    Raises:
        ValueError: Raised if `uri` is not a MongoDB connection string.
    """
    parts = urlsplit(uri)
    if parts.scheme not in ("mongodb", "mongodb+srv"):
        raise ValueError(
            "Environment variable 'MONGO_URI' must start with 'mongodb://' "
            "or 'mongodb+srv://'."
        )
    query = parts.query
    uri_db = parts.path.lstrip("/")
    has_auth_source = any(
        option.split("=", 1)[0].lower() == "authsource"
        for option in query.split("&")
    )
    if uri_db and not has_auth_source:
        auth_source = f"authSource={uri_db}"
        query = f"{query}&{auth_source}" if query else auth_source
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", query, ""))


def _redact_credentials(uri: str) -> str:
    """Remove credentials from MongoDB connection string.

    Args:
        uri: MongoDB connection string.

    Returns:
        MongoDB connection string without credentials.
    """
    parts = urlsplit(uri)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
