"""Tests for values of the configuration that come from the environment."""

from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from pydantic import SecretStr, ValidationError

from foca.config.access import set_app_config
from foca.config.config_parser import ConfigParser
from foca.config.environment import get_env, interpolate
from foca.database.register_mongodb import _get_client_options, get_mongo_uri
from foca.factories.celery_app import create_celery_app
from foca.models.config import Config, JobsConfig, MongoConfig


class TestInterpolate:
    """Tests for `interpolate()`."""

    @pytest.fixture(autouse=True)
    def _environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set some variables and unset another."""
        monkeypatch.setenv("SET", "value")
        monkeypatch.setenv("EMPTY", "")
        monkeypatch.delenv("UNSET", raising=False)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("${SET}", "value"),
            ("a-${SET}-b", "a-value-b"),
            ("${SET}${SET}", "valuevalue"),
            ("${EMPTY}", ""),
            ("${SET:-default}", "value"),
            ("${UNSET:-default}", "default"),
            ("${EMPTY:-default}", "default"),
            ("${UNSET:-}", ""),
            ("${UNSET:-a b:c}", "a b:c"),
            ("${UNSET:-$$x}", "$x"),
            ("${SET:?message}", "value"),
            ("$$", "$"),
            ("$${SET}", "${SET}"),
            ("$$$${SET}", "$${SET}"),
            ("$$${SET}", "$value"),
            ("no reference", "no reference"),
            ("$SET", "$SET"),
            ("/path$", "/path$"),
            ("a$b", "a$b"),
            ("{SET}", "{SET}"),
            ("", ""),
        ],
    )
    def test_forms(self, text: str, expected: str) -> None:
        """All forms are replaced as in Compose, and others stay."""
        assert interpolate(text) == expected

    def test_values_of_variables_stay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values of variables aren't interpolated in turn."""
        monkeypatch.setenv("REFERENCE", "$${SET}")
        assert interpolate("${REFERENCE}") == "$${SET}"

    def test_unset(self) -> None:
        """Unset variables without default are errors naming the key."""
        with pytest.raises(ValueError, match=r"'a\.b\[1\]\.c'.*'UNSET'.*not set"):
            interpolate({"a": {"b": ["x", {"c": "${UNSET}"}]}})

    @pytest.mark.parametrize("text", ["${UNSET:?needed}", "${EMPTY:?needed}"])
    def test_required(self, text: str) -> None:
        """Required variables that are unset or empty are errors with the message."""
        with pytest.raises(ValueError, match=r"'key'.*not set or empty: needed"):
            interpolate({"key": text})

    @pytest.mark.parametrize(
        "text",
        ["${", "${}", "${1A}", "${A-B}", "${A:-${B}}", "${SET", "${SET:x}", "a${ b}"],
    )
    def test_invalid(self, text: str) -> None:
        """Invalid references are errors naming the key."""
        with pytest.raises(ValueError, match=r"'key' has an invalid reference"):
            interpolate({"key": text})

    def test_no_values_in_errors(self) -> None:
        """Errors don't tell the value of the string."""
        with pytest.raises(ValueError) as exc_info:
            interpolate({"key": "hunter2-${UNSET}"})
        assert "hunter2" not in str(exc_info.value)

    def test_structure(self) -> None:
        """Only string values change, not keys or other types."""
        data = {
            "${SET}": "${SET}",
            "list": ["${SET}", 1, None, True, 1.5, {"nested": "${SET}"}],
            1: "${SET}",
        }
        assert interpolate(data) == {
            "${SET}": "value",
            "list": ["value", 1, None, True, 1.5, {"nested": "value"}],
            1: "value",
        }
        assert data["${SET}"] == "${SET}"


class TestConfigParser:
    """Tests for references in configuration files."""

    def test_values_are_replaced_and_validated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Values are replaced before validation."""
        monkeypatch.setenv("PORT_OF_APP", "1234")
        conf = tmp_path / "conf.yaml"
        conf.write_text("server:\n  port: ${PORT_OF_APP}\n")
        assert ConfigParser(conf, format_logs=False).config.server.port == 1234

    def test_error_names_file_and_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Errors name the file and the key."""
        monkeypatch.delenv("UNSET", raising=False)
        conf = tmp_path / "conf.yaml"
        conf.write_text("custom:\n  a: ${UNSET}\n")
        with pytest.raises(ValueError, match=r"conf\.yaml.*'custom\.a'.*'UNSET'"):
            ConfigParser(conf, format_logs=False)

    def test_overlay_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """References are replaced per file, before merging."""
        monkeypatch.setenv("VALUE_BASE", "base")
        monkeypatch.setenv("VALUE_OVERLAY", "overlay")
        monkeypatch.delenv("UNSET", raising=False)
        base = tmp_path / "base.yaml"
        base.write_text("custom:\n  a: ${VALUE_BASE}\n  b: ${VALUE_BASE}\n")
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text("custom:\n  b: ${VALUE_OVERLAY}\n")
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(overlay))
        config = ConfigParser(base, format_logs=False).config
        assert config.custom == {"a": "base", "b": "overlay"}  # type: ignore[attr-defined]  # extra field

    def test_overridden_values_must_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Also values that a later file overrides must resolve."""
        monkeypatch.delenv("UNSET", raising=False)
        monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
        base = tmp_path / "base.yaml"
        base.write_text("custom:\n  a: ${UNSET}\n")
        later = tmp_path / "later.yaml"
        later.write_text("custom:\n  a: value\n")
        with pytest.raises(ValueError, match=r"'custom\.a'"):
            ConfigParser([base, later], format_logs=False)

    def test_secret_not_in_validation_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Values of variables don't show up in validation errors."""
        monkeypatch.setenv("SECRET_PORT", "hunter2")
        conf = tmp_path / "conf.yaml"
        conf.write_text("server:\n  port: ${SECRET_PORT}\n")
        with pytest.raises(ValidationError) as exc_info:
            ConfigParser(conf, format_logs=False)
        assert "hunter2" not in str(exc_info.value)


class TestGetEnv:
    """Tests for `get_env()`."""

    @pytest.fixture(autouse=True)
    def _environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset the variables."""
        monkeypatch.delenv("X", raising=False)
        monkeypatch.delenv("X_FILE", raising=False)

    def test_unset(self) -> None:
        """Without variables, the default is returned."""
        assert get_env("X") is None
        assert get_env("X", "default") == "default"

    def test_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Variables are returned, also if empty."""
        monkeypatch.setenv("X", "value")
        assert get_env("X", "default") == "value"
        monkeypatch.setenv("X", "")
        assert get_env("X", "default") == ""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("secret\n", "secret"),
            ("secret", "secret"),
            ("secret\r\n", "secret"),
            ("secret\n\n", "secret\n"),
            (" secret \n", " secret "),
            ("", ""),
        ],
    )
    def test_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        content: str,
        expected: str,
    ) -> None:
        """Files are read, without the final line break."""
        path = tmp_path / "secret"
        path.write_bytes(content.encode())
        monkeypatch.setenv("X_FILE", str(path))
        assert get_env("X", "default") == expected

    def test_both(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting both is an error, without reading the file."""
        monkeypatch.setenv("X", "value")
        monkeypatch.setenv("X_FILE", str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="'X' and 'X_FILE' must not both"):
            get_env("X")

    def test_empty_counts_as_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty variables don't conflict, as with charts rendering empty values."""
        path = tmp_path / "secret"
        path.write_text("secret")
        monkeypatch.setenv("X", "")
        monkeypatch.setenv("X_FILE", str(path))
        assert get_env("X") == "secret"
        monkeypatch.setenv("X", "value")
        monkeypatch.setenv("X_FILE", "")
        assert get_env("X") == "value"

    def test_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files that can't be read are errors naming the variable."""
        monkeypatch.setenv("X_FILE", str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="'X_FILE' could not be read"):
            get_env("X")

    def test_invalid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files that aren't text are errors, without their content."""
        path = tmp_path / "binary"
        path.write_bytes(b"\xff\xfe")
        monkeypatch.setenv("X_FILE", str(path))
        with pytest.raises(ValueError, match="'X_FILE' could not be read"):
            get_env("X")


class TestUses:
    """Tests for the variables that FOCA reads."""

    @pytest.fixture
    def secret(self, tmp_path: Path) -> Path:
        """File with a secret."""
        path = tmp_path / "secret"
        path.write_text("pass word\n")
        return path

    @pytest.fixture(autouse=True)
    def _environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset the variables."""
        for name in (
            "MONGO_URI",
            "MONGO_USERNAME",
            "MONGO_PASSWORD",
            "MONGO_HOST",
            "MONGO_PORT",
            "MONGO_DBNAME",
            "CELERY_BROKER_URL",
            "CELERY_RESULT_BACKEND",
        ):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(f"{name}_FILE", raising=False)

    def test_mongo_credentials(
        self, secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MongoDB credentials can come from files."""
        user = tmp_path / "user"
        user.write_text("alice\n")
        monkeypatch.setenv("MONGO_USERNAME_FILE", str(user))
        monkeypatch.setenv("MONGO_PASSWORD_FILE", str(secret))
        assert get_mongo_uri(host="db", port=1, db="x").startswith(
            "mongodb://alice:pass+word@db:1/"
        )

    def test_mongo_uri(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MongoDB connection strings can come from files."""
        path = tmp_path / "uri"
        path.write_text("mongodb://elsewhere:2/\n")
        monkeypatch.setenv("MONGO_URI_FILE", str(path))
        assert get_mongo_uri() == "mongodb://elsewhere:2/"

    def test_mongo_both(self, secret: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting both is an error."""
        monkeypatch.setenv("MONGO_URI", "mongodb://elsewhere:2/")
        monkeypatch.setenv("MONGO_URI_FILE", str(secret))
        with pytest.raises(ValueError, match="must not both"):
            get_mongo_uri()

    def _celery_conf(self, **jobs: Any) -> Any:
        """Create a Celery app.

        Args:
            **jobs: Settings of `jobs`.

        Returns:
            Configuration of the Celery app.
        """
        app = Flask(__name__)
        set_app_config(app, Config(jobs=JobsConfig(**jobs)))
        return create_celery_app(app).conf

    def test_celery_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Celery's URLs can come from files."""
        broker = tmp_path / "broker"
        broker.write_text("amqps://u:p@broker:5671//\n")
        backend = tmp_path / "backend"
        backend.write_text("redis://:p@redis:6379/0\n")
        monkeypatch.setenv("CELERY_BROKER_URL_FILE", str(broker))
        monkeypatch.setenv("CELERY_RESULT_BACKEND_FILE", str(backend))
        conf = self._celery_conf()
        assert conf.broker_url == "amqps://u:p@broker:5671//"
        assert conf.result_backend == "redis://:p@redis:6379/0"

    def test_celery_backend_of_config(self) -> None:
        """The backend of the configuration is used, unmasked."""
        conf = self._celery_conf(backend="redis://:p@redis:6379/0")
        assert conf.result_backend == "redis://:p@redis:6379/0"


class TestSecretStr:
    """Tests for the fields of FOCA with secrets."""

    def test_backend_masked(self) -> None:
        """The result backend doesn't show up in representations."""
        jobs = JobsConfig(backend="redis://:hunter2@redis:6379/0")  # type: ignore[arg-type]  # converted
        assert isinstance(jobs.backend, SecretStr)
        assert "hunter2" not in repr(jobs)
        assert "hunter2" not in str(jobs)
        assert "hunter2" not in repr(Config(jobs=jobs))
        assert jobs.backend.get_secret_value().endswith("6379/0")

    def test_default_backend(self) -> None:
        """The default result backend is read in the same way."""
        assert JobsConfig().backend.get_secret_value() == "rpc://"

    @pytest.mark.parametrize(
        "name", ["tlsCertificateKeyFilePassword", "authMechanismProperties"]
    )
    def test_client_options_masked(self, name: str) -> None:
        """Options of MongoDB clients with secrets are masked, and passed on."""
        conf = MongoConfig(client_options={name: "hunter2", "maxPoolSize": 5})
        assert isinstance(conf.client_options[name], SecretStr)
        assert "hunter2" not in repr(conf)
        options = _get_client_options(
            uri="mongodb://db:1/", options=conf.client_options, appname=None
        )
        assert options[name.lower()] == "hunter2"
        assert options["maxpoolsize"] == 5

    def test_other_client_options_stay(self) -> None:
        """Other options stay as they are."""
        conf = MongoConfig(client_options={"appname": "x", "maxPoolSize": 5})
        assert conf.client_options == {"appname": "x", "maxPoolSize": 5}
