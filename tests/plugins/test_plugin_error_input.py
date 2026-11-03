"""Tests that validation errors of plugin settings don't show their input."""

import traceback
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from foca.plugins import Plugin
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import PluginHost
from foca.plugins.loader import load_plugins
from tests.plugins.fakes import fake_entry_points

LEAK = "SUPERSECRET-123"


class SecretSettings(BaseModel):
    """Settings with a secret, without `hide_input_in_errors`.

    Attributes:
        api_key: Secret.
        limit: Limit.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = SecretStr("default")
    limit: int = 1


class RequiredSettings(BaseModel):
    """Settings that require a field.

    Attributes:
        required: Required field.
        api_key: Secret.
    """

    required: str
    api_key: SecretStr = SecretStr("default")


class SecretPlugin(Plugin[SecretSettings, SecretSettings]):
    """Plugin with secrets in its config and params."""

    Config = SecretSettings
    Params = SecretSettings

    def on_request(self, request: Any, ctx: Any) -> None:
        """Do nothing.

        Args:
            request: Inbound request.
            ctx: Context.
        """


class RequiredPlugin(Plugin[RequiredSettings, RequiredSettings]):
    """Plugin with required fields in its config and params."""

    Config = RequiredSettings
    Params = RequiredSettings

    def on_request(self, request: Any, ctx: Any) -> None:
        """Do nothing.

        Args:
            request: Inbound request.
            ctx: Context.
        """


PLUGINS = {"test.secret": SecretPlugin, "test.required": RequiredPlugin}


def _assert_hidden(error: BaseException) -> None:
    """Check that an error and its traceback, with its chain, hide the secret.

    Args:
        error: Error.
    """
    assert LEAK not in "".join(traceback.format_exception(error))
    assert "input_value" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize(
    ("plugin", "config", "named"),
    [
        ("test.secret", {"apikey": LEAK}, "apikey"),
        ("test.secret", {"api_key": {"nested": LEAK}}, "api_key"),
        ("test.secret", {"limit": LEAK}, "limit"),
        ("test.required", {"api_key": LEAK}, "required"),
    ],
)
def test_plugin_config_errors_hide_input(
    plugin: str, config: dict[str, Any], named: str
) -> None:
    """Invalid admin settings of plugins are named, without their values."""
    conf = PluginsConfig.model_validate(
        {"available": {plugin: {"config": config}}, "pipeline": []}
    )
    with pytest.raises(ValueError, match="Invalid config of plugin") as info:
        load_plugins(conf, api="tes", find_entry_points=fake_entry_points(PLUGINS))
    assert named in str(info.value)
    _assert_hidden(info.value)


@pytest.mark.parametrize(
    ("plugin", "params", "named"),
    [
        ("test.secret", {"apikey": LEAK}, "apikey"),
        ("test.secret", {"limit": LEAK}, "limit"),
    ],
)
def test_plugin_params_errors_hide_input(
    plugin: str, params: dict[str, Any], named: str
) -> None:
    """Invalid params of plugins are named, without their values."""
    conf = PluginsConfig.model_validate(
        {
            "available": {plugin: {}},
            "pipeline": [{"plugin": plugin, "params": params}],
        }
    )
    plugins = load_plugins(
        conf, api="tes", find_entry_points=fake_entry_points(PLUGINS)
    )
    with pytest.raises(ValueError, match="Invalid params of plugin") as info:
        PluginHost(conf, api="tes", plugins=plugins)
    assert named in str(info.value)
    _assert_hidden(info.value)


def test_plugin_required_params_errors_hide_input() -> None:
    """Missing params don't show the other params, as a whole."""
    conf = PluginsConfig.model_validate(
        {
            "available": {"test.required": {"config": {"required": "x"}}},
            "pipeline": [{"plugin": "test.required", "params": {"api_key": LEAK}}],
        }
    )
    plugins = load_plugins(
        conf, api="tes", find_entry_points=fake_entry_points(PLUGINS)
    )
    with pytest.raises(ValueError, match="required") as info:
        PluginHost(conf, api="tes", plugins=plugins)
    _assert_hidden(info.value)
