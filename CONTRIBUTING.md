# Contributing

Thanks for your interest in FOCA! Bug reports, feature requests, discussions,
documentation and code are all welcome. The project follows the [contributor
guide of ELIXIR On-Cloud](https://elixir-cloud-aai.github.io/contribute/); in
short:

- Follow our [code of conduct](CODE_OF_CONDUCT.md).
- Work on [issues](https://github.com/elixir-cloud-aai/foca/issues): open or
  find one and agree on the approach before you change code.
- Branch off `dev` and open a pull request against `dev` that resolves the
  issue; it is squash-merged once the checks pass and it has been reviewed.
- Title pull requests as [Conventional
  Commits](https://www.conventionalcommits.org/)
  (`<type>[(<scope>)][!]: <description>`, at most 50 characters).
- Set up a development environment with `pip install -e ".[dev]"` and run the
  checks (`pre-commit run --all-files`, `mypy`, `coverage run -m pytest`).
- Keep the checks green and the coverage at 100%, with docstrings, type hints
  and the pre-commit hooks; update the documentation.

**See [Contributing in the
documentation](https://foca.readthedocs.io/en/latest/contributing/) for the
details**, including the development setup, the tests (Python versions,
MongoDB, Helm charts) and the Python guidelines.
