"""Tests for the command-line tool and the startup switches."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from foca import Foca
from foca.cli import main
from foca.database.register_mongodb import register_mongodb
from foca.models.config import MongoConfig
from foca.plugins.engine import PluginHost
from foca.security.access_control.register_access_control import sync_policies

FILES = Path(__file__).parent / "test_files"
SPEC = FILES / "openapi_3_petstore.original.yaml"
ISSUER = "https://login.example.org/oidc/"
MONGO_PORT = 12345
RULE = ["p", "alice", "/data", "GET"]
MODULE = """
from foca import Foca

print("printed by the module")
foca = Foca(config_file="config.yaml")
not_foca = 1


def factory():
    return foca


def wrong_factory():
    return 1


def failing_factory():
    raise ValueError("factory failed")


def app_factory():
    return Foca(config_file="config.yaml").create_app()
"""


def _spec_entry(**kwargs: Any) -> dict[str, Any]:
    """Create an entry of `api.specs`.

    Args:
        **kwargs: Further fields of the entry.

    Returns:
        Entry for the specification of a petstore, with responses of 501.
    """
    return {
        "path": str(SPEC),
        "add_operation_fields": {"x-openapi-router-controller": "controllers"},
        "connexion": {"resolver_error": 501},
        **kwargs,
    }


CONF_API = {"api": {"specs": [_spec_entry()]}}
CONF_INDEXES = {
    "db": {
        "dbs": {
            "cli-db": {
                "collections": {"items": {"indexes": [{"keys": {"name": 1}}]}},
            }
        }
    }
}
AUTH = {"issuers": [ISSUER], "audience": ["foca"]}
ACCESS_CONTROL = {"enabled": True, "policies": [RULE]}


AppFactory = Callable[..., str]


@pytest.fixture
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppFactory:
    """Get a function writing a module of an app to the current directory.

    Args:
        tmp_path: Directory of the app, made the current directory.
        monkeypatch: Monkeypatch, which restores the search path of modules.

    Returns:
        Function taking the configuration and returning the name of the module.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))

    def _write(conf: dict[str, Any] | str) -> str:
        """Write the configuration and the module.

        Args:
            conf: Configuration, or the text of the file.

        Returns:
            Unique name of the module.
        """
        text = conf if isinstance(conf, str) else yaml.safe_dump(conf)
        (tmp_path / "config.yaml").write_text(text)
        name = f"cli_app_{uuid4().hex}"
        (tmp_path / f"{name}.py").write_text(MODULE)
        return name

    return _write


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Use a real MongoDB, with a database name of its own.

    Args:
        monkeypatch: Monkeypatch, which restores the environment.

    Yields:
        Name of the database, dropped after the test.
    """
    uri = f"mongodb://localhost:{MONGO_PORT}"
    monkeypatch.setenv("MONGO_URI", uri)
    name = f"cli_test_{uuid4().hex}"
    client: MongoClient[dict[str, Any]] = MongoClient(uri)
    yield name
    client.drop_database(name)
    client.close()


def _usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run the tool, expecting an error of usage.

    Args:
        argv: Arguments.
        capsys: Capturing fixture.

    Returns:
        What was written to the standard error.
    """
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2
    return capsys.readouterr().err


class TestUsage:
    """Tests for how the tool is called."""

    @pytest.mark.parametrize(
        "command", ["check-config", "create-indexes", "sync-policies", "export-spec"]
    )
    def test_help_of_commands(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each command has a help text."""
        with pytest.raises(SystemExit) as exc_info:
            main([command, "--help"])
        assert exc_info.value.code == 0
        assert f"usage: foca {command}" in capsys.readouterr().out

    def test_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The tool has a help text."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        assert "create-indexes" in capsys.readouterr().out

    def test_no_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A command is required."""
        assert "required" in _usage_error([], capsys)

    def test_unknown_option(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Unknown options are errors of usage."""
        assert "unrecognized" in _usage_error(["check-config", "--unknown"], capsys)

    def test_no_app(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An app is required."""
        monkeypatch.delenv("FOCA_APP", raising=False)
        assert "FOCA_APP" in _usage_error(["check-config"], capsys)

    @pytest.mark.parametrize(
        "app",
        ["no_colon", ":attribute", "module:", "./module.py:foca", ".module:foca"],
    )
    def test_invalid_form(self, app: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Apps must be given as `module:attribute`."""
        message = _usage_error(["--app", app, "check-config"], capsys)
        assert "module:attribute" in message

    def test_module_not_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Modules that don't exist are errors of usage."""
        message = _usage_error(
            ["--app", "cli_no_module.x:foca", "check-config"], capsys
        )
        assert "not found" in message

    @pytest.mark.parametrize(
        ("attribute", "message"),
        [
            ("missing", "no attribute"),
            ("not_foca", "neither"),
            ("wrong_factory", "neither"),
            ("Foca", "neither"),
        ],
    )
    def test_invalid_attribute(
        self,
        app_module: AppFactory,
        capsys: pytest.CaptureFixture[str],
        attribute: str,
        message: str,
    ) -> None:
        """Attributes must be `Foca` instances or functions returning one."""
        name = app_module({})
        argv = ["--app", f"{name}:{attribute}", "check-config"]
        assert message in _usage_error(argv, capsys)


class TestLoading:
    """Tests for loading apps."""

    def test_instance(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Apps may be `Foca` instances."""
        name = app_module(CONF_API)
        assert main(["--app", f"{name}:foca", "check-config"]) == 0
        assert "valid" in capsys.readouterr().out

    def test_factory(self, app_module: AppFactory) -> None:
        """Apps may be functions returning `Foca` instances."""
        name = app_module(CONF_API)
        assert main(["--app", f"{name}:factory", "check-config"]) == 0

    def test_environment(
        self, app_module: AppFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Apps may be given in the environment."""
        monkeypatch.setenv("FOCA_APP", f"{app_module(CONF_API)}:foca")
        assert main(["check-config"]) == 0

    def test_option_wins(
        self,
        app_module: AppFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The option wins over the environment."""
        monkeypatch.setenv("FOCA_APP", "cli_no_module:foca")
        assert main(["--app", f"{app_module(CONF_API)}:foca", "check-config"]) == 0

    def test_overlay(
        self,
        app_module: AppFactory,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Overlays are merged, as for apps."""
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text("server: {unknown_key: 1}\n")
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(overlay))
        assert main(["--app", f"{app_module({})}:foca", "check-config"]) == 1
        assert "unknown_key" in capsys.readouterr().err

    def test_failing_factory(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Errors of factories are errors of the configuration."""
        name = app_module({})
        assert main(["--app", f"{name}:failing_factory", "check-config"]) == 1
        assert "Error: factory failed" in capsys.readouterr().err

    def test_missing_import_of_module(
        self,
        app_module: AppFactory,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Modules that an app's module imports don't count as usage errors."""
        name = app_module({})
        (tmp_path / f"{name}.py").write_text("import cli_no_dependency\n")
        assert main(["--app", f"{name}:foca", "check-config"]) == 1
        assert "cli_no_dependency" in capsys.readouterr().err


class TestCheckConfig:
    """Tests for `check-config`."""

    def test_validation_error(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Validation errors are reported without traceback."""
        name = app_module({"server": {"port": "not a number"}})
        assert main(["--app", f"{name}:foca", "check-config"]) == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "port" in err
        assert "Traceback" not in err

    def test_invalid_yaml(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Files that aren't YAML are reported."""
        name = app_module("a: [")
        assert main(["--app", f"{name}:foca", "check-config"]) == 1
        assert "not valid YAML" in capsys.readouterr().err

    def test_startup_checks(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The startup checks of apps run."""
        conf = {
            "security": {
                "auth": AUTH,
                "access_control": {"enabled": True, "require_admin": True},
            }
        }
        assert main(["--app", f"{app_module(conf)}:foca", "check-config"]) == 1
        assert "admin" in capsys.readouterr().err

    def test_checks_of_workers(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The startup checks of workers run, if the app has jobs."""
        conf = {
            "jobs": {
                "host": "rabbitmq",
                "periodic": {"bad": {"task": "a.b", "cron": {"minute": "99"}}},
            }
        }
        assert main(["--app", f"{app_module(conf)}:foca", "check-config"]) == 1
        assert "bad" in capsys.readouterr().err

    def test_jobs(self, app_module: AppFactory) -> None:
        """Apps with valid jobs pass."""
        conf = {"jobs": {"host": "rabbitmq"}}
        assert main(["--app", f"{app_module(conf)}:foca", "check-config"]) == 0

    def test_invalid_policies(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Configured rules that keep the app from starting are found."""
        conf = {
            "security": {
                "auth": AUTH,
                "access_control": {"enabled": True, "policies": [["p", "alice"]]},
            }
        }
        assert main(["--app", f"{app_module(conf)}:foca", "check-config"]) == 1
        assert "Error:" in capsys.readouterr().err

    def test_factory_of_web_app_writes_nothing(
        self,
        app_module: AppFactory,
        mongodb: MongoClient[dict[str, Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Functions returning the app are refused, without writes on startup."""
        conf = {
            **CONF_INDEXES,
            "security": {"auth": AUTH, "access_control": ACCESS_CONTROL},
        }
        argv = ["--app", f"{app_module(conf)}:app_factory", "check-config"]
        assert "neither" in _usage_error(argv, capsys)
        assert mongodb.list_database_names() == []

    def test_access_control_without_db_writes_nothing(
        self, app_module: AppFactory, mongodb: MongoClient[dict[str, Any]]
    ) -> None:
        """The database that access control adds isn't written to either."""
        conf = {"security": {"auth": AUTH, "access_control": ACCESS_CONTROL}}
        assert main(["--app", f"{app_module(conf)}:foca", "check-config"]) == 0
        assert mongodb.list_database_names() == []

    def test_instances_created_while_running_write_nothing(
        self, app_module: AppFactory, mongodb: MongoClient[dict[str, Any]]
    ) -> None:
        """Instances of modules that the command imports don't write either."""
        created: list[Foca] = []

        def create(foca: Foca) -> Any:
            """Create another instance, as a worker module imported by controllers.

            Args:
                foca: Instance of the app.

            Returns:
                Connexion app.
            """
            created.append(Foca(config_file="config.yaml"))
            created[0].create_app()
            return foca.create_app()

        name = app_module(CONF_INDEXES)
        with patch("foca.cli._create_app_without_writes", create):
            assert main(["--app", f"{name}:foca", "check-config"]) == 0
        assert created[0].conf.db is not None
        assert created[0].conf.db.create_indexes_on_startup is False
        assert mongodb.list_database_names() == []

    def test_app_failure_shows_debug_traceback(self, app_module: AppFactory) -> None:
        """The cause of failures is logged on level `DEBUG`."""
        name = app_module({})
        with patch("foca.cli.logger") as logger:
            assert main(["--app", f"{name}:failing_factory", "check-config"]) == 1
        logger.debug.assert_called_once_with("Command failed", exc_info=True)

    def test_writes_nothing(
        self,
        app_module: AppFactory,
        mongodb: MongoClient[dict[str, Any]],
    ) -> None:
        """Neither indexes nor policies are written."""
        conf = {
            **CONF_INDEXES,
            "security": {"auth": AUTH, "access_control": ACCESS_CONTROL},
        }
        assert main(["--app", f"{app_module(conf)}:foca", "check-config"]) == 0
        assert mongodb.list_database_names() == []


@pytest.mark.mongodb
class TestCreateIndexes:
    """Tests for `create-indexes`."""

    def test_create(
        self,
        app_module: AppFactory,
        database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The configured indexes are created."""
        conf = {
            "db": {
                "dbs": {
                    database: CONF_INDEXES["db"]["dbs"]["cli-db"],
                }
            }
        }
        assert main(["--app", f"{app_module(conf)}:foca", "create-indexes"]) == 0
        assert "created" in capsys.readouterr().out
        client: MongoClient[dict[str, Any]] = MongoClient(
            f"mongodb://localhost:{MONGO_PORT}"
        )
        try:
            assert "name_1" in client[database]["items"].index_information()
        finally:
            client.close()

    def test_also_with_switch_off(self, app_module: AppFactory, database: str) -> None:
        """Indexes are created also if startup doesn't create them."""
        conf = {
            "db": {
                "create_indexes_on_startup": False,
                "dbs": {database: CONF_INDEXES["db"]["dbs"]["cli-db"]},
            }
        }
        assert main(["--app", f"{app_module(conf)}:foca", "create-indexes"]) == 0
        client: MongoClient[dict[str, Any]] = MongoClient(
            f"mongodb://localhost:{MONGO_PORT}"
        )
        try:
            assert "name_1" in client[database]["items"].index_information()
        finally:
            client.close()

    def test_conflict(
        self,
        app_module: AppFactory,
        database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Conflicting indexes are reported."""
        client: MongoClient[dict[str, Any]] = MongoClient(
            f"mongodb://localhost:{MONGO_PORT}"
        )
        try:
            client[database]["items"].create_index({"name": 1}, unique=True)
        finally:
            client.close()
        conf = {"db": {"dbs": {database: CONF_INDEXES["db"]["dbs"]["cli-db"]}}}
        assert main(["--app", f"{app_module(conf)}:foca", "create-indexes"]) == 1
        err = capsys.readouterr().err
        assert "conflicts" in err
        assert "Traceback" not in err

    def test_no_databases(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without databases, there's nothing to do, which is reported."""
        assert main(["--app", f"{app_module({})}:foca", "create-indexes"]) == 1
        assert "No databases" in capsys.readouterr().err


@pytest.mark.mongodb
class TestSyncPolicies:
    """Tests for `sync-policies`."""

    @staticmethod
    def _conf(database: str, **access_control: Any) -> dict[str, Any]:
        """Create a configuration with access control.

        Args:
            database: Name of the database of the policies.
            **access_control: Further settings of access control.

        Returns:
            Configuration.
        """
        return {
            "security": {
                "auth": AUTH,
                "access_control": {
                    **ACCESS_CONTROL,
                    "db_name": database,
                    "collection_name": "policies",
                    **access_control,
                },
            }
        }

    @staticmethod
    def _stored(database: str) -> list[list[str]]:
        """Get the stored configured rules.

        Args:
            database: Name of the database of the policies.

        Returns:
            Stored configured rules.
        """
        client: MongoClient[dict[str, Any]] = MongoClient(
            f"mongodb://localhost:{MONGO_PORT}"
        )
        try:
            return [
                [doc["ptype"], doc["v0"], doc["v1"], doc["v2"]]
                for doc in client[database]["policies"].find({"configured": True})
            ]
        finally:
            client.close()

    def test_sync(
        self,
        app_module: AppFactory,
        database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The configured rules are stored, also if startup doesn't store them."""
        conf = self._conf(database, sync_policies_on_startup=False)
        assert main(["--app", f"{app_module(conf)}:foca", "sync-policies"]) == 0
        assert "synchronized" in capsys.readouterr().out
        assert self._stored(database) == [RULE]

    def test_newer_revision_stays(
        self,
        app_module: AppFactory,
        database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The command fails if the stored rules stay."""
        newer = self._conf(database, policies_revision=5)
        assert main(["--app", f"{app_module(newer)}:foca", "sync-policies"]) == 0
        older = self._conf(
            database, policies_revision=3, policies=[["p", "bob", "/data", "GET"]]
        )
        capsys.readouterr()
        assert main(["--app", f"{app_module(older)}:foca", "sync-policies"]) == 1
        assert "revision is newer" in capsys.readouterr().err
        assert self._stored(database) == [RULE]
        forced = self._conf(
            database,
            policies_revision=3,
            policies=None,
            policies_force=True,
        )
        assert main(["--app", f"{app_module(forced)}:foca", "sync-policies"]) == 0
        assert self._stored(database) == []

    def test_removal_with_newer_revision_stays(
        self, app_module: AppFactory, database: str
    ) -> None:
        """The command fails if the rules to remove stay."""
        newer = self._conf(database, policies_revision=5)
        assert main(["--app", f"{app_module(newer)}:foca", "sync-policies"]) == 0
        older = self._conf(database, policies_revision=3, policies=None)
        assert main(["--app", f"{app_module(older)}:foca", "sync-policies"]) == 1
        assert self._stored(database) == [RULE]

    def test_removes_without_policies(
        self, app_module: AppFactory, database: str
    ) -> None:
        """Without configured rules, stored ones are removed, as on startup."""
        assert (
            main(["--app", f"{app_module(self._conf(database))}:foca", "sync-policies"])
            == 0
        )
        assert self._stored(database) == [RULE]
        conf = self._conf(database, policies=None)
        assert main(["--app", f"{app_module(conf)}:foca", "sync-policies"]) == 0
        assert self._stored(database) == []

    def test_not_enabled(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without access control, there's nothing to do, which is reported."""
        assert main(["--app", f"{app_module({})}:foca", "sync-policies"]) == 1
        assert "not enabled" in capsys.readouterr().err


class TestExportSpec:
    """Tests for `export-spec`."""

    def test_yaml(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Specifications are written as YAML by default."""
        assert main(["--app", f"{app_module(CONF_API)}:foca", "export-spec"]) == 0
        spec = yaml.safe_load(capsys.readouterr().out)
        assert spec["info"]["title"]
        assert "/pets" in spec["paths"]

    def test_json_as_served(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Specifications are those that the app serves, as JSON."""
        name = app_module(CONF_API)
        argv = ["--app", f"{name}:foca", "export-spec", "--format", "json"]
        assert main(argv) == 0
        exported = json.loads(capsys.readouterr().out)
        foca = sys.modules[name].foca
        served = foca.create_app().app.test_client().get("/v2/openapi.json")
        assert served.status_code == 200
        assert exported == served.json
        operation = exported["paths"]["/pets"]["get"]
        assert operation["x-openapi-router-controller"] == "controllers"

    def test_swagger_as_served(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The same holds for specifications of Swagger 2."""
        conf = {
            "api": {
                "specs": [
                    _spec_entry(path=str(FILES / "openapi_2_petstore.original.yaml"))
                ]
            }
        }
        name = app_module(conf)
        argv = ["--app", f"{name}:foca", "export-spec", "--format", "json"]
        assert main(argv) == 0
        exported = json.loads(capsys.readouterr().out)
        app = sys.modules[name].foca.create_app().app
        served = app.test_client().get(f"{exported['basePath']}/swagger.json")
        assert served.status_code == 200
        assert exported == served.json

    def test_status_codes_as_strings(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """YAML has the same types as JSON, e.g., for status codes."""
        assert main(["--app", f"{app_module(CONF_API)}:foca", "export-spec"]) == 0
        spec = yaml.safe_load(capsys.readouterr().out)
        responses = spec["paths"]["/pets"]["get"]["responses"]
        assert all(isinstance(code, str) for code in responses)

    def test_several_specs_need_selection(
        self, app_module: AppFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the app has several specifications, one must be selected."""
        name = app_module(_several_specs(tmp_path))
        message = _usage_error(["--app", f"{name}:foca", "export-spec"], capsys)
        assert "--spec" in message
        assert "first" in message
        assert "second" in message

    @pytest.mark.parametrize(
        ("selection", "version"), [("first", "/v1"), ("/v2", "/v2")]
    )
    def test_select(
        self,
        app_module: AppFactory,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        selection: str,
        version: str,
    ) -> None:
        """Specifications are selected by name or base path."""
        name = app_module(_several_specs(tmp_path))
        argv = ["--app", f"{name}:foca", "export-spec", "--spec", selection]
        assert main(argv) == 0
        spec = yaml.safe_load(capsys.readouterr().out)
        assert spec["servers"][0]["url"].endswith(version)

    def test_unknown_selection(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Specifications that don't exist are reported."""
        argv = ["--app", f"{app_module(CONF_API)}:foca", "export-spec", "--spec", "x"]
        assert main(argv) == 1
        assert "no specification 'x'" in capsys.readouterr().err

    def test_no_specs(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Apps without specifications are reported."""
        assert main(["--app", f"{app_module({})}:foca", "export-spec"]) == 1
        assert "no API specifications" in capsys.readouterr().err

    def test_permissions_api_left_out(
        self, app_module: AppFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The permissions API isn't one of the specifications to choose from."""
        conf = {
            **CONF_API,
            "security": {"auth": AUTH, "access_control": {"enabled": True}},
        }
        assert main(["--app", f"{app_module(conf)}:foca", "export-spec"]) == 0
        assert "/pets" in yaml.safe_load(capsys.readouterr().out)["paths"]


def _several_specs(directory: Path) -> dict[str, Any]:
    """Create a configuration with two specifications.

    Args:
        directory: Directory for the second specification.

    Returns:
        Configuration.
    """
    second = directory / "second.yaml"
    shutil.copy(SPEC, second)
    spec = yaml.safe_load(second.read_text())
    spec["servers"] = [{"url": "http://example.org/v2"}]
    second.write_text(yaml.safe_dump(spec))
    first = directory / "first.yaml"
    spec["servers"] = [{"url": "http://example.org/v1"}]
    first.write_text(yaml.safe_dump(spec))
    return {
        "api": {
            "specs": {
                "first": _spec_entry(path=str(first)),
                "second": _spec_entry(path=str(second)),
            }
        }
    }


class TestSwitches:
    """Tests for the startup switches."""

    def test_defaults(self) -> None:
        """By default, startup does as before."""
        assert MongoConfig().create_indexes_on_startup is True

    def test_indexes_on_startup(self, mongodb: MongoClient[dict[str, Any]]) -> None:
        """By default, indexes are created on startup."""
        conf = MongoConfig.model_validate(CONF_INDEXES["db"])
        register_mongodb(conf)
        assert "name_1" in mongodb["cli-db"]["items"].index_information()

    def test_no_indexes_on_startup(self, mongodb: MongoClient[dict[str, Any]]) -> None:
        """With the switch off, indexes aren't created, but clients are set."""
        conf = MongoConfig.model_validate(
            {**CONF_INDEXES["db"], "create_indexes_on_startup": False}
        )
        register_mongodb(conf)
        assert "cli-db" not in mongodb.list_database_names()
        collection = conf.dbs["cli-db"].collections["items"]  # type: ignore[index]  # set
        assert collection.client is not None

    def _foca(self, tmp_path: Path, **access_control: Any) -> Foca:
        """Create an instance of an app with configured rules.

        Args:
            tmp_path: Directory of the configuration.
            **access_control: Further settings of access control.

        Returns:
            Instance.
        """
        path = tmp_path / "switch.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "security": {
                        "auth": AUTH,
                        "access_control": {
                            **ACCESS_CONTROL,
                            "db_name": "cli-ac",
                            "collection_name": "policies",
                            **access_control,
                        },
                    }
                }
            )
        )
        return Foca(config_file=path)

    def test_policies_on_startup(
        self, tmp_path: Path, mongodb: MongoClient[dict[str, Any]]
    ) -> None:
        """By default, configured rules are stored on startup."""
        self._foca(tmp_path).create_app()
        assert mongodb["cli-ac"]["policies"].count_documents({"configured": True}) == 1

    def test_no_policies_on_startup(
        self, tmp_path: Path, mongodb: MongoClient[dict[str, Any]]
    ) -> None:
        """With the switch off, stored rules stay, until they are synchronized."""
        foca = self._foca(tmp_path, sync_policies_on_startup=False)
        app = foca.create_app()
        assert mongodb["cli-ac"]["policies"].count_documents({"configured": True}) == 0
        sync_policies(app.app, foca.conf.security.access_control)
        assert mongodb["cli-ac"]["policies"].count_documents({"configured": True}) == 1

    def test_store_of_gateway_on_startup(self) -> None:
        """Gateway apps set up their store on startup, by default."""
        foca = Foca()
        foca.gateway = MagicMock(backends=None)
        with mock.patch("foca.foca.runtime") as runtime:
            foca.create_app()
        runtime.create_host.return_value.setup_store.assert_called_once_with()

    def test_no_store_of_gateway_on_startup(
        self, mongodb: MongoClient[dict[str, Any]]
    ) -> None:
        """With the switch off, gateway apps leave their store alone."""
        foca = Foca()
        foca.conf.db = MongoConfig(create_indexes_on_startup=False)
        foca.gateway = MagicMock(backends=None)
        with mock.patch("foca.foca.runtime") as runtime:
            foca.create_app()
        host = runtime.create_host.return_value
        host.setup_store.assert_not_called()
        host.warn_store_settings.assert_called_once_with()
        runtime.create_host.return_value.close.assert_called_once_with()

    @pytest.mark.parametrize("db", [None, MongoConfig(create_indexes_on_startup=True)])
    def test_no_store_of_gateway_without_writes(self, db: MongoConfig | None) -> None:
        """Without startup writes, gateway apps leave their store alone."""
        foca = Foca()
        foca.conf.db = db
        foca.gateway = MagicMock(backends=None)
        foca.disable_startup_writes()
        with mock.patch("foca.foca.runtime") as runtime:
            foca.create_app()
        host = runtime.create_host.return_value
        host.setup_store.assert_not_called()
        host.warn_store_settings.assert_called_once_with()
        host.close.assert_called_once_with()

    def test_create_indexes_of_gateway(
        self, mongodb: MongoClient[dict[str, Any]]
    ) -> None:
        """Creating indexes sets up the store of gateway apps."""
        foca = Foca()
        foca.conf.db = MongoConfig.model_validate(CONF_INDEXES["db"])
        foca.gateway = MagicMock()
        with mock.patch("foca.foca.runtime") as runtime:
            foca.create_indexes()
        host = runtime.create_host.return_value
        host.setup_store.assert_called_once_with(strict=True)
        host.close.assert_called_once_with()
        assert "name_1" in mongodb["cli-db"]["items"].index_information()


class TestStoreSetup:
    """Tests for setting up the store of gateway plugins."""

    @staticmethod
    def _host(backend: MagicMock) -> MagicMock:
        """Create a stand-in for a plugin host.

        Args:
            backend: Backend of the store.

        Returns:
            Stand-in with a store in a namespace.
        """
        settings = MagicMock(backend=backend, namespace="ns", max_ttl=1.0)
        return MagicMock(_stores={"plugin": settings}, _store_backend=backend)

    def test_strict_index_error(self) -> None:
        """Errors of creating indexes are raised, if strict."""
        backend = MagicMock()
        backend.setup.side_effect = OperationFailure("conflict", code=85)
        with pytest.raises(OperationFailure):
            PluginHost.setup_store(self._host(backend), strict=True)

    def test_strict_expiry_error(self) -> None:
        """Errors of capping the expiry are raised, if strict."""
        backend = MagicMock()
        backend.cap_expiry.side_effect = OperationFailure("denied", code=13)
        with pytest.raises(OperationFailure):
            PluginHost.setup_store(self._host(backend), strict=True)

    def test_errors_logged_by_default(self) -> None:
        """Errors are logged by default, so that the app starts."""
        backend = MagicMock()
        backend.setup.side_effect = OperationFailure("conflict", code=85)
        backend.cap_expiry.side_effect = OperationFailure("denied", code=13)
        PluginHost.setup_store(self._host(backend))


class TestProcess:
    """Tests for running the tool in a process of its own."""

    @staticmethod
    def _run(*argv: str) -> subprocess.CompletedProcess[str]:
        """Run the entry point of the tool.

        Args:
            *argv: Arguments.

        Returns:
            Result of the process.
        """
        return subprocess.run(  # noqa: S603 (fixed arguments, no shell)
            [
                sys.executable,
                "-c",
                "import sys; from foca.cli import main; sys.exit(main())",
                *argv,
            ],
            capture_output=True,
            check=False,
            # The FOCA under test, whatever is installed
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
            text=True,
            timeout=120,
        )

    def test_entry_point(self) -> None:
        """The console script is the entry point."""
        pyproject = Path(__file__).parents[1] / "pyproject.toml"
        scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
        assert scripts["foca"] == "foca.cli:main"

    def test_output_is_only_the_specification(self, app_module: AppFactory) -> None:
        """Logs don't mix with the specification, with the default log settings."""
        name = app_module(CONF_API)
        result = self._run("--app", f"{name}:foca", "export-spec", "--format", "json")
        assert result.returncode == 0
        assert "/pets" in json.loads(result.stdout)["paths"]
        assert "printed by the module" in result.stderr

    @pytest.mark.parametrize(
        ("conf", "argv", "code"),
        [
            ({}, ["check-config"], 0),
            ({"server": {"port": "x"}}, ["check-config"], 1),
            ({}, ["check-config", "--unknown"], 2),
        ],
    )
    def test_exit_codes(
        self, app_module: AppFactory, conf: dict[str, Any], argv: list[str], code: int
    ) -> None:
        """The exit codes tell success, errors and errors of usage apart."""
        result = self._run("--app", f"{app_module(conf)}:foca", *argv)
        assert result.returncode == code, result.stderr
        assert "Traceback" not in result.stderr
