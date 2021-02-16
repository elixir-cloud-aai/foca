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

![Hint][img-hint] **Check out the [petstore example application][example]
shipped with this repository to see FOCA in action!**

## Configuration file

In order to use FOCA functionalities, you must create a [YAML][res-yaml]
configuration file that includes keyword sections reserved by FOCA.

![Hint][img-hint] **In order to get you started writing your own app
configuration, you can copy the [**annotated template**][config-template]
shipped with this repository and modify it.**

For further information on the writing FOCA configuration files, read on.

### Editing your configuration file

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
> key `id`, which is required to be unique.

If you do _not_ want to register a database collection, you can simply omit
that section, but note that once a section is included, it _MUST_ adhere
to the corresponding model described in the [API
documentation][docs-models].

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

### More examples

Apart from the [annotated template][config-template], you can also check
out the [configuration file][config-petstore] of the [petstore app][example]
for another example.

![Hint][img-hint] _**Or why not explore [apps that already use
FOCA][res-using-foca]?**_

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
[example]: examples/README.md
[img-hint]: images/hint.svg
[img-logo-banner]: images/logo-banner.svg
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[res-celery]: <http://celeryproject.org/>
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