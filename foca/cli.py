"""Command-line tool for admin tasks of FOCA apps.

Run admin tasks once, e.g., before a deployment, rather than at the startup of each
worker of each replica. Exit codes are 0 on success, 1 on errors of the
configuration or the task, and 2 on errors of usage.
"""

import argparse
import importlib
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import yaml

from foca.foca import Foca
from foca.security.access_control.register_access_control import sync_policies
from foca.utils.urls import _get_apis, _RegisteredApi

APP_ENV_VAR = "FOCA_APP"


class _UsageError(Exception):
    """Error of how the tool was called."""


def _load_app(app: str) -> Foca:
    """Load the `Foca` instance of an app.

    Args:
        app: Where to find it, as `module:attribute`, naming a `Foca` instance or a
            function returning one. The current directory is searched for the
            module, too.

    Returns:
        The `Foca` instance.

    Raises:
        _UsageError: `app` isn't valid, or doesn't name a `Foca` instance or a
            function returning one.
    """
    module_name, separator, attribute = app.partition(":")
    if not (module_name and separator and attribute):
        raise _UsageError(f"'{app}' is not of the form 'module:attribute'")
    sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Not if the module was found, but something it imports wasn't
        if exc.name is None or not f"{module_name}.".startswith(f"{exc.name}."):
            raise
        raise _UsageError(f"module '{module_name}' not found") from exc
    if not hasattr(module, attribute):
        raise _UsageError(f"module '{module_name}' has no attribute '{attribute}'")
    value = getattr(module, attribute)
    if callable(value) and not isinstance(value, Foca):
        value = value()
    if not isinstance(value, Foca):
        raise _UsageError(f"'{app}' is neither a Foca instance nor returns one")
    return value


def _create_app_without_writes(foca: Foca) -> Any:
    """Create the app, running startup checks, but writing nothing to the database.

    Args:
        foca: `Foca` instance, whose startup switches are turned off.

    Returns:
        Connexion app.
    """
    if foca.conf.db is not None:
        foca.conf.db.create_indexes_on_startup = False
    foca.conf.security.access_control.sync_policies_on_startup = False
    return foca.create_app()


def _check_config(foca: Foca, _args: argparse.Namespace) -> None:
    """Run the startup checks of the app and, if it has jobs, of its workers.

    Args:
        foca: `Foca` instance.
        _args: Arguments of the command.
    """
    _create_app_without_writes(foca)
    if foca.conf.jobs:
        foca.create_celery_app()
    sys.stdout.write("Configuration is valid.\n")


def _create_indexes(foca: Foca, _args: argparse.Namespace) -> None:
    """Create the configured indexes.

    Args:
        foca: `Foca` instance.
        _args: Arguments of the command.
    """
    foca.create_indexes()
    sys.stdout.write("Indexes created.\n")


def _sync_policies(foca: Foca, _args: argparse.Namespace) -> None:
    """Replace the stored configured policy rules with the configured ones.

    Args:
        foca: `Foca` instance.
        _args: Arguments of the command.

    Raises:
        ValueError: Access control isn't enabled.
    """
    access_control = foca.conf.security.access_control
    if not access_control.enabled:
        raise ValueError("Access control is not enabled")
    app = _create_app_without_writes(foca)
    sync_policies(app.app, access_control)
    sys.stdout.write("Policies synchronized.\n")


def _select_spec(apis: list[_RegisteredApi], name: str | None) -> _RegisteredApi:
    """Select the API whose specification to export.

    Args:
        apis: APIs of the app's specifications.
        name: Name or base path of the specification, if given.

    Returns:
        The API.

    Raises:
        ValueError: The app has no specifications, or none with that name.
        _UsageError: The app has several specifications, and none was selected.
    """
    if not apis:
        raise ValueError("The app has no API specifications")
    names = ", ".join(api.name or api.base_path or "/" for api in apis)
    if name is None:
        if len(apis) > 1:
            raise _UsageError(
                f"the app has several specifications; use --spec: {names}"
            )
        return apis[0]
    for api in apis:
        if name in (api.name, api.base_path or "/"):
            return api
    raise ValueError(f"The app has no specification '{name}', but: {names}")


def _export_spec(foca: Foca, args: argparse.Namespace) -> None:
    """Write a merged API specification, as served, to the standard output.

    Args:
        foca: `Foca` instance.
        args: Arguments of the command.
    """
    app = _create_app_without_writes(foca)
    apis = [api for api in _get_apis(app.app) if api.is_app_api]
    # Keeps it in the types of JSON, also for YAML, e.g., for status codes
    text = json.dumps(_select_spec(apis, args.spec).specification, default=str)
    if args.format == "json":
        sys.stdout.write(f"{text}\n")
    else:
        sys.stdout.write(yaml.safe_dump(json.loads(text), sort_keys=False))


def _create_parser() -> argparse.ArgumentParser:
    """Create the parser of the arguments of the tool.

    Returns:
        Parser.
    """
    parser = argparse.ArgumentParser(prog="foca", description=__doc__)
    parser.add_argument(
        "--app",
        help=(
            "The app, as 'module:attribute', naming a Foca instance or a function "
            f"returning one (default: environment variable {APP_ENV_VAR})"
        ),
    )
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)
    for name, function, description in (
        (
            "check-config",
            _check_config,
            "Load and validate the configuration and run the startup checks, "
            "without writing to the database.",
        ),
        ("create-indexes", _create_indexes, "Create the configured indexes."),
        (
            "sync-policies",
            _sync_policies,
            "Replace the stored configured policy rules with the configured ones, "
            "as on startup.",
        ),
        (
            "export-spec",
            _export_spec,
            "Write a merged API specification, as served, to the standard output.",
        ),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.set_defaults(function=function)
        if name == "export-spec":
            command.add_argument(
                "--format",
                choices=["yaml", "json"],
                default="yaml",
                help="Format of the output (default: %(default)s)",
            )
            command.add_argument(
                "--spec",
                help=(
                    "Name in 'api.specs', or base path, of the specification; "
                    "required if the app has several"
                ),
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tool.

    Args:
        argv: Arguments; if `None`, those of the process.

    Returns:
        Exit code: 0 on success, 1 on errors of the configuration or the task. Errors
        of usage exit the process with code 2.
    """
    parser = _create_parser()
    args = parser.parse_args(argv)
    app = args.app or os.environ.get(APP_ENV_VAR)
    if not app:
        parser.error(f"--app or the environment variable {APP_ENV_VAR} is required")
    try:
        args.function(_load_app(app), args)
    except _UsageError as exc:
        parser.error(str(exc))
    # What goes wrong is for the operator to fix, so no traceback; whatever the app's
    # code and the startup checks raise, e.g., errors of the database
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0
