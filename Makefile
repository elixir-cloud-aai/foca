# NOTE: This Makefile assumes that dependencies are installed and, if a virtual
# environment is used, it is activated.

## Variables ##################################################################
# NOTE: Define any variables here if needed in the future

## Documentation ##############################################################
# NOTE: Keep all the targets in alphabetical order for better readability.

default: help

.PHONY: help
help:
	@echo "\nUsage: make [target] ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
	@echo "Available targets:\n"

	@echo "Code Quality ------------------------------------------------------------------"
	@echo "  \033[1m\033[35mformat-lint\033[0m \033[37m(fl)\033[0m: \033[36mRun linter, formatter, spellcheck.\033[0m"
	@echo "  \033[1m\033[35mtype-check\033[0m \033[37m(tc)\033[0m: \033[36mPerform type checking.\033[0m\n"

	@echo "Environment Management --------------------------------------------------------"
	@echo "  \033[1m\033[35mclean-venv\033[0m \033[37m(cv)\033[0m: \033[36mRemove virtual environment.\033[0m"
	@echo "  \033[1m\033[35minstall\033[0m \033[37m(i)\033[0m: \033[36mInstall app and dependencies.\033[0m"
	@echo "  \033[1m\033[35mvenv\033[0m \033[37m(v)\033[0m: \033[36mCreate virtual environment.\033[0m\n"

	@echo "Testing -----------------------------------------------------------------------"
	@echo "  \033[1m\033[35mtest\033[0m \033[37m(t)\033[0m: \033[36mRun all tests.\033[0m\n"

.PHONY: clean-venv cv
clean-venv:
	@echo "\nRemoving the virtual environment ++++++++++++++++++++++++++++++++++++++++++++++\n"
	@rm -rf .venv

cv: clean-venv

.PHONY: format-lint fl
format-lint:
	@echo "\nRunning linter and formatter using ruff and typos +++++++++++++++++++++++++++++\n"
	@poetry run ruff format && poetry run ruff check --fix
	@typos .

fl: format-lint

.PHONY: install i
install:
	@echo "\nInstalling this package its dependencies +++++++++++++++++++++++++++++++++\n"
	@poetry install --with=code_quality,docs,test,types

i: install

.PHONY: test t
test:
	@echo "\nRunning tests using pytest ++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
	@poetry run pytest tests/

t: test

.PHONY: type-check tc
type-check:
	@echo "\nPerforming type checking with mypy ++++++++++++++++++++++++++++++++++++++++++++\n"
	@poetry run mypy foca

tc: type-check

.PHONY: venv v
venv:
	@echo "\nCreating a virtual environment ++++++++++++++++++++++++++++++++++++++++++++++++\n"
	@python -m venv .venv
	@echo "\nSummary +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
	@echo "Virtual environment created successfully."
	@echo "To activate the environment for this shell session, run:"
	@echo "source .venv/bin/activate"

v: venv
