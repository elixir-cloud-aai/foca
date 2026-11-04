"""Register MongoDB database and collections."""

import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote_plus, urlsplit

from pydantic import SecretStr
from pymongo import MongoClient
from pymongo.errors import OperationFailure
from pymongo.uri_parser import split_options

from foca.config.environment import get_env
from foca.models.config import DBConfig, MongoConfig

# Get logger instance
logger = logging.getLogger(__name__)

# Options of MongoDB clients unless set otherwise, in milliseconds: with
# MongoDB down, requests and the startup of the app fail after this time, not
# after PyMongo's default of 30 seconds
DEFAULT_CLIENT_OPTIONS = {
    "serverSelectionTimeoutMS": 5000,
    "connectTimeoutMS": 5000,
}


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


def reopen_mongodb(conf: MongoConfig) -> None:
    """Replace the MongoDB client of an app, for processes that fork.

    A client's threads don't survive a fork, and a client used across one may
    hang (cf. https://dochub.mongodb.org/core/pymongo-fork-deadlock). The
    current client is closed, and databases and collections are set to a new
    client, which only connects on first use, so that this doesn't block and
    forked processes can inherit the new client safely until it is used.
    Nothing is done if no client was created yet.

    Anything taken from the previous client (e.g., a collection kept in a
    variable, or the adapter of the access control) can't be used afterwards.

    Args:
        conf: :py:class:`foca.models.config.MongoConfig` instance whose
            clients of databases and collections are replaced.
    """
    previous = conf._client
    if previous is None:
        return
    conf._client = None
    client = get_mongo_client(conf, connect=False)
    for db_conf in (conf.dbs or {}).values():
        if db_conf.client is None:
            continue
        db_conf.client = client[db_conf.client.name]
        for coll_conf in (db_conf.collections or {}).values():
            if coll_conf.client is not None:
                coll_conf.client = db_conf.client[coll_conf.client.name]
    previous.close()
    logger.debug("Reopened MongoDB client.")


def get_mongo_client(
    conf: MongoConfig, connect: bool = True
) -> MongoClient[dict[str, Any]]:
    """Get the MongoDB client of an app, creating it on first use.

    If credentials are configured via environment variables, the client
    authenticates against the database named in `MONGO_DBNAME`, if set,
    otherwise against the first configured database. Use `MONGO_URI` (e.g.,
    with option `authSource`) for other setups.

    Args:
        conf: :py:class:`foca.models.config.MongoConfig` instance describing
            the MongoDB server and databases.
        connect: Whether a client created here connects in the background
            right away, rather than on first use.

    Returns:
        MongoDB client, shared by all databases of `conf`.
    """
    if conf._client is None:
        conf._client = create_mongo_client(
            host=conf.host,
            port=conf.port,
            db=next(iter(conf.dbs or {}), None),
            connect=connect,
            options=conf.client_options,
            appname=conf._default_appname,
        )
    return conf._client


def create_mongo_client(
    host: str = "mongodb",
    port: int = 27017,
    db: str | None = None,
    connect: bool = True,
    options: Mapping[str, Any] | None = None,
    appname: str | None = None,
) -> MongoClient[dict[str, Any]]:
    """Create MongoDB client.

    Cf. :py:func:`get_mongo_uri` for how the connection string is built,
    including environment variables. Select databases via the client, e.g.,
    ``client[get_mongo_db_name("my_db")]``. Options in the connection string
    win over `options`, with a warning, and over defaults (cf.
    :py:func:`_get_client_options`).

    Args:
        host: Host at which MongoDB is exposed.
        port: Port at which MongoDB is exposed.
        db: Name of the database named in the connection string, i.e., the
            database to authenticate against, if credentials are given.
        connect: Whether the client connects in the background right away,
            rather than on first use.
        options: Options of the client, with PyMongo's names.
        appname: Name of the app in MongoDB's logs, unless `options` or the
            connection string set one.

    Returns:
        MongoDB client.

    Raises:
        pymongo.errors.PyMongoError: An option is unknown, or options conflict.
        TypeError: An option has a value of the wrong type.
        ValueError: An option has an invalid value.
    """
    uri = get_mongo_uri(host=host, port=port, db=db or "")
    client: MongoClient[dict[str, Any]] = MongoClient(
        uri,
        connect=connect,
        **_get_client_options(uri=uri, options=options or {}, appname=appname),
    )
    logger.info("Created MongoDB client for '%s'.", _redact_uri(uri))
    return client


def _get_client_options(
    uri: str, options: Mapping[str, Any], appname: str | None
) -> dict[str, Any]:
    """Get the keyword arguments for a MongoDB client.

    PyMongo gives keyword arguments precedence over the connection string, but
    the connection string of a deployment should win over the configuration of
    an app, so options it sets are left out. Names of options are not
    case-sensitive, as in PyMongo.

    Args:
        uri: Connection string.
        options: Configured options.
        appname: Name of the app, if not set otherwise.

    Returns:
        Defaults (cf. `DEFAULT_CLIENT_OPTIONS`), the name of the app and the
        configured options, in order of precedence and with lowercase names,
        without those in the connection string.
    """
    # As PyMongo parses it (e.g., `#` isn't special, `;` separates options);
    # names only, as `MongoClient` validates the values
    query = uri.partition("?")[2]
    in_uri = (
        {
            name.lower()
            for name in split_options(
                query, validate=False, warn=False, normalize=False
            )
        }
        if query
        else set()
    )
    overridden = sorted(name for name in options if name.lower() in in_uri)
    if overridden:
        # Only the names, as values may be sensitive
        logger.warning(
            "MongoDB client options %s are also set in the connection string, "
            "which takes precedence.",
            ", ".join(overridden),
        )
    merged: dict[str, Any] = {
        name.lower(): value for name, value in DEFAULT_CLIENT_OPTIONS.items()
    }
    if appname is not None:
        merged["appname"] = appname
    merged.update(
        {
            name.lower(): (
                value.get_secret_value() if isinstance(value, SecretStr) else value
            )
            for name, value in options.items()
        }
    )
    return {name: value for name, value in merged.items() if name not in in_uri}


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
    uri = get_env("MONGO_URI")
    if uri:
        if urlsplit(uri).scheme not in ("mongodb", "mongodb+srv"):
            raise ValueError(
                "Environment variable 'MONGO_URI' must start with "
                "'mongodb://' or 'mongodb+srv://'."
            )
        return uri

    auth = ""
    user = get_env("MONGO_USERNAME")
    if user:
        # An unset password is passed as 'None', as in previous versions;
        # PyMongo refuses to create clients with a username but no password
        password = str(get_env("MONGO_PASSWORD"))
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
    # As PyMongo splits it: for `urlsplit()`, a `#` in a password would start
    # a fragment
    scheme, _, rest = uri.partition("://")
    return f"{scheme}://{rest.partition('?')[0].rpartition('@')[2]}"
