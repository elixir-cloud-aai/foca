# FOCA

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Docs][badge-docs]][badge-url-docs]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![PyPI_release][badge-pypi]][badge-url-pypi]

_Quickly develop Flask microservices!_

## Description

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

## Usage

(1) Install the [FOCA package][badge-url-pypi] with `pip`:

```bash
pip install foca
```

(2) Create a [configuration file](#configuration-file).

(3) Import the FOCA main function `foca()` and pass your config:

```python
from foca import foca

app = foca("path/to/my/app/config.yaml")  # returns a Connexion app instance
```

(4) Start your [Flask][res-flask] app as usual.

![Hint][img-hint] _**Check out the [Petstore example application][example]
shipped with this repository to see FOCA in action!**_

## Configuration file

In order to use FOCA functionalities, you must create a [YAML][res-yaml]
configuration file that includes keyword sections reserved by FOCA.

![Hint][img-hint] _**In order to get you started writing your own app
configuration, you can copy the [**annotated template**][config-template]
shipped with this repository and modify it.**_

For further information on the writing FOCA configuration files, read on.

### Writing your configuration file

FOCA provides support to adhere to multiple application configurations.
Keywords reserved by FOCA include the following (exhaustive; follow links to
corresponding models):

* [`api`][docs-models-api]
* [`db`][docs-models-db]
* [`exceptions`][docs-models-exceptions]
* [`jobs`][docs-models-jobs]
* [`log`][docs-models-log]
* [`security`][docs-models-security]
* [`server`][docs-models-server]

Any values passed to reserved keywords are automatically validated and a
corresponding informative exception will be raised whenever a value does not
adhere to the corresponding model.

Any top-level sections that are _not_ listed above will simply be passed to the
app instance returned by the `foca()` function. All configuration parameters,
reserved by FOCA _and_ any custom ones, will be available in the [application
context][res-flask-app-context] as attributes of `current_app.config['FOCA']`.
If you do _not_ want to register a section, you can simply omit it, but note
that once a section is included, it _MUST_ adhere to the corresponding model
described in the [API documentation][docs-models].

#### Writing API Configuration

This configuration is utilised to specify the OpenAPI specification of your
application.

```yaml
api:
  specs:
    - path:
        - path/to/my/openapi/specs.yaml
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
```

> This config would introduce your OpenAPI specifications elaborated under the
> `path/to/my/openapi/specs.yaml` file. FOCA supports multiple specification
> paths. Hence, if you have more than one specification file, you may simple
> add `path/to/my/second/openapi/specs.yaml` under the `path` list. The
> `x-openapi-router-controller` represents the path to corresponding API
> request controllers. Support to introduce a token validator can also be
> found under `add_security_fields`. You may also enable and disable auth,
> swagger etc.

#### Writing Database Configuration

For example, if you want to register a [MongoDB][res-mongo-db] database
collection, your configuration file must include the top-level `database`
keyword section, e.g.:

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
```

> This config would create a MongoDB database `myDb` with collection
> `myCollection` in your database server. The collection would be indexed by
> key `id`, which is required to be unique. To register another collection,
> simply add another named `CollectionConfig` object as a child to
> `collections`, e.g., `yourCollection`, with its own `indexes` etc.

#### Writing Exceptions Configuration

FOCA provides support to add configurable exception handlers. Basic config
can be found in the below, for possible extensions refer the
[`exceptions`][docs-models-exceptions] docs.

```yaml
exceptions:
  required_members: [['msg'], ['status']]
  status_member: ['status']
  exceptions: my_app.exceptions.exceptions
```

> This config would attach the configured exceptions defined in the
> `my_app.exceptions.exceptions` file. Every error response will have be
> characterized by a `msg` and `status`, while `status` will represent the
> API response status. You may configure list of `public_members` and
> `private_members`. Non required/optional can be added under the
> `extension_members` list.

#### Writing Jobs Configuration

You may add your `celery` and `rabbitmq` broker configurations for runing
specific tasks required tasks to be attached to your application. For example,
following represents the broker instance and attached task definition.

```yaml
jobs:
  host: rabbitmq
  port: 5672
  backend: 'rpc://'
  include:
    - my_app.tasks.my_task_1
    - my_app.tasks.my_task_2
```

> This config attaches the `rabbitmq` broker host running on port `5672` with
> two tasks viz., `my_task_1` and `my_task_2`. Similary the `host` and `port`
> for `celery` can also be introduced.

#### Writing Log Configuration

You can also configure error/info logging for you application by properly
defining log handlers and log formatters.

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

> This config would configure the application logging as per the above defined
> formatter and handler objects.

#### Writing Security Configuration

A layer of security authentication can also be added as a part of the application
configuration. `auth` and `cors` can be altered here.

```yaml
security:
  auth:
    add_key_to_claims: True
    algorithms:
      - RS256
    allow_expired: False
    audience: null
    validation_methods:
      - userinfo
      - public_key
    validation_checks: any
  cors:
    enabled: True
```

> This config specify the auth params including `algorithms`,
> `validation_methods` and `validation_checks`. The allowed enum values are
> mentioned under the [`security`][docs-models-security] docs. Further the
> `CORS` enable support can also be set at the config level.

#### Writing Server Configuration

For example, if you want to register a given server environment for your
application, your configuration file must include the top-level `server`
keyword section, e.g.:

```yaml
server:
  host: '0.0.0.0'
  port: 8080
  debug: True
  environment: development
  testing: False
  use_reloader: False
```

> This config would create an application server hosted at `0.0.0.0` and port
>`8080` with debugger on. You may further change the other server variables as
> per requirement.

#### Writing Additional Configurations

FOCA provides support to add additional config parameters. The can be added
under the configuration file. There params will be available in app context as
attributes of `current_app.config['FOCA']` but are **NOT VALIDATED VIA FOCA**.
If desired, custom validations can be added at application level.

```yaml
my_custom_param: 'some_value'
```

### More examples

Apart from the [annotated template][config-template], you can also check
out the [configuration file][config-petstore] of the [Petstore app][example]
for another example.

![Hint][img-hint] _**Or why not explore [apps that already use
FOCA][res-using-foca]?**_

## Extended Support and Usage of FOCA

### Accessing FOCA- and app-specific params withing app

Once the application is created using `foca`, one can easily access the config
variables inside the [application context][res-flask-app-context].

```python
from flask import current_app

app_config = current_app.config["FOCA"]

db = app_config.db
api = app_config.api
server = app_config.server
exceptions = app_config.exceptions
security = app_config.security
jobs = app_config.jobs
log = app_config.log
app_specific_param = current_app.config["app_specific_param"]
```

### Utility/Helper function support

#### Database Utilities

FOCA provides support for the following basic database controllers.

* Fetch latest object given the db `collection`.

```python
from foca.utils.db import find_one_latest

latest_object = find_one_latest("your_db_collection_instance")
```

* Fetch latest object identifier (`id`) given the db `collection`.

```python
from foca.utils.db import find_id_latest

latest_object_id = find_id_latest("your_db_collection_instance")
```

#### Logging Utilities

FOCA provides support for developing endpoints in a manner such that logging requests
and responses can be manually changed at individual endpoint level, thus providing
granularity to the end user.

```python
from foca.utils.logging import log_traffic

@log_traffic(log_request=True, log_response=True, log_level=2)
def your_controller():
    pass
```

> The above decorater will log both request and response with the given `log_level=2`
> specification,

#### Other helpers

FOCA provides support for some common functions that can be utilised by the end user.
Currently only one helper to generate a random object identifier is provided as a part
of the package.

```python
import string

from foca.utils.misc import generate_id

obj_id = generate_id(charset=string.digits, length=6)
```

> The above function processes and returns a random `obj_id` of length `6` and with
> characters consisting of digits (`string.digits`).

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

[badge-build-status]: <https://travis-ci.com/elixir-cloud-aai/foca.svg?branch=dev>
[badge-coverage]: <https://img.shields.io/coveralls/github/elixir-cloud-aai/foca>
[badge-docs]: <https://readthedocs.org/projects/foca/badge/>
[badge-github-tag]: <https://img.shields.io/github/v/tag/elixir-cloud-aai/foca?color=C39BD3>
[badge-license]: <https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-pypi]: <https://img.shields.io/pypi/v/foca.svg?style=flat&color=C39BD3>
[badge-url-build-status]: <https://travis-ci.com/elixir-cloud-aai/foca>
[badge-url-coverage]: <https://coveralls.io/github/elixir-cloud-aai/foca>
[badge-url-docs]: <https://foca.readthedocs.io/en/latest/>
[badge-url-github-tag]: <https://github.com/elixir-cloud-aai/foca/releases>
[badge-url-license]: <http://www.apache.org/licenses/LICENSE-2.0>
[badge-url-pypi]: <https://pypi.python.org/pypi/foca>
[config-template]: templates/config.yaml
[config-petstore]: examples/petstore/config.yaml
[docs-models]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html>
[docs-models-api]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.APIConfig>
[docs-models-db]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.DBConfig>
[docs-models-exceptions]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.ExceptionConfig>
[docs-models-jobs]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.JobsConfig>
[docs-models-log]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.LogConfig>
[docs-models-security]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.SecurityConfig>
[docs-models-server]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.ServerConfig>
[example]: examples/petstore/README.md
[img-hint]: images/hint.svg
[img-logo-banner]: images/logo-banner.svg
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[res-celery]: <http://docs.celeryproject.org/>
[res-connexion]: <https://github.com/zalando/connexion>
[res-cors]: <https://flask-cors.readthedocs.io/en/latest/>
[res-elixir-cloud-coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[res-elixir-cloud-contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[res-flask]: <http://flask.pocoo.org/>
[res-flask-app-context]: <https://flask.palletsprojects.com/en/1.1.x/appcontext/>
[res-foca]: <https://pypi.org/project/foca/>
[res-jwt]: <https://jwt.io>
[res-mongo-db]: <https://www.mongodb.com/>
[res-openapi]: <https://www.openapis.org/>
[res-rabbitmq]: <https://www.rabbitmq.com/>
[res-semver]: <https://semver.org/>
[res-using-foca]: <https://github.com/elixir-cloud-aai/foca/network/dependents>
[res-yaml]: <https://yaml.org/>
