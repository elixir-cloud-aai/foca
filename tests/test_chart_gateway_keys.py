"""Tests that the library chart knows the settings of FOCA's plugin engine."""

import inspect
import re
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel

from foca.models.config import IssuerConfig
from foca.plugins import config as plugins_config
from foca.plugins.backends import config as backends_config

TEMPLATE = (
    Path(__file__).parents[1] / "charts" / "foca-common" / "templates" / "_gateway.tpl"
)


def _fields(module: ModuleType = plugins_config) -> set[str]:
    """Get the names of the fields of a module's models with several words.

    Args:
        module: Module.

    Returns:
        Names in snake_case.
    """
    return {
        name
        for model in vars(module).values()
        if inspect.isclass(model)
        and issubclass(model, BaseModel)
        and model.__module__ == module.__name__
        for name in model.model_fields
        if "_" in name
    }


def _keys(template: str) -> dict[str, str]:
    """Get a table of keys of the chart.

    Args:
        template: Name of the template with the table.

    Returns:
        Keys in snake_case by those in camelCase.
    """
    text = TEMPLATE.read_text()
    start = text.index(f'define "foca-common.{template}"')
    body = text[start : text.index("{{- end -}}", start)]
    return dict(re.findall(r'"([A-Za-z]+)" "([a-z_]+)"', body))


def test_keys_complete() -> None:
    """All settings of plugins with several words have a key in camelCase."""
    keys = _keys("pluginKeys")
    assert set(keys.values()) == _fields()
    for camel, snake in keys.items():
        first, *rest = snake.split("_")
        assert camel == first + "".join(word.title() for word in rest)


def test_backend_keys_rendered() -> None:
    """The chart's test of backend lists covers all fields with several words."""
    tests = TEMPLATE.parents[1] / "tests" / "test_templates.py"
    text = tests.read_text()
    for field in _fields(backends_config):
        assert f'"{field}"' in text


def test_issuer_settings_complete() -> None:
    """The chart's check of issuers knows the settings of issuers."""
    settings = re.search(r'\$settings := list ((?:"[a-z_]+" ?)+)', TEMPLATE.read_text())
    assert settings is not None
    assert set(re.findall(r'"([a-z_]+)"', settings[1])) == set(
        IssuerConfig.model_fields
    )
