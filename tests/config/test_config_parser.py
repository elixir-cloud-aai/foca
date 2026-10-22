"""Tests for config_parser.py."""

import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic import (
    BaseModel,
    ValidationError,
)

from foca.config import config_parser as parser_module
from foca.config.config_parser import ConfigParser, deep_merge
from foca.factories import connexion_app as factory_module
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config

DIR = Path(__file__).parent.parent / "test_files"
PATH = str(DIR / "openapi_2_petstore.original.yaml")
PATH_ADDITION = str(DIR / "openapi_2_petstore.addition.yaml")
TEST_CONFIG_INSTANCE = Config()
TEST_CONFIG_MODEL = "tests.test_files.model_valid.CustomConfig"
TEST_CONFIG_MODEL_NOT_EXISTS = "tests.test_files.model_valid.NotExists"
TEST_CONFIG_MODEL_MODULE_NOT_EXISTS = "tests.test_files.not_a_module.NotExists"
TEST_DICT: dict[str, Any] = {}
TEST_FILE = "tests/test_files/conf_valid.yaml"
TEST_FILE_CUSTOM_INVALID = "tests/test_files/conf_valid_custom_invalid.yaml"
TEST_FILE_INVALID = "tests/test_files/conf_invalid_log_level.yaml"
TEST_FILE_INVALID_YAML = "tests/test_files/conf_no_yaml.txt"
TEST_FILE_INVALID_LOG = "tests/test_files/conf_log_invalid.yaml"


def test_config_parser_valid_config_file() -> None:
    """Test valid YAML parsing."""
    conf = ConfigParser(Path(TEST_FILE))
    assert type(conf.config.model_dump()) is type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_config_parser_invalid_config_file() -> None:
    """Test invalid YAML parsing."""
    with pytest.raises(ValidationError):
        ConfigParser(Path(TEST_FILE_INVALID))


def test_config_parser_invalid_file_path() -> None:
    """Test invalid file path."""
    conf = ConfigParser(Path(TEST_FILE))
    with pytest.raises(OSError):
        assert conf.parse_yaml(Path("")) is not None


def test_config_parser_invalid_log_config() -> None:
    """Test invalid log config YAML."""
    conf = ConfigParser(Path(TEST_FILE_INVALID_LOG))
    assert type(conf.config.model_dump()) is type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_config_parser_with_custom_config_model() -> None:
    """Test with valid custom config model class."""
    conf = ConfigParser(
        config_file=Path(TEST_FILE),
        custom_config_model=TEST_CONFIG_MODEL,
    )
    assert isinstance(conf.config.custom.param, str)  # type: ignore[attr-defined]  # extra field
    assert conf.config.custom.param == "STRING"  # type: ignore[attr-defined]  # extra field


def test_process_yaml_valid_config_file() -> None:
    """Test process_yaml with valid YAML file."""
    result = ConfigParser.parse_yaml(Path(TEST_FILE))
    assert isinstance(result, dict)


def test_process_yaml_invalid_config_file() -> None:
    """Test process_yaml with invalid YAML file."""
    with pytest.raises(ValueError):
        ConfigParser.parse_yaml(Path(TEST_FILE_INVALID_YAML))


def test_process_yaml_missing_file() -> None:
    """Test process_yaml when file cannot be opened."""
    with mock.patch("foca.config.config_parser.open") as mock_open:
        mock_open.side_effect = OSError
        with pytest.raises(OSError):
            ConfigParser.parse_yaml(Path(TEST_FILE))


def test_merge_yaml_with_no_args() -> None:
    """Test merge_yaml with no arguments."""
    empty_list: list[Path] = []
    res = ConfigParser.merge_yaml(*empty_list)
    assert res == {}


def test_merge_yaml_with_two_args() -> None:
    """Test merge_yaml with no arguments."""
    yaml_list = [Path(PATH), Path(PATH_ADDITION)]
    res = ConfigParser.merge_yaml(*yaml_list)
    assert "put" in res["paths"]["/pets/{petId}"]


def test_parse_custom_config_valid_model() -> None:
    """Test ``.parse_custom_config()`` with a valid model class."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    result = conf.parse_custom_config(model=TEST_CONFIG_MODEL)
    assert isinstance(result, BaseModel)
    assert isinstance(result.param, str)  # type: ignore[attr-defined]  # `CustomConfig` field
    assert result.param == "STRING"  # type: ignore[attr-defined]  # `CustomConfig` field


def test_parse_custom_config_model_module_not_exists() -> None:
    """Test ``.parse_custom_config()`` when module does not exist."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL_MODULE_NOT_EXISTS)


def test_parse_custom_config_model_not_exists() -> None:
    """Test ``.parse_custom_config()`` when model class does not exist."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL_NOT_EXISTS)


def test_parse_custom_config_model_not_dotted() -> None:
    """Test ``.parse_custom_config()`` when model path has no module."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model="CustomConfig")


def test_parse_custom_config_invalid() -> None:
    """Test ``.parse_custom_config()`` when model class does not exist."""
    conf = ConfigParser(config_file=Path(TEST_FILE_CUSTOM_INVALID))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL)


@pytest.mark.parametrize(
    "base,overlay,expected",
    [
        # nested mappings are merged
        (
            {"a": {"b": 1, "c": {"d": 2, "e": 3}}},
            {"a": {"c": {"d": 4}, "f": 5}},
            {"a": {"b": 1, "c": {"d": 4, "e": 3}, "f": 5}},
        ),
        # lists and scalars are replaced
        ({"a": [1, 2], "b": 1}, {"a": [3], "b": 2}, {"a": [3], "b": 2}),
        # null sets values to null
        ({"a": {"b": 1}}, {"a": None}, {"a": None}),
        # mappings replace other values and vice versa
        ({"a": 1}, {"a": {"b": 1}}, {"a": {"b": 1}}),
        ({"a": {"b": 1}}, {"a": [1]}, {"a": [1]}),
        ({}, {"a": 1}, {"a": 1}),
    ],
)
def test_deep_merge(
    base: dict[str, Any], overlay: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Test merging configurations."""
    base_copy = deepcopy(base)
    assert deep_merge(base, overlay) == expected
    assert base == base_copy


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def config_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Base config and overlays in different directories."""
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    base = _write(
        tmp_path / "app" / "config.yaml",
        "server:\n  port: 8000\n  host: 127.0.0.1\ncustom:\n  a: [1, 2]\n  b: base\n",
    )
    dev = _write(
        tmp_path / "deployment" / "config.dev.yaml",
        "server:\n  debug: true\ncustom:\n  a: [3]\n",
    )
    prod = _write(
        tmp_path / "deployment" / "config.prod.yaml",
        "server:\n  port: 80\ncustom:\n  b: null\n",
    )
    return base, dev, prod


class TestConfigFiles:
    """Tests for merging config files and overlays."""

    def test_multiple_files(self, config_files: tuple[Path, Path, Path]) -> None:
        """Config files are merged in the given order."""
        base, dev, prod = config_files
        conf = ConfigParser(config_file=[base, str(dev), prod], format_logs=False)
        assert conf.config_files == [base, dev, prod]
        assert conf.config_dir == base.parent
        server = conf.config.server
        assert (server.host, server.port, server.debug) == (
            "127.0.0.1",
            80,
            True,
        )
        assert conf.config.custom == {"a": [3], "b": None}  # type: ignore[attr-defined]  # extra field

    def test_overlay_env_var(
        self,
        config_files: tuple[Path, Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Overlays are read from `FOCA_CONFIG_OVERLAY`."""
        base, dev, prod = config_files
        # relative overlay paths are resolved against the working directory
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", "deployment/config.dev.yaml")
        conf = ConfigParser(config_file=base, format_logs=False)
        assert conf.config_files == [base, dev]
        assert conf.config.server.debug is True
        assert conf.config.server.port == 8000
        monkeypatch.setenv(
            "FOCA_CONFIG_OVERLAY", f"{dev}:{prod}".replace(":", os.pathsep)
        )
        conf = ConfigParser(config_file=base, format_logs=False)
        assert conf.config_files == [base, dev, prod]
        assert conf.config.server.port == 80
        assert conf.config.custom == {"a": [3], "b": None}  # type: ignore[attr-defined]  # extra field

    def test_overlay_without_config_file(
        self, config_files: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overlays are applied also without a config file."""
        _, dev, _ = config_files
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(dev))
        conf = ConfigParser(format_logs=False)
        assert conf.config_files == [dev]
        assert conf.config_dir == Path.cwd()
        assert conf.config.server.debug is True

    def test_overlay_missing(
        self,
        config_files: tuple[Path, Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Missing overlays raise an error."""
        base, _, _ = config_files
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(tmp_path / "missing.yaml"))
        with pytest.raises(FileNotFoundError, match="FOCA_CONFIG_OVERLAY"):
            ConfigParser(config_file=base, format_logs=False)

    def test_not_mapping(
        self, config_files: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        """Config files that are not YAML mappings raise an error."""
        base, _, _ = config_files
        not_mapping = _write(tmp_path / "list.yaml", "- a\n- b\n")
        with pytest.raises(ValueError, match="not a YAML mapping"):
            ConfigParser(config_file=[base, not_mapping], format_logs=False)

    def test_empty_overlay(
        self, config_files: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        """Empty overlays are ignored."""
        base, _, _ = config_files
        empty = _write(tmp_path / "empty.yaml", "")
        conf = ConfigParser(config_file=[base, empty], format_logs=False)
        assert conf.config.server.port == 8000


NAMED_BASE = f"""
api:
  specs:
    petstore:
      path: {PATH}
      add_operation_fields:
        x-swagger-router-controller: controllers
    addition:
      path: {PATH_ADDITION}
security:
  auth:
    issuers: [https://login.example.org/oidc/]
    audience: [my-app]
  access_control:
    enabled: true
    policies:
      read: [p, "*", /pets, GET]
      write: [p, admin, /pets, POST]
"""


class TestNamedEntries:
    """Tests for overlays changing named entries of specs and policies."""

    @pytest.fixture
    def base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Base config with named specs and policies."""
        monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
        return _write(tmp_path / "config.yaml", NAMED_BASE)

    def test_change_add_remove(self, base: Path, tmp_path: Path) -> None:
        """Overlays change, add and remove single entries."""
        overlay = _write(
            tmp_path / "overlay.yaml",
            "api:\n"
            "  specs:\n"
            "    petstore:\n"
            "      patches: [patch.yaml]\n"
            "    addition: null\n"
            "security:\n"
            "  access_control:\n"
            "    policies:\n"
            "      read: [p, '*', /pets, (GET)|(HEAD)]\n"
            "      write: null\n"
            "      delete: [p, admin, /pets, DELETE]\n",
        )
        conf = ConfigParser(config_file=[base, overlay], format_logs=False).config
        assert len(conf.api.specs) == 1
        spec = conf.api.specs[0]
        assert spec.name == "petstore"
        assert spec.path == [Path(PATH)]
        assert spec.add_operation_fields == {
            "x-swagger-router-controller": "controllers"
        }
        assert spec.patches == [tmp_path / "patch.yaml"]
        assert conf.security.access_control.policies == {
            "read": ["p", "*", "/pets", "(GET)|(HEAD)"],
            "delete": ["p", "admin", "/pets", "DELETE"],
        }

    def test_removed_entry_added_again(self, base: Path, tmp_path: Path) -> None:
        """Entries removed by an overlay can be added by a later one."""
        remove = _write(
            tmp_path / "remove.yaml",
            "security:\n  access_control:\n    policies:\n      read: null\n",
        )
        add = _write(
            tmp_path / "add.yaml",
            "security:\n"
            "  access_control:\n"
            "    policies:\n"
            "      read: [p, '*', /pets, GET]\n",
        )
        conf = ConfigParser(config_file=[base, remove], format_logs=False).config
        assert set(conf.security.access_control.policies or {}) == {"write"}
        conf = ConfigParser(
            config_file=[base, remove, remove], format_logs=False
        ).config
        assert set(conf.security.access_control.policies or {}) == {"write"}
        conf = ConfigParser(config_file=[base, remove, add], format_logs=False).config
        assert set(conf.security.access_control.policies or {}) == {"read", "write"}

    def test_list_replaced(self, base: Path, tmp_path: Path) -> None:
        """Lists in overlays replace named entries, as before."""
        overlay = _write(
            tmp_path / "overlay.yaml",
            f"api:\n  specs:\n    - path: {PATH}\n"
            "security:\n"
            "  access_control:\n"
            "    policies:\n"
            "      - [p, '*', /pets, GET]\n",
        )
        conf = ConfigParser(config_file=[base, overlay], format_logs=False).config
        assert [spec.name for spec in conf.api.specs] == [None]
        assert conf.security.access_control.policies == [["p", "*", "/pets", "GET"]]

    @pytest.mark.parametrize(
        "content,message",
        [
            # removing entries that are not set
            (
                "security:\n  access_control:\n    policies:\n      reed: null\n",
                "removes unknown entries of 'security.access_control.policies': reed",
            ),
            ("api:\n  specs:\n    other: null\n", "'api.specs': other"),
            # changing entries that are not set
            (
                "api:\n  specs:\n    pet-store:\n      patches: [patch.yaml]\n",
                "API spec 'pet-store' has no 'path'",
            ),
        ],
    )
    def test_unknown_names(
        self, base: Path, tmp_path: Path, content: str, message: str
    ) -> None:
        """Overlays naming unknown entries are rejected."""
        overlay = _write(tmp_path / "overlay.yaml", content)
        with pytest.raises((ValueError, ValidationError), match=message):
            ConfigParser(config_file=[base, overlay], format_logs=False)

    @pytest.mark.parametrize(
        "content,section",
        [
            (f"api:\n  specs:\n    - path: {PATH}\n", "api.specs"),
            (
                "security:\n"
                "  auth:\n"
                "    issuers: [https://login.example.org/oidc/]\n"
                "    audience: [my-app]\n"
                "  access_control:\n"
                "    enabled: true\n"
                "    policies:\n"
                "      - [p, '*', /pets, GET]\n",
                "security.access_control.policies",
            ),
        ],
    )
    def test_named_entries_into_list(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        content: str,
        section: str,
    ) -> None:
        """Named entries cannot be merged into lists of earlier files.

        The entries of the list would be lost otherwise.
        """
        monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
        base = _write(tmp_path / "config.yaml", content)
        overlay = _write(tmp_path / "overlay.yaml", NAMED_BASE)
        with pytest.raises(ValueError, match=f"'{section}' to named entries"):
            ConfigParser(config_file=[base, overlay], format_logs=False)

    def test_named_entries_into_empty_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Named entries can replace empty lists, as no entries are lost."""
        monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
        base = _write(tmp_path / "config.yaml", "api:\n  specs: []\n")
        overlay = _write(tmp_path / "overlay.yaml", NAMED_BASE)
        conf = ConfigParser(config_file=[base, overlay], format_logs=False).config
        assert [spec.name for spec in conf.api.specs] == ["petstore", "addition"]

    def test_unknown_name_in_first_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing entries is rejected in the first config file, too."""
        monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
        base = _write(tmp_path / "config.yaml", "api:\n  specs:\n    a: null\n")
        with pytest.raises(ValueError, match=r"'api\.specs': a"):
            ConfigParser(config_file=base, format_logs=False)


def test_no_config_values_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only config section names are logged, not values."""
    messages: list[str] = []

    def debug(msg: str, *args: object) -> None:
        messages.append(msg % args)

    monkeypatch.setattr(parser_module.logger, "debug", debug)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("custom:\n  api_key: s3cr3t\n")
    ConfigParser(config_file=config_file, format_logs=False)
    logged = " ".join(messages)
    assert "s3cr3t" not in logged
    assert "custom" in logged


def test_no_flask_config_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Flask config, including secrets, is not logged."""
    messages: list[str] = []
    monkeypatch.setattr(factory_module.logger, "debug", messages.append)
    app = create_connexion_app(Config())
    logged = " ".join(messages)
    assert "Connexion app configured." in logged
    assert "SECRET_KEY" in app.app.config
    for key in app.app.config:
        assert key not in logged
