# Contributing

Your contributions are welcome and necessary. Please use the
[GitHub issue tracker](https://guides.github.com/features/issues/) to:

- Report bugs
- Propose or implement features
- Submit code changes / fixes
- Discuss code

See here for a [short tutorial for GitHub's issue tracking
system](https://guides.github.com/features/issues/).

Please adhere to the [Code of Conduct](CODE_OF_CONDUCT.md).

Please *do not* ask usage questions, installation problems (unless they appear
to be bugs) etc. via the GitHub issue tracker. We have a [slack channel](https://elixir-cloud.slack.com/archives/C013Q13NG74) for that.

## Reporting bugs

Please use the project's
[issue tracker](https://github.com/elixir-cloud-aai/foca/issues) to report
bugs. If you have no experience in filing bug reports, see e.g.,
[these recommendations by the Mozilla Developer Network](https://developer.mozilla.org/en-US/docs/Mozilla/QA/Bug_writing_guidelines)
first. Briefly, it is important that bug reports contain enough detail,
background and, if applicable, _minimal_ reproducible sample code. Tell us
what you expect to happen, and what actually does happen.

## Implementing features and submitting fixes

Kindly use pull requests to submit changes to the code base. But please note
that this project is driven by a community that likes to act on consensus. So
in your own best interest, before just firing off a pull request after a lot of
work, please [open an
issue](https://github.com/elixir-cloud-aai/foca/issues) to **discuss your
proposed changes first**. Afterwards, please stick to the following simple
rules to make sure your pull request will indeed be merged:

1. Clone the repo. **Yes, no need to create a fork.**
1. Create a [_feature
   branch_](https://datasift.github.io/gitflow/IntroducingGitFlow.html) from
   branch `dev`.
3. If you've added code that should be tested, add tests. **The tests should not reduce the code coverage.**
4. Ensure that all tests pass.
5. Document your code and update all relevant documentation.
6. Stick to the code and documentation style (see below).
7. Issue the pull request.


Important: Note that all your contributions are understood to be covered by the
[same license](LICENSE.md) that covers the entire project.

## Code & documentation style

### Python

Please use a recent version of Python 3.6 or higher. Python code, docstring and
comment style loosely follows the
[Google Python Style Guide](https://github.com/google/styleguide/blob/gh-pages/pyguide.md).
Please try to conform to the used style to maintain consistency. Include
[type hints/annotations](https://docs.python.org/3.6/library/typing.html) at
least for function/method signatures.

Please use **all** of the following linters and validation tools with default
settings and ensure that no warnings/errors are reported in any files before you
commit:

- [`pylint`](https://github.com/PyCQA/pylint)
- [`flake8`](https://gitlab.com/pycqa/flake8)
- [`mypy`](https://github.com/python/mypy)