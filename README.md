# FOCA - Flask-OpenAPI-Connexion Archetype

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![PyPI_release][badge-pypi]][badge-url-pypi]

## Synopsis

Opinionated set of tools/utils for quickly developing
[OpenAPI][res-open-api]-based microservices with [Flask][res-flask] and
[Connexion][res-connexion].

## Description

FOCA is a [Python package][res-foca] that enables fast development of
OpenAPI-based HTTP API microservices in [Flask][res-flask]. It includes modules
for:

* configuration management
* error handling
* database interaction (currently [MongoDB][res-mongo-db])
* [JWT][res-jwt] validation

## Usage

Install with `pip`:

```bash
pip install foca
```

Import in your code! For example:

```bash
from foca.config.config_parser import YAMLConfigParser
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
