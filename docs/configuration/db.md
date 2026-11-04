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

To use a client outside an app (e.g., in scripts), use
`create_mongo_client()`:

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
`db.port` or `MONGO_URI`.

FOCA sets these defaults:

| Option | Default | Description |
| --- | --- | --- |
| `serverSelectionTimeoutMS` | `5000` | If MongoDB can't be reached, requests and the startup of the app fail after this time; PyMongo's default is 30 seconds. |
| `connectTimeoutMS` | `5000` | Time allowed to open a connection; PyMongo's default is 20 seconds. |
| `appname` | `service_info.service.id`, else `service_info.package`, else `foca` | Name of the app in MongoDB's logs. |

Options in the connection string of `MONGO_URI` win over `client_options`, and
FOCA logs a warning naming them. Note that this differs from PyMongo itself, where
keyword arguments win. It lets a deployment change an option without changing the
app's configuration, also for the defaults above, without a warning. Names in
either place are not case-sensitive. Aliases aren't resolved (e.g., `ssl` for
`tls`), so use the same name in both places.

The options apply to all clients of the app. `create_mongo_client()` takes them
as `options`, and applies the defaults as well.

## Environment variables

The connection can be configured through environment variables, e.g., to keep
credentials out of configuration files:

| Variable | Description |
| --- | --- |
| `MONGO_URI` | Full [connection string](https://www.mongodb.com/docs/manual/reference/connection-string/) (`mongodb://` or `mongodb+srv://`), e.g., for replica sets, TLS or other connection options. Takes precedence over the variables below, except `MONGO_DBNAME`. It is used as is, so a database in its path only acts as authentication database (as per MongoDB's defaults); each configured database is still accessed under its own name. |
| `MONGO_HOST` | Host; overrides `db.host`. |
| `MONGO_PORT` | Port; overrides `db.port`. |
| `MONGO_USERNAME` | Username. |
| `MONGO_PASSWORD` | Password. |
| `MONGO_DBNAME` | Database name; overrides the names of _all_ configured databases, i.e., all of them (including the access control database) point to this one database. Useful for deployments with a single database per app. |

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
