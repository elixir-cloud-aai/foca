"""Register MongoDB database and collections."""

import logging
import os
from typing import Any
from urllib.parse import quote_plus, urlsplit, urlunsplit

from pymongo import MongoClient
from pymongo.errors import OperationFailure

from foca.models.config import DBConfig, MongoConfig

# Get logger instance
logger = logging.getLogger(__name__)


def register_mongodb(conf: MongoConfig) -> MongoConfig:
    """Register MongoDB databases and collections.

    All databases share the app's MongoDB client (cf.
    :py:func:`get_mongo_client`). Configured indexes are created if they do
    not exist yet; existing indexes are kept.

    Args:
        conf: :py:class:`foca.models.config.MongoConfig` instance describing
            databases and collections to be registered.

    Returns:
        `conf`, with clients of databases and collections set.
    """
    for db_name, db_conf in (conf.dbs or {}).items():
        _register_database(
            client=get_mongo_client(conf),
            db_name=db_name,
            db_conf=db_conf,
        )
    return conf


def get_mongo_client(conf: MongoConfig) -> MongoClient[dict[str, Any]]:
    """Get the MongoDB client of an app, creating it on first use.

    If credentials are configured via environment variables, the client
    authenticates against the database named in `MONGO_DBNAME`, if set,
    otherwise against the first configured database. Use `MONGO_URI` (e.g.,
    with option `authSource`) for other setups.

    Args:
        conf: :py:class:`foca.models.config.MongoConfig` instance describing
            the MongoDB server and databases.

    Returns:
        MongoDB client, shared by all databases of `conf`.
    """
    if conf._client is None:
        conf._client = create_mongo_client(
            host=conf.host,
            port=conf.port,
            db=next(iter(conf.dbs or {}), None),
        )
    return conf._client


def create_mongo_client(
    host: str = "mongodb",
    port: int = 27017,
    db: str | None = None,
) -> MongoClient[dict[str, Any]]:
    """Create MongoDB client.

    Cf. :py:func:`get_mongo_uri` for how the connection string is built,
    including environment variables. Select databases via the client, e.g.,
    ``client[get_mongo_db_name("my_db")]``.

    Args:
        host: Host at which MongoDB is exposed.
        port: Port at which MongoDB is exposed.
        db: Name of the database named in the connection string, i.e., the
            database to authenticate against, if credentials are given.

    Returns:
        MongoDB client.
    """
    uri = get_mongo_uri(host=host, port=port, db=db or "")
    client: MongoClient[dict[str, Any]] = MongoClient(uri)
    logger.info("Created MongoDB client for '%s'.", _redact_uri(uri))
    return client


def _register_database(
    client: MongoClient[dict[str, Any]],
    db_name: str,
    db_conf: DBConfig,
) -> None:
    """Register a database, its collections and their indexes.

    Args:
        client: MongoDB client.
        db_name: Name of the database as configured (cf.
            :py:func:`get_mongo_db_name`).
        db_conf: :py:class:`foca.models.config.DBConfig` instance describing
            the database; clients of the database and its collections are
            set.

    Raises:
        ValueError: A configured index conflicts with an existing index
            (e.g., one with the same name but different keys or options).
    """
    db_conf.client = client[get_mongo_db_name(db_name)]
    logger.info("Added database '%s'.", db_name)
    for coll_name, coll_conf in (db_conf.collections or {}).items():
        coll_conf.client = db_conf.client[coll_name]
        logger.info("Added database collection '%s'.", coll_name)
        for index in coll_conf.indexes or []:
            if index.keys is None:
                continue
            try:
                coll_conf.client.create_index(index.keys, **index.options)
            except OperationFailure as exc:
                raise ValueError(
                    f"Index {index.keys} of collection '{coll_name}' in "
                    f"database '{db_name}' conflicts with an existing index; "
                    f"drop or rename the existing index: {exc}"
                ) from exc
        if coll_conf.indexes:
            logger.info("Indexes ensured for collection '%s'.", coll_name)


def get_mongo_uri(
    host: str = "mongodb",
    port: int = 27017,
    db: str = "database",
) -> str:
    """Get MongoDB connection string.

    If environment variable `MONGO_URI` is set, it is returned unchanged. As
    a database named in its path is used as authentication database (and
    connection strings may set other database-related options), clients
    must select the database to access by name, e.g., via
    :py:func:`get_mongo_db_name`, rather than via the connection string.

    Otherwise, the connection string is built from `host`, `port` and `db`,
    which may be overridden by environment variables `MONGO_HOST`,
    `MONGO_PORT` and `MONGO_DBNAME`, respectively; credentials are taken
    from environment variables `MONGO_USERNAME` and `MONGO_PASSWORD`.

    Args:
        host: Host at which MongoDB database is exposed.
        port: Port at which MongoDB database is exposed.
        db: Name of database to be accessed/created.

    Returns:
        MongoDB connection string.

    Raises:
        ValueError: Raised if `MONGO_URI` is not a MongoDB connection string.
    """
    uri = os.environ.get("MONGO_URI")
    if uri:
        if urlsplit(uri).scheme not in ("mongodb", "mongodb+srv"):
            raise ValueError(
                "Environment variable 'MONGO_URI' must start with "
                "'mongodb://' or 'mongodb+srv://'."
            )
        return uri

    auth = ""
    user = os.environ.get("MONGO_USERNAME")
    if user:
        # An unset password is passed as 'None', as in previous versions;
        # PyMongo refuses to create clients with a username but no password
        password = str(os.environ.get("MONGO_PASSWORD"))
        auth = f"{quote_plus(user)}:{quote_plus(password)}@"
    host = os.environ.get("MONGO_HOST", host)
    port_str = os.environ.get("MONGO_PORT", str(port))
    return f"mongodb://{auth}{host}:{port_str}/{get_mongo_db_name(db)}"


def get_mongo_db_name(db: str) -> str:
    """Get name of MongoDB database.

    Args:
        db: Name of database as configured.

    Returns:
        Value of environment variable `MONGO_DBNAME`, if set, otherwise
        `db`.
    """
    return os.environ.get("MONGO_DBNAME", db)


def _redact_uri(uri: str) -> str:
    """Remove credentials and options from MongoDB connection string.

    Options are removed, as some may contain secrets (e.g.,
    `tlsCertificateKeyFilePassword`).

    Args:
        uri: MongoDB connection string.

    Returns:
        MongoDB connection string without credentials and options.
    """
    parts = urlsplit(uri)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
