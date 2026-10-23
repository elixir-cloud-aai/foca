# Contributing

This project is a community effort and lives off your contributions, be it
bug reports, feature requests, discussions, documentation or code. It follows
the [contributor guide of ELIXIR
On-Cloud](https://elixir-cloud-aai.github.io/contribute/), which also
describes the project's values and what it offers regular contributors; this
page sums up how it applies to FOCA, the framework that apps such as
[Cloud Registry](https://github.com/elixir-cloud-aai/cloud-registry) are built
with.

Please follow the [code of
conduct](https://github.com/elixir-cloud-aai/foca/blob/dev/CODE_OF_CONDUCT.md)
in all interactions with the community. To report a violation, email
[cloud-service@elixir-europe.org](mailto:cloud-service@elixir-europe.org).

Report vulnerabilities privately, not in issues, as described in the
[security
policy](https://github.com/elixir-cloud-aai/foca/blob/dev/SECURITY.md).

## Getting started

1. Find an issue in the [issue
   tracker](https://github.com/elixir-cloud-aai/foca/issues), or open one (see
   [Issues](#issues)).
2. Join the ELIXIR On-Cloud chat and introduce yourself in the open source
   community channel: your skills and experience, your GitHub username and
   the issues you are interested in (see the
   [workflow](https://elixir-cloud-aai.github.io/contribute/workflow/); send
   a direct message to a project lead instead if you don't want to share
   these publicly). Once you are a member of the GitHub organization, you can
   assign issues to yourself.
3. Set up a development environment (see [Development](#development)) and
   start coding.

One-time contributors can skip the second step and open pull requests from
forks; some checks may need a maintainer's approval to run for them.

## Issues

Use the issue tracker to propose features, report bugs and discuss existing
issues; use the chat for questions and informal discussions. Write issues
concisely, but with enough detail and context (links, logs, screenshots) for
others to start working on them; bug reports need steps to reproduce, ideally
a minimal app configuration. The maintainers add labels and milestones.

**Always work on an issue:** before you change code, make sure there is an
issue for it and that the maintainers agree with the approach, so that no work
is in vain.

## Branches and pull requests

The project follows a variant of the GitHub flow: all changes reach the
development branch `dev` through pull requests, which are reviewed and
tested; `main` only moves forward to released commits (see [Versioning and
releases](#versioning-and-releases)).

1. Create a feature branch from an up-to-date `dev`, named after the type and
   topic of the change, e.g., `fix/<topic>` or `docs/<topic>`.
2. Commit your changes, push the branch and open a pull request against `dev`
   that refers to the issue (`Resolves #<issue>`) and says what changes, why,
   and how you tested it. Pull requests into `dev` don't close issues
   automatically; the maintainers close them after the merge.
3. Request a review: every change is reviewed by at least one other person.
   Address the comments by pushing further commits (there may be several
   rounds).
4. Once the checks pass and all conversations are resolved, the maintainers
   squash-merge the pull request into `dev`.

Keep pull requests focused: if a change can't be summarized in a title of 50
characters, split it into several pull requests. Feature branches are unstable:
don't build on other people's branches without talking to them first.

## Commit messages and pull request titles

Pull request titles, which become the commit messages on `dev`, follow the
[Conventional Commits](https://www.conventionalcommits.org/) specification:

```text
<type>[(<scope>)][!]: <description>
```

- The whole title line has at most 50 characters.
- The description starts with a verb in the imperative mood (e.g., `add`,
  `fix`) and has no trailing period.
- `!` (and a `BREAKING CHANGE:` footer in the body) marks breaking changes.
- The type names the predominant reason for the change:

| Type | Changes |
| --- | --- |
| `feat` | new functionality |
| `fix` | bug fixes |
| `perf` | performance improvements |
| `refactor` | code changes that neither add features nor fix bugs |
| `style` | formatting, docstrings and type hints of the code |
| `build` | build system, packaging and dependencies |
| `ci` | CI and release workflows |
| `docs` | documentation |
| `test` | tests |
| `chore` | other maintenance, e.g., `.gitignore` |
| `revert` | reverts of earlier commits |

Use `feat`, `fix`, `perf`, `refactor` and `style` only for changes of what is
released: the package (`foca/`), its Docker image (`docker/`) and the Helm
library chart (`charts/foca-common/`, outside its tests). Commit messages on
feature branches may follow the same format, which makes the history easier
to review.

## Development

FOCA supports Python 3.11 and later (`requires-python` in `pyproject.toml`).
To set up a development environment (e.g., in a virtual environment) and run
the checks that CI runs:

```bash
pip install -e ".[dev]"  # the package, with the dev dependencies
pre-commit install  # run the hooks on every commit
pre-commit run --all-files  # ruff, semgrep, ast-grep, YAML, TOML, whitespace
mypy  # type checks of the package and the tests
coverage run -m pytest
coverage report -m
```

The tests marked `mongodb` need a MongoDB server on `localhost:12345`, e.g.:

```bash
docker run -d --rm --name mongodb-test -p 127.0.0.1:12345:27017 mongo:7.0
coverage run -m pytest
docker stop mongodb-test
```

To skip them instead, run `pytest -m "not mongodb"`; coverage is then below
100%.

CI (`.github/workflows/checks.yml`) runs the unit tests with Python 3.11,
3.12, 3.13 and 3.14 against MongoDB 7.0, and with Python 3.13 against MongoDB
8.0; once more with the lowest versions of the direct dependencies that
`pyproject.toml` allows (Python 3.11), so that their lower bounds stay
honest; and integration tests against the [petstore
example](https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore),
built from the FOCA image of the repository. To run the integration tests
locally:

```bash
cd examples/petstore
docker compose build foca-petstore-root  # FOCA image
docker compose up -d --build
cd ../..
# Wait until the app accepts requests
until curl -s -o /dev/null http://localhost:8080/; do sleep 2; done
pytest tests/integration_tests.py
```

The tests of the Helm library chart need [Helm](https://helm.sh/) and run
separately (`pytest charts/foca-common/tests`); CI
(`.github/workflows/charts.yml`) also installs the petstore example with its
chart on a kind cluster and checks that changes of the library chart raise
its version (see [Developing the library
chart](helm-chart.md#developing-the-library-chart)).

Run `pre-commit` in the environment with the dev dependencies: the ast-grep
hooks use the installed package. The first run sets up the other hooks, which
takes a few minutes.

The lint job also builds the documentation (see [Documentation](#documentation))
and the distributions, and checks that the ruff and semgrep versions of the
pre-commit hooks match the ones pinned in `requirements/lint.txt` and
`requirements/semgrep.txt` (update both together). semgrep is pinned in a
file of its own, as its dependencies conflict with FOCA's: install it in a
separate environment to run it outside pre-commit.

### Image scan

CI (`checks.yml`) builds the Docker images of all Python versions for every
pull request and push and scans them with [Trivy](https://trivy.dev/)
(`.github/actions/scan-image`); the release workflow builds and scans the
release commit's images again before publishing anything (in a job without
its credentials), and a weekly workflow (`scan.yml`) scans the images of the
latest release. They fail on critical and high vulnerabilities that have a
fix, and upload all findings to code scanning (Security tab; not for pull
requests from forks, nor in the release workflow). The publishing workflows
take the Docker Hub organization from the secret `DOCKERHUB_ORG`; the weekly
scan, which needs no secrets, from the repository variable of that name (by
default `elixircloud`), so forks set both. If a scan fails:

1. Fix the vulnerability if FOCA can: raise the lower bound of the dependency
   in `pyproject.toml` or update the base image (Dependabot's PRs do both).
2. Otherwise (e.g., no fixed version yet, or a vulnerability that does not
   affect FOCA), accept it in `.trivyignore.yaml` with the affected package
   versions (`purls`), the reason (`statement`) and an expiry date
   (`expired_at`), after which the scans fail again, so that the decision is
   reviewed. Trivy also scans for secrets (critical and high ones fail the
   scans too); accept those under `secrets:` in the same file.
3. For a released image, release a patch version once the fix is on `dev`.

The weekly scan uses the `.trivyignore.yaml` of `dev`, so that accepting a
finding on `dev` also applies to the released images. Dependabot updates the
actions, but not the Trivy version (`version:` in
`.github/actions/scan-image/action.yml`): bump it by hand. The setup is the
same as in [cloud-registry](https://github.com/elixir-cloud-aai/cloud-registry).

## Citation metadata

FOCA is cited with the metadata in
[`CITATION.cff`](https://github.com/elixir-cloud-aai/foca/blob/dev/CITATION.cff)
([Citation File Format](https://citation-file-format.github.io/)), which
GitHub ("Cite this repository") and Zenodo use. Its authors are the people
who committed at least 100 lines (lines added) to `main`, the release
branch, not counting bots, vendored or generated files and the reformatting
commits in `.git-blame-ignore-revs`. They are ordered by lines, then by
commits; equal contributors are listed alphabetically by family name and
marked with an asterisk (*). Affiliations come from public ORCID records
only. To add or correct your entry (e.g., your name, [ORCID
iD](https://orcid.org/) or affiliation), open a pull request that changes
only your entry; don't add e-mail addresses. CI validates the file
(`cffconvert --validate`). Before each release, `python scripts/citation.py
set X.Y.Z` sets its `version` and `date-released`, and the release workflow
fails if they don't match the release (`python scripts/citation.py check
X.Y.Z`; see [Versioning and releases](#versioning-and-releases)).

## Versioning and releases

The project adopts [semantic versioning](https://semver.org/). While the
major version is 0, minor releases may contain breaking changes. The version
is derived from git tags (via
[setuptools-scm](https://setuptools-scm.readthedocs.io/)) and available as
`foca.__version__`; builds between releases get development versions (e.g.,
`0.14.1.dev3+g1a2b3c4`).

Features and fixes are merged into `dev`. Branch `main` only ever moves
forward to commits on `dev` that are released. First, shortly before the
release, set its version and date in the citation metadata, which Zenodo
archives with each release, and merge the change into `dev`; each version,
also each pre-release (e.g., `X.Y.Zrc1`), needs its own such change:

```bash
python scripts/citation.py set X.Y.Z  # date: today (UTC), or --date YYYY-MM-DD
```

Then, to release version `X.Y.Z` from commit `<sha>` on `dev` (with that
change), after CI passed for it:

```bash
git switch --detach <sha>
python scripts/citation.py check X.Y.Z  # as the release workflow does
git push origin <sha>:main  # fails unless it is a fast-forward
git tag -s vX.Y.Z <sha> -m "vX.Y.Z"
git push origin vX.Y.Z
```

Pushing the tag triggers the [release
workflow](https://github.com/elixir-cloud-aai/foca/blob/dev/.github/workflows/release.yml),
which checks that the tagged commit is on `main` and that its `CITATION.cff`
has the release version and a date that is not in the future (UTC),
publishes the distributions to PyPI (after approval in the `pypi`
environment), pushes, attests and signs Docker images tagged `X.Y.Z` and
`latest`, and creates a GitHub release with generated notes. Tags of alpha,
beta and release candidate versions (e.g., `vX.Y.Zrc1`) do not update
`latest`. These and, until 1.0.0, all other releases are GitHub pre-releases
(never marked as the latest release).

A tag ruleset allows only admins to create `v*` and `chart-v*` tags and
blocks updates and deletions of these tags for everyone, and GitHub releases
are immutable. If a release fails, re-run its workflow for the same tag; if
the tagged commit itself is wrong, release a new version.

The Helm library chart `foca-common` (`charts/foca-common`) is versioned and
released separately, from signed tags `chart-vX.Y.Z`; see [releases of the
library chart](helm-chart.md#releases).

## Python guidelines

Most of the following is checked by pre-commit and CI; the rest is checked in
reviews.

- **Packaging:** the package, its dependencies and the configuration of the
  tools are in `pyproject.toml`; runtime dependencies in
  `project.dependencies`, with lower bounds that the lowest-dependencies job
  tests, and those for development and the documentation in the extras `dev`
  and `docs`. The versions of the lint and release tools are pinned in
  `requirements/`. Don't add `setup.py`.
- **Compatibility:** FOCA is a library: code must work with all supported
  Python versions (ruff's `target-version` and mypy's `python_version` are
  the lowest) and dependency versions. Changes that break apps need a `!` in
  the title and an entry in the migration notes of the next release.
- **Formatting and linting:** [ruff](https://docs.astral.sh/ruff/) formats
  the code (`ruff format`, compatible with Black, 88 characters per line) and
  lints it (`ruff check`) with the rule set shared by FOCA and FOCA apps:
  pycodestyle, Pyflakes, isort, flake8-bugbear, flake8-comprehensions,
  flake8-simplify, Pylint, Ruff's own rules, pyupgrade, bandit (security),
  logging format and pydocstyle (`pyproject.toml`, where each ignored rule
  says why). Prefer explicit checks to suppressions (`# noqa`,
  `# type: ignore`); justify the rare exceptions in a comment on the line.
- **Further checks:** [semgrep](https://semgrep.dev/) checks for security
  issues that ruff doesn't cover (rules in `.semgrep/`, e.g., JWTs decoded
  without verification) and [ast-grep](https://ast-grep.github.io/) for
  anti-patterns (rules in `.ast-grep/`, e.g., the root logger or `print()` in
  the package), each rule with tests (`.semgrep/foca.py` and
  `.semgrep/foca.test.yaml`, `.ast-grep/rule-tests/`). Rules shared with FOCA
  apps (e.g., Cloud Registry) are kept identical there, except for the files
  they apply to. Suppress a false
  positive with `# nosemgrep: <rule-id>` or `# ast-grep-ignore: <rule-id>` on
  the line before, with the reason in a comment.
- **Docstrings:** Google-style docstrings at all levels: packages, modules,
  classes, methods and functions, also private ones and tests, with a
  one-line summary and, where applicable, the sections `Args`, `Returns` (or
  `Yields`), `Raises` and, for classes, `Attributes`; consider `Examples` and
  `Note`. Ruff's pydocstyle rules (Google convention) check those of public
  objects and their `Args`; reviews check the others. The [API
  reference](reference.md) is generated from them.
- **Type hints:** complete type hints for all functions and methods (also of
  the tests), including `*args` and `**kwargs`, and generic types with their
  parameters (e.g., `dict[str, Any]`, not `dict`); add hints to module-level
  variables and to local variables whose type isn't obvious.
  [mypy](https://mypy.readthedocs.io/) checks the hints, and that functions
  and generic types have them (`disallow_untyped_defs`,
  `disallow_any_generics`); reviews check those of variables. The package is
  typed (`py.typed`), so apps rely on its hints.
- **Tests:** [pytest](https://docs.pytest.org/), with coverage measured by
  [coverage](https://coverage.readthedocs.io/): keep it at 100% of `foca`,
  which is enforced: `coverage report` (and CI) fails below 100%
  (`fail_under` in `pyproject.toml`), and so do Codecov's checks of the whole
  code and of the changes of a pull request (`codecov.yml`). Add tests that
  fail without your change.

## Documentation

Update the documentation with every change that affects it: the pages in
`docs/`, the README and the docstrings. Describe user-visible changes in the
pull request; breaking changes also in the migration notes of the next
release (`docs/migration-<version>.md`).

This documentation is built with [MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/), from
`docs/` and `mkdocs.yml`, by Read the Docs and in CI; warnings, e.g., of
broken links, fail the build. The [API reference](reference.md) is generated
from the docstrings with [mkdocstrings](https://mkdocstrings.github.io/). To
build it locally, or serve it with live reloading:

```bash
pip install -e ".[docs]"
mkdocs build --strict  # into `site/`, ignored by Git
mkdocs serve
```
