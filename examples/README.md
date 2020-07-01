# FOCA - Flask-OpenAPI-Connexion Archetype

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![PyPI_release][badge-pypi]][badge-url-pypi]

## Synopsis

A [petstore][res-petstore] based example application implemented using
[FOCA][res-foca].

## Description

FOCA is a [Python package][res-foca] that enables fast development of
OpenAPI-based HTTP API microservices in [Flask][res-flask]. It includes modules
for:

* configuration management
* error handling
* database interaction (currently [MongoDB][res-mongo-db])
* [JWT][res-jwt] validation

We tried to create an example application that uses the underlying implementation of
[FOCA][res-foca]. For this, we used the parsed the [configuration settings][res-config]
via FOCA. Further the endpoint logic for the example app was defined based on the
[petstore specifications][res-specs] that were specified in the configuration settings. 
All the specs and configurations can be redifined in the [config][res-config] and
[specs][res-specs] files respectively. One can then modify the endpoint logic as per
requirement and run the service.
## Installation

### docker-compose

#### Requirements

Ensure you have the following software installed:

* Docker (19.03.4, build 9013bf583a)
* docker-compose (1.25.5)
* Git (2.17.1)

> These are the versions used for development/testing. Other versions may or
> may not work. Please let us know if you encounter any issues with _newer_
> versions than the listed ones.

#### Instructions

##### Set up environment

Clone repository:

```bash
git clone https://github.com/elixir-cloud-aai/foca.git foca
```

Traverse to example app directory:

```bash
cd foca/examples/
```

##### Optional: Edit/override app config

* Via the **app configuration file**

  ```bash
  vi foca/examples/petstore/config.yaml
  ```

* Via **environment variables**

  A few configuration settings can be overridden by environment variables:

  ```bash
  export <ENV_VAR_NAME>=<VALUE>
  ```

  List of the available environment variables:

  | Variable       | Description             |
  |----------------|-------------------------|
  | MONGO_HOST     | MongoDB host endpoint   |
  | MONGO_PORT     | MongoDB service port    |
  | MONGO_DBNAME   | MongoDB database name   |
  | MONGO_USERNAME | MongoDB client username |
  | MONGO_PASSWORD | MongoDB client password |
  | RABBIT_HOST    | RabbitMQ host endpoint  |
  | RABBIT_PORT    | RabbitMQ service port   |

##### Optional: Edit/override petstore openAPI specs

* Via the **petstore specs file**

  ```bash
  vi foca/examples/petstore/petstore.yaml
  ```

* In case of any changes you'll need to modify the endpoint logic
defined under

  ```bash
  vi foca/examples/petstore/controllers/__init__.py
  ```

###### Build & deploy

Build and run services in detached/daemonized mode:

```bash
docker-compose up -d --build
```

###### Use service

Visit Swagger UI:

```bash
firefox http://localhost:80/
```

You may use the GET/POST endpoints by providing the required/desired values
based on the description given on the swagger UI. 
Leave the rest of the values empty and hit the `Try it out!` button.

You can also use the service through `curl`. For example:
* to send a request to the `GET /pets` endpoint (retrieve all pets):
```console
curl -X GET \
    --header 'Accept: application/json' \
    'http://localhost:80/pets' 
```

* to send a request to the `POST /pets` endpoint (create a new pet):
```console
curl -X GET \
    --header 'Accept: application/json' \
    'http://localhost:80/pets' \
    -d "{\"name\":\"new_2\",\"tag\":\"tag_2\"}"
```

* to send a request to the `GET /pets/{id}` endpoint (retrieve single pet):
```console
curl -X GET \
    --header 'Accept: application/json' \
    'http://localhost:80/pets/1' 
```

* to send a request to the `DELETE /pets/{id}` endpoint (delete specific pet):
```console
curl -X DELETE \
    --header 'Accept: application/json' \
    'http://localhost:80/pets/1' 
```

Check the [API docs][docs-api] to see what's in FOCA.

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contributing
guidelines][res-elixir-cloud-contributing] if you are interested to contribute.
Please mind the [code of conduct][res-elixir-cloud-coc] for all interactions
with the community.

## Versioning

The project adopts the [semantic versioning][res-semver] scheme for versioning.
Currently the service is in beta stage, so the API may change without further
notice.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][org-elixir-cloud]. Follow the link to get in touch with us via chat or
email. Please mention the name of this service for any inquiry, proposal,
question etc.

[badge-build-status]:<https://travis-ci.com/elixir-cloud-aai/foca.svg?branch=dev>
[badge-coverage]:<https://img.shields.io/coveralls/github/elixir-cloud-aai/foca>
[badge-github-tag]:<https://img.shields.io/github/v/tag/elixir-cloud-aai/foca?color=C39BD3>
[badge-license]:<https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-pypi]:<https://img.shields.io/pypi/v/foca.svg?style=flat&color=C39BD3>
[badge-url-build-status]:<https://travis-ci.com/elixir-cloud-aai/foca>
[badge-url-coverage]:<https://coveralls.io/github/elixir-cloud-aai/foca>
[badge-url-github-tag]:<https://github.com/elixir-cloud-aai/foca/releases>
[badge-url-license]:<http://www.apache.org/licenses/LICENSE-2.0>
[badge-url-pypi]:<https://pypi.python.org/pypi/foca>
[docs-api]: <https://foca.readthedocs.io/en/latest/>
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[res-elixir-cloud-coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[res-elixir-cloud-contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[res-connexion]: <https://github.com/zalando/connexion>
[res-flask]: <http://flask.pocoo.org/>
[res-foca]: <https://pypi.org/project/foca/>
[res-jwt]: <https://jwt.io>
[res-mongo-db]: <https://www.mongodb.com/>
[res-open-api]: <https://www.openapis.org/>
[res-semver]: <https://semver.org/>
[res-petstore]: <https://petstore.swagger.io/>
[res-specs]: <https://github.com/elixir-cloud-aai/foca/blob/dev/examples/petstore/petstore.yaml>
[res-config]: <https://github.com/elixir-cloud-aai/foca/blob/dev/examples/petstore/config.yaml>