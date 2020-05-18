# FOCA - Flask-OpenAPI-Connexion Archetype

[![License][badge-license]][badge-url-license]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]
[![GitHub_tag][badge-github-tag]][badge-url-github-tag]
[![PyPI_release][badge-pypi]][badge-url-pypi]

## Synopsis

Opinionated set of tools/utils for quickly developing OpenAPI-based
microservices with Flask and Connexion.

## Description

FOCA is a [Python package](https://pypi.org/project/foca/) that enables fast development of OpenAPI-based HTTP API microservices in Flask. It includes modules for
* configuration management
* error handling
* database interaction (currently MongoDB)
* JWT validation

## Usage

Install with `pip`:

```bash
pip install foca
```

Import in your code! For example:

```bash
from foca.config.config_parser import YAMLConfigParser
```

## Contributing

This project is a community effort and lives off your contributions, be it in the form of bug reports, feature requests, discussions, or fixes and other code changes. Please read these [guidelines and practices](https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/resources/contributing_guidelines.md) if you want to contribute. And please mind the [code of conduct](https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md) for all interactions with the community.

## License

This project is covered by the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) also
[shipped with this repository](LICENSE).

## Contact

The project is a collaborative effort under the umbrella of the [ELIXIR
Cloud and AAI](https://elixir-europe.github.io/cloud/) group.

Please contact the [project leader](mailto:alexander.kanitz@sib.swiss) for inquiries, proposals, questions etc. that are not covered by the [Contributing](#Contributing) sections.

## References

* <https://elixir-europe.github.io/cloud/>
* <https://www.ga4gh.org/>


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

