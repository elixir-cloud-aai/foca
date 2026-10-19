# ![FOCA_logo][foca-logo] &ensp;_Develop Flask microservices quickly!_

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Docs][badge-docs]][badge-url-docs]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![PyPI_release][badge-pypi]][badge-url-pypi]

**FOCA** (**F**lask-**O**penAPI-**C**onnexion-**A**rchetype) is an opinionated
archetype that enables fast development of [OpenAPI][res-openapi]-based HTTP
API microservices in [Flask][res-flask], leveraging the excellent
[Connexion][res-connexion] framework.

FOCA reduces the required boilerplate code to fire up your app to a bare
minimum and allows you to _**focus on your application logic**_. It also avoids
unnecessary code repetition and introduces cross-service consistency when
developing multiple applications. Simply write a configuration file, pass
it to FOCA and you're good to go!

Currently supported features:

* Manage app configuration
* Handle exceptions
* Register OpenAPI 2.x/3.x specifications
* Protect endpoints via [JWT][res-jwt] validation
* Register [MongoDB][res-mongo-db] collections
* Run asynchronous tasks via [RabbitMQ][res-rabbitmq] & [Celery][res-celery]
* [CORS][res-cors] support

Check the [API docs][badge-url-docs] for further details.

## Table of Contents

* [Usage](#usage)
* [Configuration](#configuration)
  * [Environment-specific configuration](#environment-specific-configuration)
  * [Configuring OpenAPI specifications](#configuring-openapi-specifications)
  * [Configuring MongoDB](#configuring-mongodb)
  * [Configuring exceptions](#configuring-exceptions)
  * [Configuring asynchronous tasks](#configuring-asynchronous-tasks)
  * [Configuring logging](#configuring-logging)
  * [Configuring security](#configuring-security)
  * [Configuring the server](#configuring-the-server)
  * [Custom configuration](#custom-configuration)
  * [Accessing configuration parameters](#accessing-configuration-parameters)
* [Utilities](#utilities)
  * [Database utilities](#database-utilities)
  * [Logging utitlies](#logging-utilities)
  * [Miscellaneous utilities](#miscellaneous-utilities)
  * [Access Control utilities](#access-control-utilities)
* [Contributing](#contributing)
* [Versioning](#versioning)
* [License](#license)
* [Contact](#contact)

## Usage

(1) Install the [FOCA package][badge-url-pypi] with `pip`:

```bash
pip install foca
```

(2) Create a [configuration](#configuration) file.

(3) Import the FOCA class and pass your config file:

```python
from foca import Foca

foca = Foca(config_file="path/to/my/app/config.yaml")
app = foca.create_app()  # returns a Connexion app instance
```

(4) Start your [Flask][res-flask] app as usual.

![Hint][img-hint] _**Check out the [Petstore example application][example]
shipped with this repository to see FOCA in action!**_

## Configuration

![Hint][img-hint] _**In order to get you started writing your own app
configuration, you can copy the [**annotated template**][config-template]
shipped with this repository and modify it.**_

In order to use FOCA functionalities, you must create a [YAML][res-yaml]
configuration file that includes keyword sections reserved by FOCA. The
following top-level sections are interpreted by FOCA (exhaustive; links are
provided to the corresponding sections in this documentation, as well as to the
corresponding models in the [API docuementation][badge-url-docs]):

* [`api`](#configuring-openapi-specifications) / [model][docs-models-api]
* [`db`](#configuring-mongodb) / [model][docs-models-db]
* [`exceptions`](#configuring-exceptions) / [model][docs-models-exceptions]
* [`jobs`](#configuring-asynchronous-tasks) / [model][docs-models-jobs]
* [`log`](#configuring-logging) / [model][docs-models-log]
* [`security`](#configuring-security) / [model][docs-models-security]
* [`server`](#configuring-the-server) / [model][docs-models-server]

**_Any values passed to reserved keywords are automatically validated_**, and a
corresponding informative exception will be raised whenever a value does not
adhere to the corresponding model as described in the [API
documentation][docs-models]. If you do _not_ want to make use of a specific
FOCA functionality, simply omit the corresponding section.

Relative file paths in the configuration (e.g., of OpenAPI specifications or
the access control model) are resolved against the directory of the
configuration file (the first one, if several are used; see below), so an
app finds its files irrespective of the directory it is started from.
Without a configuration file (e.g., if the configuration is created in
code), they are resolved against the current working directory. Use
`Foca.config_dir` to resolve paths in your custom configuration the same way.

### Environment-specific configuration

Settings that differ between environments (e.g., development and
production) can be kept in separate files that are merged into a base
configuration, in order: mappings are merged recursively, while lists and
other values (including `null`) replace those of earlier files.

```python
from foca import Foca

foca = Foca(config_file=["config.yaml", "config.dev.yaml"])
```

Files listed in the environment variable `FOCA_CONFIG_OVERLAY` (separated by
`:` on Linux and macOS, `;` on Windows; relative to the current working
directory) are merged afterwards, e.g., to select the overlay at deployment
time, such as from a Kubernetes ConfigMap:

```bash
FOCA_CONFIG_OVERLAY=deployment/config.prod.yaml python app.py
```

```yaml
# deployment/config.prod.yaml
server:
  port: 80
log:
  root:
    level: 30
```

The merged configuration is validated once, and the merged files are logged.
If your app needs to know its environment, define a value in your
[custom configuration](#custom-configuration) and set it in the overlays.

### Configuring OpenAPI specifications

The `api` section is used to specify any [OpenAPI][res-openapi] specifications
consumed as part of your application. Essentially, FOCA adds a wrapper around
[Connexion][res-connexion], which validates requests/responses and can serve
the specifications as well as a [Swagger][res-swagger]-based user interface to
explore the API. FOCA supports multiple specification files (versions
Swagger/OpenAPI 2.x, OpenAPI 3.x and mixed) and multiple fragments thereof, and
it adds additional features that allow easy modification of specifications on
the fly. In particular, links to routers and security definitions can be added
to each specified endpoint.

_Example:_

```yaml
api:
  specs:
    - path:
        - path/to/openapi/specs.yaml
        - path/to/openapi/additions.yaml
      add_operation_fields:
        x-openapi-router-controller: myapi.controllers
      add_security_fields:
        x-bearerInfoFunc: app.validate_token
      disable_auth: False
      connexion:
        strict_validation: True
        validate_responses: True
        options:
          swagger_ui: True
          serve_spec: True
    - path:
        - path/to/openapi/other_specs.yaml
```

> In this example, the configuration file lists two separate specifications.
> The first is a composite one that FOCA will compile from two files,
> `path/to/openapi/specs.yaml` and `path/to/openapi/additions.yaml`. It comes
> with a range of different explicitly specified parameters to further
> customize the specification itself (classes/functions implementing
> controllers and token validation are linked to each endpoint via
> `add_operation_fields`; `x-openapi-router-controller` and `x-bearerInfoFunc`
> can be used to link controller functions/classes and authorization validation
> functions to endpoints, respectively. Furthermore, a flag to disable the need
> for passing authorization tokens and several [Connexion][res-connexion]
> options are explicitly set for this specification. In contrast, only the path
> to a single file is specified for the second specification, implying default
> values for all other options.  
>  
> Further support for validating authorization can also be added to
> specifications via the `add_security_fields` parameter under `specs` (not
> shown here). Cf. the [API model][docs-models-api] for this and other options,
> as well as further details.

### Configuring MongoDB

FOCA can register one or more [MongoDB][res-mongo-db] databases and/or
collections for you. To use that functionality, simply include the top-level
`db` keyword section in your configuration file and tune its behavior through
the available parameters.

_Example:_

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
                'unique': True
        mySecondCollection:
          indexes:
            - keys:
                other_id: 1
    myOtherDb:
      collections:
        myThirdCollection:
          indexes:
            - keys:
                third_id: 1
```

> In this example, two databases (`myDb` and `myOtherDb`) are configured, the
> former with two and the latter with one collection (`myCollection`,
> `mySecondCollection` and `myThirdCollection`, respectively). FOCA will
> automatically register and initialize these databases and collections for you
> and add convenient clients to the app instance (accessible as children of
> `current_app.config.foca` in an [application
> context][res-flask-app-context]). The collections would be indexed by keys
> `id`, `other_id` and `third_id`, respectively. Out of these, only `id`
> will be required to be unique.  
>  
> Cf. the [API model][docs-models-db] for further options and details.

All databases (including the access control database) share a single
[PyMongo][res-pymongo] client per app. Configured indexes are created on
startup if they do not exist yet; existing indexes, including ones created
outside FOCA, are kept. If a configured index conflicts with an existing one
(e.g., same name, different keys), FOCA refuses to start; drop or rename the
existing index in that case. To use a client outside an app (e.g., in
scripts), use `foca.database.register_mongodb.create_mongo_client()`:

```python
from foca.database.register_mongodb import (
    create_mongo_client,
    get_mongo_db_name,
)

db = create_mongo_client(host="mongodb", port=27017)[get_mongo_db_name("myDb")]
```

The connection can be configured through environment variables, e.g., to
keep credentials out of configuration files:

| Variable | Description |
| --- | --- |
| `MONGO_URI` | Full [connection string][res-mongo-db-uri] (`mongodb://` or `mongodb+srv://`), e.g., for replica sets, TLS or other connection options. Takes precedence over the variables below, except `MONGO_DBNAME`. It is used as is, so a database in its path only acts as authentication database (as per MongoDB's defaults); each configured database is still accessed under its own name. |
| `MONGO_HOST` | Host; overrides `db.host`. |
| `MONGO_PORT` | Port; overrides `db.port`. |
| `MONGO_USERNAME` | Username. |
| `MONGO_PASSWORD` | Password. |
| `MONGO_DBNAME` | Database name; overrides the names of _all_ configured databases, i.e., all of them (including the access control database) point to this one database. Useful for deployments with a single database per app. |

If credentials are given via `MONGO_USERNAME` and `MONGO_PASSWORD`, the
client authenticates against the database in `MONGO_DBNAME`, if set,
otherwise against the first configured database. For other setups, use
`MONGO_URI` (e.g., with option `authSource`).

### Configuring exceptions

FOCA registers an exception handler that responds to all exceptions with
[problem details][res-rfc-9457] (`application/problem+json`), e.g.:

```json
{"title": "Not Found", "status": 404}
```

By default, exceptions with an HTTP error status code (e.g., werkzeug and
Connexion exceptions, such as `werkzeug.exceptions.NotFound`) get that status
and its standard reason phrase as title, and all other exceptions `500
Internal Server Error`. To customize problem details, map exception classes
to problem details in a dictionary and specify it in the top-level
`exceptions` section of the app configuration:

_Example:_

```yaml
exceptions:
  mapping: my_app.exceptions.problems
  public_members: [title, status, detail]
  logging: oneline
```

```python
# my_app/exceptions.py
from werkzeug.exceptions import NotFound


class PetNotFound(NotFound):
    pass


problems = {
    NotFound: {"title": "Not Found", "status": 404},
    PetNotFound: {
        "title": "Pet Not Found",
        "status": 404,
        "detail": "We have never heard of this pet.",
        "type": "https://example.org/problems/pet-not-found",
    },
}
```

> Each entry must have an integer `status` (400 to 599, used as the
> response's status code) and a string `title`; other members are optional.
> Exceptions that are not listed are handled like their closest listed base
> class, so a subclass of `PetNotFound` gets the problem details of
> `PetNotFound`, and other subclasses of `NotFound` those of `NotFound`. An
> entry for `Exception` is only used for exceptions without status code. With
> `public_members`, only the listed members are returned to clients (by
> default, all are). Exceptions are logged with their full problem details,
> here on a single line, including any traceback (`logging`: `oneline`,
> `minimal`, `regular` or `none`).
>
> Cf. the [API model][docs-models-exceptions] for further details.

### Configuring asynchronous tasks

FOCA offers limited support for running asynchronous tasks via the
[RabbitMQ][res-rabbitmq] broker and [Celery][res-celery]. To make use of it,
include the `jobs` top-level section in the app configuration file.

_Example:_

```yaml
jobs:
  host: rabbitmq
  port: 5672
  backend: 'rpc://'
  include:
    - my_app.tasks.my_task_1
    - my_app.tasks.my_task_2
```

> This config attaches the `rabbitmq` broker host running on port `5672` to
> FOCA and registers the tasks found in modules `my_task_1` and `my_task_2`.  
>  
> Cf. the [API model][docs-models-jobs] for further details.

To pass credentials or use TLS, set the broker URL via environment variable
`CELERY_BROKER_URL` (e.g., `amqps://user:password@rabbitmq:5671//`), which
takes precedence over `jobs.host` and `jobs.port`. Likewise,
`CELERY_RESULT_BACKEND` takes precedence over `jobs.backend`. Both are logged
without credentials.  

The `foca.Foca` class provides a method `.create_celery_app()` that you can
use in your Celery worker entry point to crate a Celery app, like so:

```py
from foca import Foca

foca = Foca(config_file="my_app/config.yaml")
my_celery_app = foca.create_celery_app()
```

### Configuring logging

FOCA allows you to specify a YAML-based logging configuration to control your
application's logging behavior in an effort to provide a single configuration
file for every application. To use it, simply add a `log` top-level section in
your app configuration file.

_Example:_

```yaml
log:
  version: 1
  disable_existing_loggers: False
  formatters:
    standard:
      class: logging.Formatter
      style: "{"
      format: "[{asctime}: {levelname:<8}] {message} [{name}]"
  handlers:
    console:
      class: logging.StreamHandler
      level: 20
      formatter: standard
      stream: ext://sys.stderr
  root:
    level: 10
    handlers: [console]
```

> The logging configuration is simply passed on to Python's `logging' package,
> and so it has to conform with that [package's
> requirements][res-python-logging]. See [here][res-python-logging-how-to] for
> more info.

### Configuring security

FOCA offers some convenience functionalities for securing your app.
Specifically, it allows you to configure the validation of [JSON Web
Token (JWT)][res-jwt]-based authorization, a [Casbin][res-casbin]-based access
control model, and the use of [cross-origin resource sharing (CORS)][res-cors].
To make use of them, include the `security` top-level section in your app
configuration, as well as the desired sublevel section(s):

```yaml
security:
  auth:
    issuers:
      - https://login.example.org/oidc/
    audience:
      - my-app
    algorithms:
      - RS256
    allow_expired: False
  access_control:
    enabled: True
    api_specs: 'path/to/your/access/control/specs'
    db_name: access_control_db_name
    collection_name: access_control_collection_name
    model: access_control_model_definition
  cors:
    enabled: True
    allow_origins:
      - https://my-app.example.org
    allow_credentials: True
```

> In this example, only JWT Bearer tokens issued by
> `https://login.example.org/oidc/` for audience `my-app` would be accepted.
> They would be verified locally with the issuer's public keys (obtained via
> OpenID Connect discovery), signed with the `RS256` algorithm, and expired
> tokens would be rejected. Tokens of other issuers are rejected without
> contacting them; if no `issuers` are configured, all tokens are rejected.
> Set `audience` whenever your identity provider includes your app in the
> audience of its tokens; without it, tokens that trusted issuers issued for
> other services are accepted, too (FOCA logs a warning at startup).
> Set `validate_userinfo: True` to additionally validate tokens via the
> issuer's user info endpoint. Which operations require a token is defined by
> the security requirements of the operations in your API specifications
> (e.g., via `x-bearerInfoFunc: foca.security.auth.validate_token`).
> Furthermore, as access control is `enabled`, the application created with
> this config would provide an access control model `model`. Corresponding
> permissions could be accessed and altered by a user with admin permissions
> via the permissions API (`/admin/access-control/permissions`), defined in
> `api_specs` (FOCA's default specification if not set). Permissions will be
> stored in collection `collection_name` of MongoDB database `db_name`.
> Finally, cross-origin requests would only be allowed from
> `https://my-app.example.org`, including credentials. By default, CORS is
> enabled for any origin, without credentials; allowing credentials requires
> explicit origins. Further options (`allow_methods`, `allow_headers`,
> `expose_headers`, `max_age`) follow Starlette's `CORSMiddleware`.
>  
> Cf. the [API model][docs-models-security] for further options and details.

**Note:** A detailed explaination of the access control implementation can be
found [here][docs-access-control].

### Configuring the server

FOCA allows you to pass certain basic configuration options to your Flask
application. To modify defaults, include the top-level `server` keyword section
in your app configuration file:

```yaml
server:
  host: '127.0.0.1'
  port: 8080
  debug: True
```

> This config would create an application server at `127.0.0.1:8080` with
> Flask's debug mode, including its interactive debugger, switched on. As the
> debugger allows executing arbitrary code, debug mode is off by default and
> should only be enabled for local development; FOCA logs a warning if it is
> enabled on a host other than a loopback address (e.g., `0.0.0.0`).
>  
> Cf. the [API model][docs-models-server] for further options and details.

### Custom configuration

If you would like FOCA to validate your custom app configuration (e.g.,
parameters required for individual controllers, you can provide a path, in
dot notation, to a [`pydantic`][res-pydantic] `BaseModel`-derived model. FOCA
then tries to instantiate the model class with any custom parameters listed
under keyword section `custom`.

Suppose you have a model like the following defined in module
`my_app.custom_config`:

```py
from pydantic import BaseModel


class CustomConfig(BaseModel):
    my_param: int = 5
```

And you have, in your app configuration file `my_app/config.yaml`, the
following section:

```console
custom:
  my_param: 10
```

You can then have FOCA validate your custom configuration against the
`CustomConfig` class by including it in the `Foca()` call like so:

```py
from foca import Foca

foca = Foca(
    config="my_app/config.yaml",
    custom_config_model="my_app.custom_config.CustomConfig",
)
my_app = foca.create_app()
```

We recommend that, when defining your `pydantic` model, that you supply
default values wherever possible. In this way, the custom configuration
parameters will always be available, even if not explicitly listed in the app
configuration (like with the FOCA-specific parameters).

> Note that there is tooling available to automatically generate `pydantic`
> models from different file formats like JSON Schema etc. See here for the
> [datamodel-code-generator][res-datamodel-code-generator] project.

Apart from the reserved keyword sections listed above, you are free to include
any other sections and parameters in your app configuration file. FOCA will
simply attach these to your application instance as described
[above](#configuration) and shown [below](#accessing-configuration-parameters).
Note, however, that any such parameters need to be _manually_ validated. The
same is true if you include a `custom` section but do _not_ provide a
validation model class via the `custom_config_model` parameter when
instantiating `Foca`.

_Example:_

```yaml
my_custom_param: 'some_value'

my_custom_param_section:
  another_custom_param: 3
  my_custom_list_param:
    - 1
    - 2
    - 3
```

### Accessing configuration parameters

Once the application is created via `Foca(...).create_app()`, use
`foca.get_config()` to access the (validated and typed) configuration, e.g.,
in controllers, security handlers or Celery tasks:

```python
from foca import get_collection, get_config

config = get_config()

db = config.db
api = config.api
server = config.server
exceptions = config.exceptions
security = config.security
jobs = config.jobs
log = config.log
custom = config.custom  # if a `custom` section is configured

# Registered MongoDB collections
collection = get_collection(db="myDb", collection="myCollection")
```

Inside an [application context][res-flask-app-context], `get_config()`
returns the configuration of that app; otherwise (e.g., in Celery workers or
scripts), that of the most recently created FOCA app. It raises a
`RuntimeError` if no FOCA app has been created. Outside of apps, the
configuration is also available as `Foca(...).conf`. For backwards
compatibility, it remains available as `current_app.config.foca`, too.

### More examples

Apart from the [annotated template][config-template], you can also check
out the [configuration file][config-petstore] of the [Petstore app][example]
for another example.

![Hint][img-hint] _**Or why not explore [apps that already use
FOCA][res-using-foca]?**_

## Utilities

FOCA provides some functions that may be useful for several applications.
Simply import them if you want to use them.

### Database utilities

FOCA provides the following general-purpose MongoDB controllers:

* Fetch latest object given the db `collection`:

```python
from foca.utils.db import find_one_latest

latest_object = find_one_latest("your_db_collection_instance")
```

* Fetch latest object identifier (`id`) given the db `collection`:

```python
from foca.utils.db import find_id_latest

latest_object_id = find_id_latest("your_db_collection_instance")
```

### Logging utilities

FOCA provides a decorator that can be used on any route to automatically log
any requests and/or responses passing through that route:

```python
from foca.utils.logging import log_traffic


@log_traffic(log_request=True, log_response=True, log_level=20)
def your_controller():
    pass
```

> The above decorater will log both requests and responses with the specified
> logging level (`20`, or `INFO`).

### Miscellaneous utilities

* Generate a random object from a given character set:

```python
import string

from foca.utils.misc import generate_id

obj_id = generate_id(charset=string.digits, length=6)
```

> The above function processes and returns a random `obj_id` of length `6`
> consisting of only digits (`string.digits`). Characters are drawn with the
> cryptographically secure `secrets` module. The `charset` is used as is: if
> it is read from a config file, list the allowed characters themselves (e.g.,
> `ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789`); expressions such as
> `string.ascii_uppercase + string.digits` are no longer evaluated and raise a
> `ValueError`.

### Access Control utilities

Set `security.access_control.enforce: True` to enforce access control
policies on all operations requiring authentication, using the identity and
group claims of validated bearer tokens. Alternatively, enforce policies on
single controllers only:

```python
from foca.security.access_control import enforce_access_control


@enforce_access_control
def your_controller():
    pass
```

In your controllers, use `get_current_subject()` and `get_current_groups()`
to get the identity and roles of the current request. FOCA also provides
helpers for managing resource-scoped roles, e.g., for resource ownership. See
the [access control documentation][docs-access-control] for details.

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contributing
guidelines][res-elixir-cloud-contributing] if you are interested to contribute.
Please mind the [code of conduct][res-elixir-cloud-coc] for all interactions
with the community.

## Versioning

The project adopts [semantic versioning][res-semver]. Currently the service
is in beta stage, so the API may change without further notice.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][org-elixir-cloud]. Follow the link to get in touch with us via chat or
email. Please mention the name of this service for any inquiry, proposal,
question etc.

![Logo_banner][img-logo-banner]

[badge-build-status]: <https://github.com/elixir-cloud-aai/foca/actions/workflows/checks.yml/badge.svg>
[badge-coverage]: <https://codecov.io/gh/elixir-cloud-aai/foca/branch/dev/graph/badge.svg?branch=dev>
[badge-docs]: <https://readthedocs.org/projects/foca/badge/>
[badge-github-tag]: <https://img.shields.io/github/v/tag/elixir-cloud-aai/foca?color=C39BD3>
[badge-license]: <https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-pypi]: <https://img.shields.io/pypi/v/foca.svg?style=flat&color=C39BD3>
[badge-url-build-status]: <https://github.com/elixir-cloud-aai/foca/actions/workflows/checks.yml>
[badge-url-coverage]: <https://codecov.io/gh/elixir-cloud-aai/foca?branch=dev>
[badge-url-docs]: <https://foca.readthedocs.io/en/latest/>
[badge-url-github-tag]: <https://github.com/elixir-cloud-aai/foca/releases>
[badge-url-license]: <http://www.apache.org/licenses/LICENSE-2.0>
[badge-url-pypi]: <https://pypi.python.org/pypi/foca>
[config-template]: templates/config.yaml
[config-petstore]: examples/petstore/config.yaml
[docs-access-control]: docs/access_control/README.md
[docs-models]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html>
[docs-models-api]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.APIConfig>
[docs-models-db]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.DBConfig>
[docs-models-exceptions]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.ExceptionConfig>
[docs-models-jobs]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.JobsConfig>
[docs-models-log]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.LogConfig>
[docs-models-security]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.SecurityConfig>
[docs-models-server]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.ServerConfig>
[example]: examples/petstore/README.md
[foca-logo]: images/foca_logo_192px.png
[img-hint]: images/hint.svg
[img-logo-banner]: images/logo-banner.svg
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[res-casbin]: <https://casbin.org/>
[res-celery]: <http://docs.celeryproject.org/>
[res-connexion]: <https://github.com/zalando/connexion>
[res-cors]: <https://flask-cors.readthedocs.io/en/latest/>
[res-elixir-cloud-coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[res-elixir-cloud-contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[res-flask]: <http://flask.pocoo.org/>
[res-flask-app-context]: <https://flask.palletsprojects.com/en/1.1.x/appcontext/>
[res-jwt]: <https://jwt.io>
[res-mongo-db]: <https://www.mongodb.com/>
[res-mongo-db-uri]: <https://www.mongodb.com/docs/manual/reference/connection-string/>
[res-openapi]: <https://www.openapis.org/>
[res-pydantic]: <https://pydantic-docs.helpmanual.io/>
[res-rabbitmq]: <https://www.rabbitmq.com/>
[res-semver]: <https://semver.org/>
[res-swagger]: <https://swagger.io/tools/swagger-ui/>
[res-rfc-9457]: <https://www.rfc-editor.org/rfc/rfc9457>
[res-pymongo]: <https://pymongo.readthedocs.io/>
[res-using-foca]: <https://github.com/elixir-cloud-aai/foca/network/dependents>
[res-yaml]: <https://yaml.org/>
