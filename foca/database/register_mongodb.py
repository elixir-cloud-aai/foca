"""Register MongoDB database and collections."""

import logging
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote_plus

from pydantic import SecretStr
from pymongo import MongoClient
from pymongo.errors import InvalidURI, OperationFailure
from pymongo.uri_parser import split_options

from foca.config.environment import get_env
from foca.models.config import DBConfig, MongoConfig

# Get logger instance
logger = logging.getLogger(__name__)

# `IndexAlreadyExists`, `IndexOptionsConflict`, `IndexKeySpecsConflict`
_INDEX_CONFLICT_CODES = (68, 85, 86)
_DUPLICATE_KEY_CODE = 11000

# Names of databases that logs tell as they are
_PLAIN_DB_NAME = re.compile(r"[\w-]*")

# Starts of messages of `InvalidURI` that only name PyMongo's own options; all
# others are replaced, as they may quote parts of the connection string
# (wording checked against PyMongo 4.19; the tests pin it, and a changed one
# only makes such a message generic)
_OPTION_ERRORS = (
    "URI option",
    "Can not mix",
    "Can not specify conflicting values for URI options",
    "MongoDB URI options",
    "You cannot specify",
    "the authSource database",
)

# Options of MongoDB clients unless set otherwise, in milliseconds: with
# MongoDB down, requests and the startup of the app fail after this time, not
# after PyMongo's default of 30 seconds
DEFAULT_CLIENT_OPTIONS = {
    "serverSelectionTimeoutMS": 5000,
    "connectTimeoutMS": 5000,
}


def register_mongodb(
    conf: MongoConfig, create_indexes: bool | None = None
) -> MongoConfig:
    """Register MongoDB databases and collections.

    All databases share the app's MongoDB client (cf.
    :py:func:`get_mongo_client`). Configured indexes are created if they do
    not exist yet; existing indexes are kept.

    Args:
        conf: :py:class:`foca.models.config.MongoConfig` instance describing
            databases and collections to be registered.
        create_indexes: Whether to create the configured indexes; if `None`,
            as set via `db.create_indexes_on_startup`.

    Returns:
        `conf`, with clients of databases and collections set.
    """
    if create_indexes is None:
        create_indexes = conf.create_indexes_on_startup
    for db_name, db_conf in (conf.dbs or {}).items():
        _register_database(
            client=get_mongo_client(conf),
            db_name=db_name,
            db_conf=db_conf,
            create_indexes=create_indexes,
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
        pymongo.errors.InvalidURI: The connection string is malformed, with a
            fixed message and no chain, as PyMongo's may quote a part of it,
            e.g., of a password with unescaped characters. Options conflict,
            with PyMongo's message.
        pymongo.errors.PyMongoError: An option is unknown.
        TypeError: An option has a value of the wrong type.
        ValueError: An option has an invalid value.
    """
    uri = get_mongo_uri(host=host, port=port, db=db or "")
    malformed = False
    try:
        client: MongoClient[dict[str, Any]] = MongoClient(
            uri,
            connect=connect,
            **_get_client_options(uri=uri, options=options or {}, appname=appname),
        )
    except InvalidURI as exc:
        if str(exc).startswith(_OPTION_ERRORS):
            raise
        malformed = True
    # Out of the handler, as the original would stay available as context
    if malformed:
        raise InvalidURI(
            "The MongoDB connection string, e.g., in 'MONGO_URI', is malformed. "
            "Check that username and password are percent-escaped (a '/', ':', "
            "'@' or '%' in them breaks it), and its hosts and database name; "
            "PyMongo's message isn't shown, as it may quote credentials."
        )
    logger.info(
        "Created MongoDB client for '%s/%s'.",
        _get_client_hosts(client, uri),
        _get_loggable_db_name(db or ""),
    )
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
    # Secrets are unwrapped here, where they are used
    merged.update(
        {
            name.lower(): (
                value.get_secret_value() if isinstance(value, SecretStr) else value
            )
            for name, value in options.items()
        }
    )
    return {name: value for name, value in merged.items() if name not in in_uri}


class IndexConflictError(ValueError):
    """A configured index conflicts with an existing index or stored documents.

    Other errors of creating indexes (e.g., missing permissions or connection
    failures) stay what they are: subclasses of
    :py:class:`pymongo.errors.PyMongoError`.
    """


def _is_index_conflict(exc: OperationFailure) -> bool:
    """Check whether an error of creating an index tells of a conflict.

    Args:
        exc: Error.

    Returns:
        Whether it does, by its code; mongomock has none, but a message.
    """
    return exc.code in _INDEX_CONFLICT_CODES or (
        exc.code is None and "already exists" in str(exc)
    )


def _register_database(
    client: MongoClient[dict[str, Any]],
    db_name: str,
    db_conf: DBConfig,
    create_indexes: bool = True,
) -> None:
    """Register a database, its collections and their indexes.

    Args:
        client: MongoDB client.
        db_name: Name of the database as configured (cf.
            :py:func:`get_mongo_db_name`).
        db_conf: :py:class:`foca.models.config.DBConfig` instance describing
            the database; clients of the database and its collections are
            set.
        create_indexes: Whether to create the configured indexes.

    Raises:
        IndexConflictError: A configured index conflicts with an existing
            index (e.g., one with the same name but different keys or options),
            or stored documents violate it, with the error of PyMongo as cause.
        pymongo.errors.PyMongoError: Indexes can't be created for another reason,
            e.g., missing permissions or a connection failure.
    """
    db_conf.client = client[get_mongo_db_name(db_name)]
    logger.info("Added database '%s'.", db_name)
    for coll_name, coll_conf in (db_conf.collections or {}).items():
        coll_conf.client = db_conf.client[coll_name]
        logger.info("Added database collection '%s'.", coll_name)
        if not create_indexes:
            continue
        for index in coll_conf.indexes or []:
            if index.keys is None:
                continue
            try:
                coll_conf.client.create_index(index.keys, **index.options)
            except OperationFailure as exc:
                if exc.code == _DUPLICATE_KEY_CODE:
                    raise IndexConflictError(
                        f"Index {index.keys} of collection '{coll_name}' in "
                        f"database '{db_name}' can't be created, as stored "
                        f"documents violate it: {exc}"
                    ) from exc
                if not _is_index_conflict(exc):
                    raise
                raise IndexConflictError(
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
        # Not via `urlsplit()`, whose errors may quote credentials
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError(
                "Environment variable 'MONGO_URI' must start with "
                "'mongodb://' or 'mongodb+srv://'."
            )
        return uri

    auth = ""
    user = get_env("MONGO_USERNAME")
    # Read also without a username, so that a wrong source is reported
    password = get_env("MONGO_PASSWORD")
    if user:
        # An unset password is passed as 'None', as in previous versions;
        # PyMongo refuses to create clients with a username but no password
        auth = f"{quote_plus(user)}:{quote_plus(str(password))}@"
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


def _get_client_hosts(client: MongoClient[dict[str, Any]], uri: str) -> str:
    """Get the hosts and ports of a MongoDB client, for logs.

    They come from what the client made of its connection string, so that no
    other part of it ends up in logs, whatever its form. They are left out if
    a credential with unescaped characters may have been taken for a host: an
    `@` after the end of the hosts is that, or a value of an option, and so is
    a `:` in what has no port, i.e., sockets and SRV records. A host that looks
    like any other can't be told apart, e.g., behind a `,` in a password.

    Args:
        client: MongoDB client.
        uri: Connection string of the client, only checked for these forms.

    Returns:
        Hosts and ports, separated by commas, or `unknown` if they are left
        out, and for stand-ins of clients in the tests of apps, e.g., of
        mongomock.
    """
    if "@" in uri.partition("://")[2].replace("?", "/").partition("/")[2]:
        return "unknown"
    try:
        addresses = list(client.topology_description.server_descriptions())
    except (AttributeError, TypeError):
        return "unknown"
    if not addresses or any(port is None and ":" in host for host, port in addresses):
        return "unknown"
    return ",".join(
        sorted(host if port is None else f"{host}:{port}" for host, port in addresses)
    )


def _get_loggable_db_name(db: str) -> str:
    """Get the name of a MongoDB database, for logs.

    Args:
        db: Name of the database as configured (cf. :py:func:`get_mongo_db_name`).

    Returns:
        The name in use or, as `MONGO_DBNAME` ends up in a connection string
        and may so hold options, a placeholder if it has other characters than
        word characters and hyphens.
    """
    name = get_mongo_db_name(db)
    return name if _PLAIN_DB_NAME.fullmatch(name) else "(name not shown)"
