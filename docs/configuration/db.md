# `db`

[MongoDB](https://www.mongodb.com/) databases, collections and indexes of
the app.

```yaml
db:
  host: mongodb
  port: 27017
  dbs:
    myDb:
      collections:
        myCollection:
          indexes:
            - keys:
                id: 1
              options:
                unique: true
        mySecondCollection:
          indexes:
            - keys:
                other_id: 1
    myOtherDb:
      collections:
        myThirdCollection: {}
```

FOCA registers the databases and collections on app creation; use
[`get_collection()`](index.md#accessing-the-configuration) to access them, e.g.,
`get_collection(db="myDb", collection="myCollection")`.

All databases (including the access control database) share a single
[PyMongo](https://pymongo.readthedocs.io/) client per app. Configured indexes
are created on startup if they do not exist yet; existing indexes, including
ones created outside FOCA, are kept. If a configured index conflicts with an
existing one (e.g., same name, different keys), FOCA refuses to start; drop
or rename the existing index in that case.
`foca.database.register_mongodb.IndexConflictError` tells that apart from
other failures, such as missing permissions or lost connections, which stay
errors of PyMongo. To leave creating indexes to a step of your deployment, set
`db.create_indexes_on_startup: false` and run
[`foca create-indexes`](../cli.md#create-indexes).

To use the configured collections outside an app (e.g., in scripts), call
`Foca.use_config()`: `get_collection()` then returns them, without an app or
app context, and no indexes are created (see [Preparing for the ASGI
release](../preparing-for-asgi.md#configuration)). For a client of your own,
use `create_mongo_client()`:

```python
from foca.database.register_mongodb import (
    create_mongo_client,
    get_mongo_db_name,
)

db = create_mongo_client(host="mongodb", port=27017)[get_mongo_db_name("myDb")]
```

## Client options

`client_options` are passed to PyMongo's
[`MongoClient`](https://pymongo.readthedocs.io/en/stable/api/pymongo/mongo_client.html),
with PyMongo's names, e.g., for the size of the connection pool:

```yaml
db:
  client_options:
    maxPoolSize: 20
    serverSelectionTimeoutMS: 2000
```

Values are strings, numbers or booleans. Unknown or invalid options stop the app
on startup. `host`, `port` and `connect` can't be set here: use `db.host`,
`db.port` or `MONGO_URI`. Neither can `username` and `password`: use
[the environment variables below](#environment-variables).

FOCA sets these defaults:

| Option | Default | Description |
| --- | --- | --- |
| `serverSelectionTimeoutMS` | `5000` | If MongoDB can't be reached, requests and the startup of the app fail after this time; PyMongo's default is 30 seconds. |
| `connectTimeoutMS` | `5000` | Time allowed to open a connection; PyMongo's default is 20 seconds. |
| `appname` | `service_info.service.id`, else `service_info.package`, else `foca` (the latter if longer than the 128 bytes that PyMongo allows, and for clients of apps without a `db` section) | Name of the app in MongoDB's logs. |

Options in the connection string of `MONGO_URI` win over `client_options`, and
FOCA logs a warning naming them. Note that this differs from PyMongo itself, where
keyword arguments win. It lets a deployment change an option without changing the
app's configuration, also for the defaults above, without a warning. Names in
either place are not case-sensitive. Aliases aren't resolved (e.g., `ssl` for
`tls`), so use the same name in both places; PyMongo refuses `ssl` and `tls` with
different values. `authSource` only counts as set if the connection string has
that option: the database in its path is merely the default for it.

The options apply to all clients that the app gets from `get_mongo_client()`.
`create_mongo_client()` takes them as `options`, and applies the defaults, but
not `client_options`.

## Environment variables

The connection can be configured through environment variables, e.g., to keep
credentials out of configuration files:

`MONGO_URI`, `MONGO_USERNAME` and `MONGO_PASSWORD` can also [come from
files](index.md#environment-variables), through `MONGO_URI_FILE` and so on, e.g., for
mounted secrets.

| Variable | Description |
| --- | --- |
| `MONGO_URI` | Full [connection string](https://www.mongodb.com/docs/manual/reference/connection-string/) (`mongodb://` or `mongodb+srv://`), e.g., for replica sets, TLS or other connection options. Takes precedence over the variables below, except `MONGO_DBNAME`. It is used as is, so a database in its path only acts as authentication database (as per MongoDB's defaults); each configured database is still accessed under its own name. |
| `MONGO_HOST` | Host; overrides `db.host`. |
| `MONGO_PORT` | Port; overrides `db.port`. |
| `MONGO_USERNAME` | Username. |
| `MONGO_PASSWORD` | Password. |
| `MONGO_DBNAME` | Database name; overrides the names of _all_ configured databases, i.e., all of them (including the access control database) point to this one database. Useful for deployments with a single database per app. |

Username and password in `MONGO_URI` must be percent-escaped (FOCA does that for
`MONGO_USERNAME` and `MONGO_PASSWORD`). As PyMongo's errors about a malformed
connection string may quote parts of it, and so of a password, FOCA replaces them
with a fixed message of the same type (`pymongo.errors.InvalidURI`), without the
original as cause; errors about options keep their messages, as they name only the
options.

An unescaped `?` in a password makes the rest look like options, which PyMongo's
warnings about options, and the error about a missing file of an option like
`tlsCAFile`, can quote: these aren't changed, as they are what tells of wrong options.

If credentials are given via `MONGO_USERNAME` and `MONGO_PASSWORD`, the client
authenticates against the database in `MONGO_DBNAME`, if set, otherwise
against the first configured database. For other setups, use `MONGO_URI`
(e.g., with option `authSource`).

::: foca.models.config.MongoConfig
    options:
      members: false

::: foca.models.config.DBConfig
    options:
      members: false

::: foca.models.config.CollectionConfig
    options:
      members: false

::: foca.models.config.IndexConfig
    options:
      members: false
