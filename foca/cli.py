"""Command-line tool for admin tasks of FOCA apps.

Run admin tasks once, e.g., before a deployment, rather than at the startup of each
worker of each replica. Exit codes are 0 on success, 1 on errors of the
configuration or the task, and 2 on errors of usage.
"""

import argparse
import contextlib
import importlib
import inspect
import json
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

import yaml

from foca.foca import Foca, no_startup_writes
from foca.security.access_control.register_access_control import sync_policies
from foca.utils.urls import _get_apis, _RegisteredApi

APP_ENV_VAR = "FOCA_APP"

logger = logging.getLogger(__name__)


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
    if not (module_name and separator and attribute) or (
        module_name.startswith(".") or "/" in module_name
    ):
        raise _UsageError(f"'{app}' is not of the form 'module:attribute'")
    if os.getcwd() not in sys.path:
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
    # Not any callable: classes, and apps of the web framework, aren't factories
    if inspect.isroutine(value):
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
    foca.disable_startup_writes()
    return foca.create_app()


def _check_config(foca: Foca, _args: argparse.Namespace) -> str:
    """Run the startup checks of the app and, if it has jobs, of its workers.

    Args:
        foca: `Foca` instance.
        _args: Arguments of the command.

    Returns:
        Output.
    """
    _create_app_without_writes(foca)
    if foca.conf.jobs:
        foca.create_celery_app()
    return "Configuration is valid.\n"


def _create_indexes(foca: Foca, _args: argparse.Namespace) -> str:
    """Create the configured indexes.

    Args:
        foca: `Foca` instance.
        _args: Arguments of the command.

    Returns:
        Output.
    """
    foca.create_indexes()
    return "Indexes created.\n"


def _sync_policies(foca: Foca, _args: argparse.Namespace) -> str:
    """Replace the stored configured policy rules with the configured ones.

    Args:
        foca: `Foca` instance.
        _args: Arguments of the command.

    Returns:
        Output.

    Raises:
        ValueError: Access control isn't enabled, or the stored rules stay, as their
            revision is newer.
    """
    access_control = foca.conf.security.access_control
    if not access_control.enabled:
        raise ValueError("Access control is not enabled")
    app = _create_app_without_writes(foca)
    if not sync_policies(app.app, access_control):
        raise ValueError(
            "Stored policy rules not replaced, as their revision is newer: raise "
            "'policies_revision', or set 'policies_force' for a rollback"
        )
    return "Policies synchronized.\n"


def _namespace_subjects(foca: Foca, args: argparse.Namespace) -> str:
    """Namespace the bare users of the stored policy rules.

    Args:
        foca: `Foca` instance.
        args: Arguments of the command: `namespace`, and `dry_run`.

    Returns:
        Output: the numbers of rules (cf.
        :py:meth:`foca.security.access_control.foca_casbin_adapter.adapter.Adapter.namespace_users`).

    Raises:
        ValueError: Access control isn't enabled, the namespace is empty or
            no trusted issuer has it, or an issuer keeps its subjects bare,
            so that bare users are that issuer's.
    """
    access_control = foca.conf.security.access_control
    if not access_control.enabled:
        raise ValueError("Access control is not enabled")
    namespaces = foca.conf.security.auth.get_namespaces()
    namespace = args.namespace
    if namespace not in namespaces.values() or not namespace:
        known = ", ".join(sorted(value for value in namespaces.values() if value))
        raise ValueError(
            f"No trusted issuer has the namespace '{namespace}', but: "
            f"{known or 'none'} (security.auth.issuers)"
        )
    bare = [issuer for issuer, value in namespaces.items() if value is None]
    if bare:
        raise ValueError(
            f"Issuer '{bare[0]}' keeps its subjects bare (namespace ''), so "
            "bare users are its users: nothing to namespace"
        )
    app = _create_app_without_writes(foca)
    counts = app.app.config["casbin_adapter"].namespace_users(
        access_control.subject_prefix,
        namespace,
        [value for value in namespaces.values() if value],
        dry_run=args.dry_run,
    )
    verb = "To namespace" if args.dry_run else "Namespaced"
    output = f"{verb}: {counts.rewritten} stored rules with bare users.\n"
    if counts.invalid:
        output += (
            f"Kept: {counts.invalid} stored rules whose namespaced users would "
            "not be valid rule values (e.g., too long).\n"
        )
    if counts.conflicting:
        output += (
            f"Kept: {counts.conflicting} stored rules granting roles on resources "
            "on which the namespaced users already have a role (e.g., changed "
            "since the upgrade).\n"
        )
    if counts.ambiguous:
        output += (
            f"Kept: {counts.ambiguous} stored rules naming users that already "
            f"start with '{namespace}|' (e.g., namespaced by an earlier run); "
            "bare subjects that look like this can't be told apart, so if the "
            "issuer's subjects may start so, choose another namespace.\n"
        )
    if counts.configured:
        output += (
            f"Kept: {counts.configured} configured rules with bare users; "
            "namespace them in 'security.access_control.policies'.\n"
        )
    return output


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


def _export_spec(foca: Foca, args: argparse.Namespace) -> str:
    """Get a merged API specification, as served.

    Args:
        foca: `Foca` instance.
        args: Arguments of the command.

    Returns:
        Output: the specification.
    """
    app = _create_app_without_writes(foca)
    apis = [api for api in _get_apis(app.app) if api.is_app_api]
    # Keeps it in the types of JSON, also for YAML, e.g., for status codes
    text = json.dumps(_select_spec(apis, args.spec).specification, default=str)
    if args.format == "json":
        return f"{text}\n"
    return yaml.safe_dump(json.loads(text), sort_keys=False)


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
        (
            "namespace-subjects",
            _namespace_subjects,
            "Namespace the bare users of the stored policy rules with the "
            "namespace of a trusted issuer, e.g., those stored before FOCA 0.23.0.",
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
        elif name == "namespace-subjects":
            command.add_argument(
                "namespace",
                help=(
                    "Namespace of the issuer whose users are bare: by default, "
                    "the issuer (e.g., https://login.example.org/)"
                ),
            )
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="Only count the rules, without changes",
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
    # Only the output of the command goes to the standard output, not what the app's
    # code prints or logs there
    with contextlib.redirect_stdout(sys.stderr):
        try:
            # Also while it runs: controllers may import a worker, with its own instance
            with no_startup_writes():
                output = args.function(_load_app(app), args)
        except _UsageError as exc:
            parser.error(str(exc))
        # What goes wrong is for the operator to fix, so no traceback by default;
        # whatever the app's code and the startup checks raise, e.g., database errors
        except Exception as exc:
            logger.debug("Command failed", exc_info=True)
            sys.stderr.write(f"Error: {exc}\n")
            return 1
    sys.stdout.write(output)
    return 0
