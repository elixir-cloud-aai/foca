"""
Tests for config_parser.py
"""

import os
from copy import deepcopy
from pathlib import Path
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
TEST_DICT = {}
TEST_FILE = "tests/test_files/conf_valid.yaml"
TEST_FILE_CUSTOM_INVALID = "tests/test_files/conf_valid_custom_invalid.yaml"
TEST_FILE_INVALID = "tests/test_files/conf_invalid_log_level.yaml"
TEST_FILE_INVALID_YAML = "tests/test_files/conf_no_yaml.txt"
TEST_FILE_INVALID_LOG = "tests/test_files/conf_log_invalid.yaml"


def test_config_parser_valid_config_file():
    """Test valid YAML parsing."""
    conf = ConfigParser(Path(TEST_FILE))
    assert type(conf.config.model_dump()) is type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_config_parser_invalid_config_file():
    """Test invalid YAML parsing."""
    with pytest.raises(ValidationError):
        ConfigParser(Path(TEST_FILE_INVALID))


def test_config_parser_invalid_file_path():
    """Test invalid file path."""
    conf = ConfigParser(Path(TEST_FILE))
    with pytest.raises(OSError):
        assert conf.parse_yaml(Path("")) is not None


def test_config_parser_invalid_log_config():
    """Test invalid log config YAML."""
    conf = ConfigParser(Path(TEST_FILE_INVALID_LOG))
    assert type(conf.config.model_dump()) is type(TEST_DICT)
    assert isinstance(conf.config, type(TEST_CONFIG_INSTANCE))


def test_config_parser_with_custom_config_model():
    """Test with valid custom config model class."""
    conf = ConfigParser(
        config_file=Path(TEST_FILE),
        custom_config_model=TEST_CONFIG_MODEL,
    )
    assert isinstance(conf.config.custom.param, str)
    assert conf.config.custom.param == "STRING"


def test_process_yaml_valid_config_file():
    """Test process_yaml with valid YAML file."""
    result = ConfigParser.parse_yaml(Path(TEST_FILE))
    assert isinstance(result, dict)


def test_process_yaml_invalid_config_file():
    """Test process_yaml with invalid YAML file."""
    with pytest.raises(ValueError):
        ConfigParser.parse_yaml(Path(TEST_FILE_INVALID_YAML))


def test_process_yaml_missing_file():
    """Test process_yaml when file cannot be opened."""
    with mock.patch("foca.config.config_parser.open") as mock_open:
        mock_open.side_effect = OSError
        with pytest.raises(OSError):
            ConfigParser.parse_yaml(Path(TEST_FILE))


def test_merge_yaml_with_no_args():
    """Test merge_yaml with no arguments."""
    empty_list = []
    res = ConfigParser.merge_yaml(*empty_list)
    assert res == {}


def test_merge_yaml_with_two_args():
    """Test merge_yaml with no arguments."""
    yaml_list = [Path(PATH), Path(PATH_ADDITION)]
    res = ConfigParser.merge_yaml(*yaml_list)
    assert "put" in res["paths"]["/pets/{petId}"]


def test_parse_custom_config_valid_model():
    """Test ``.parse_custom_config()`` with a valid model class."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    result = conf.parse_custom_config(model=TEST_CONFIG_MODEL)
    assert isinstance(result, BaseModel)
    assert isinstance(result.param, str)
    assert result.param == "STRING"


def test_parse_custom_config_model_module_not_exists():
    """Test ``.parse_custom_config()`` when module does not exist."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL_MODULE_NOT_EXISTS)


def test_parse_custom_config_model_not_exists():
    """Test ``.parse_custom_config()`` when model class does not exist."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model=TEST_CONFIG_MODEL_NOT_EXISTS)


def test_parse_custom_config_model_not_dotted():
    """Test ``.parse_custom_config()`` when model path has no module."""
    conf = ConfigParser(config_file=Path(TEST_FILE))
    with pytest.raises(ValueError):
        conf.parse_custom_config(model="CustomConfig")


def test_parse_custom_config_invalid():
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
def test_deep_merge(base, overlay, expected):
    """Test merging configurations."""
    base_copy = deepcopy(base)
    assert deep_merge(base, overlay) == expected
    assert base == base_copy


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """Base config and overlays in different directories."""
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    base = _write(
        tmp_path / "app" / "config.yaml",
        "server:\n  port: 8000\n  host: 127.0.0.1\n"
        "custom:\n  a: [1, 2]\n  b: base\n",
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

    def test_multiple_files(self, config_files):
        base, dev, prod = config_files
        conf = ConfigParser(
            config_file=[base, str(dev), prod], format_logs=False
        )
        assert conf.config_files == [base, dev, prod]
        assert conf.config_dir == base.parent
        server = conf.config.server
        assert (server.host, server.port, server.debug) == (
            "127.0.0.1",
            80,
            True,
        )
        assert conf.config.custom == {"a": [3], "b": None}

    def test_overlay_env_var(self, config_files, monkeypatch, tmp_path):
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
        assert conf.config.custom == {"a": [3], "b": None}

    def test_overlay_without_config_file(self, config_files, monkeypatch):
        _, dev, _ = config_files
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(dev))
        conf = ConfigParser(format_logs=False)
        assert conf.config_files == [dev]
        assert conf.config_dir == Path.cwd()
        assert conf.config.server.debug is True

    def test_overlay_missing(self, config_files, monkeypatch, tmp_path):
        base, _, _ = config_files
        monkeypatch.setenv(
            "FOCA_CONFIG_OVERLAY", str(tmp_path / "missing.yaml")
        )
        with pytest.raises(FileNotFoundError, match="FOCA_CONFIG_OVERLAY"):
            ConfigParser(config_file=base, format_logs=False)

    def test_not_mapping(self, config_files, tmp_path):
        base, _, _ = config_files
        not_mapping = _write(tmp_path / "list.yaml", "- a\n- b\n")
        with pytest.raises(ValueError, match="not a YAML mapping"):
            ConfigParser(config_file=[base, not_mapping], format_logs=False)

    def test_empty_overlay(self, config_files, tmp_path):
        base, _, _ = config_files
        empty = _write(tmp_path / "empty.yaml", "")
        conf = ConfigParser(config_file=[base, empty], format_logs=False)
        assert conf.config.server.port == 8000


def test_no_config_values_logged(tmp_path, monkeypatch):
    """Only config section names are logged, not values."""
    messages: list[str] = []
    monkeypatch.setattr(parser_module.logger, "debug", messages.append)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("custom:\n  api_key: s3cr3t\n")
    ConfigParser(config_file=config_file, format_logs=False)
    logged = " ".join(messages)
    assert "s3cr3t" not in logged
    assert "custom" in logged


def test_no_flask_config_logged(monkeypatch):
    """The Flask config, including secrets, is not logged."""
    messages: list[str] = []
    monkeypatch.setattr(factory_module.logger, "debug", messages.append)
    app = create_connexion_app(Config())
    app.app.config["SECRET_KEY"] = "s3cr3t"
    create_connexion_app(Config())
    assert "SECRET_KEY" not in " ".join(messages)
