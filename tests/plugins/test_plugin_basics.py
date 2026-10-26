"""Tests of the plugin model, base class, context and configuration."""

import copy
import time
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import foca.plugins
from foca.plugins import Headers, Plugin, Principal, Rejected
from foca.plugins.base import EmptyModel, check_plugin_class
from foca.plugins.config import PluginsConfig, StepConfig, check_plugin_id
from foca.plugins.context import Context
from tests.plugins.fakes import FailingPlugin, TagPlugin


def test_public_names() -> None:
    """Every name that `foca.plugins` exports resolves."""
    missing = [name for name in foca.plugins.__all__ if not hasattr(foca.plugins, name)]
    assert missing == []
    assert len(set(foca.plugins.__all__)) == len(foca.plugins.__all__)


def test_headers() -> None:
    """Headers are case-insensitive and reject line breaks."""
    headers = Headers({"Content-Type": "a"})
    headers["content-type"] = "b"
    headers["X-One"] = "1"
    assert list(headers) == ["content-type", "X-One"]
    assert headers["CONTENT-TYPE"] == "b"
    assert len(headers) == 2
    assert headers == {"Content-Type": "b", "x-one": "1"}
    assert headers != Headers({"x-one": "1"})
    assert headers != ["x"]
    assert repr(headers) == "Headers(['content-type', 'X-One'])"
    copied = headers.copy()
    del copied["x-one"]
    assert "x-one" in headers
    assert "x-one" not in copied
    assert copy.deepcopy(headers) == headers
    with pytest.raises(TypeError):
        headers["x"] = 1  # type: ignore[assignment]
    with pytest.raises(ValueError, match="line breaks"):
        headers["x"] = "a\r\nb"


def test_principal_hides_token() -> None:
    """Tokens are not shown in representations."""
    assert "secret" not in repr(Principal(subject="a", token="secret"))  # noqa: S106


def test_rejected() -> None:
    """Rejections need allowed statuses."""
    rejection = Rejected("no", status=422, title="Nope")
    assert (rejection.status, rejection.title, rejection.detail) == (422, "Nope", "no")
    with pytest.raises(ValueError, match="statuses"):
        Rejected("no", status=500)


def test_plugin_metadata() -> None:
    """Phases and summaries are derived from the class."""
    assert TagPlugin.phases() == ("request",)
    assert TagPlugin.summary() == (
        "Add a tag to the payload.",
        "Sets `tags[key]` of the payload to the prefixed value.",
    )
    assert FailingPlugin.summary() == ("Change the payload, then fail.",) * 2

    class Bare(Plugin[EmptyModel, EmptyModel]):
        title = "Bare"

    assert Bare.summary() == ("Bare", "Bare")
    instance = Bare("x", EmptyModel())
    instance.close()
    assert instance.id == "x"


class _Hooked(Plugin[EmptyModel, EmptyModel]):
    """Plugin with a hook, as base for invalid ones."""

    def on_request(self, request: Any, ctx: Any) -> None:
        """Do nothing.

        Args:
            request: Request.
            ctx: Context.
        """


class _OtherVersion(_Hooked):
    """Plugin of another API version."""

    api_version = 2


class _WesOnly(_Hooked):
    """WES plugin."""

    apis = frozenset({"wes"})


class _BadMaturity(_Hooked):
    """Plugin with an invalid maturity."""

    maturity = "beta"  # type: ignore[assignment]


class _BadModel(_Hooked):
    """Plugin without a pydantic model."""

    Params = dict  # type: ignore[assignment]


class _BadCapability(_Hooked):
    """Plugin with an invalid capability."""

    capabilities: ClassVar[frozenset[str]] = frozenset({"Crypt4GH Input"})


class _BadTitle(_Hooked):
    """Plugin with a title that is no string."""

    title = 1  # type: ignore[assignment]


class _BadTransfers(_Hooked):
    """Plugin with data transfers that are no list of strings."""

    data_transfers = "db-ip.com"  # type: ignore[assignment]


class _BadTransferItem(_Hooked):
    """Plugin with a data transfer that is no string."""

    data_transfers = (object(),)  # type: ignore[assignment]


class _Opaque:
    """Type without JSON Schema."""


class _OpaqueParams(BaseModel):
    """Parameters without JSON Schema."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: _Opaque | None = None


class _NoSchema(_Hooked):
    """Plugin whose parameters have no JSON Schema."""

    Params = _OpaqueParams


class _NoHook(Plugin[EmptyModel, EmptyModel]):
    """Plugin without hooks."""


@pytest.mark.parametrize(
    ("cls", "message"),
    [
        (dict, "not a subclass"),
        (_OtherVersion, "API version 2"),
        (_WesOnly, "does not support"),
        (_NoHook, "no hook"),
        (_BadMaturity, "maturity"),
        (_BadModel, "pydantic"),
        (_BadCapability, "capabilities"),
        (_BadTitle, "invalid title"),
        (_BadTransfers, "data_transfers"),
        (_BadTransferItem, "data_transfers"),
        (_NoSchema, "JSON Schema"),
    ],
)
def test_check_plugin_class(cls: Any, message: str) -> None:
    """Invalid plugin classes are refused."""
    with pytest.raises(ValueError, match=message):
        check_plugin_class(cls, plugin_id="x", api="tes")


def test_context() -> None:
    """Contexts give copies of the original and check annotations."""
    original = {"a": [1]}
    ctx = Context(
        api="tes",
        plugin_id="x",
        principal=Principal(),
        params=EmptyModel(),
        original=original,
        deadline=time.monotonic() + 10,
        state={},
    )
    ctx.original["a"].append(2)
    assert original == {"a": [1]}
    assert 0 < ctx.remaining() <= 10
    ctx.record("n", 1)
    ctx.record("list", ("a", 1, None))
    assert ctx.annotations == {"n": 1, "list": ["a", 1, None]}
    for key, value in [
        ("", 1),
        ("k" * 65, 1),
        ("k", {"a": 1}),
        ("k", list(range(17))),
        ("k", [{"a": 1}]),
        ("k", "x" * 2100),
        ("k", float("nan")),
        ("k", [1.0, float("inf")]),
    ]:
        with pytest.raises(ValueError):
            ctx.record(key, value)


def test_step_config() -> None:
    """Steps have a short form, default identifiers and reserved keys."""
    step = StepConfig.model_validate({"plugin": "a.b", "params": {"x": 1}})
    assert step.id == "a.b"
    assert step.alternatives[0].params == {"x": 1}
    assert StepConfig.model_validate({"plugin": "a.b"}).alternatives[0].params == {}
    for data, message in [
        ({"plugin": "a", "when": "x"}, "reserved"),
        ({"plugin": "a", "alternatives": []}, "either"),
        ({"id": "Bad Id", "plugin": "a"}, "step identifier"),
        ({"plugin": "Bad"}, "plugin identifier"),
        ({"alternatives": []}, "at least 1"),
    ]:
        with pytest.raises(ValidationError, match=message):
            StepConfig.model_validate(data)
    assert StepConfig.model_validate("a.b").id == "a.b"
    with pytest.raises(ValidationError):
        StepConfig.model_validate(1)


def test_plugins_config() -> None:
    """Pipelines refer to allowlisted plugins with unique step identifiers."""
    conf = PluginsConfig.model_validate(
        {"available": {"a.b": {"version": ">=1"}}, "pipeline": [{"plugin": "a.b"}]}
    )
    assert conf.pipelines() == {
        "pipeline": conf.pipeline,
        "prepend": [],
        "append": conf.client_pipelines.append,
    }
    assert list(conf.available) == ["a.b", "foca.routing.ordered"]
    assert conf.client_pipelines.append[0].if_missing_phase == "route"
    assert PluginsConfig().pipeline[0].id == "foca.routing.ordered"
    listed = PluginsConfig.model_validate(
        {"available": {"foca.routing.ordered": {"enabled": False}}}
    )
    assert not listed.available["foca.routing.ordered"].enabled
    for data, message in [
        ({"available": {"Bad": {}}}, "plugin identifier"),
        ({"available": {"foca.other": {}}}, "reserved"),
        (
            {"available": {"foca.routing.ordered": {"version": ">=1"}}},
            "cannot be pinned",
        ),
        ({"available": {"a": {"version": "one"}}}, "version specifier"),
        ({"pipeline": [{"plugin": "a"}]}, "not in 'plugins.available'"),
        (
            {"available": {"a": {}}, "pipeline": [{"plugin": "a"}, {"plugin": "a"}]},
            "Duplicate step",
        ),
        (
            {
                "available": {"a": {}},
                "pipeline": [{"plugin": "a", "if_missing_phase": "route"}],
            },
            "if_missing_phase",
        ),
        ({"gateway_id": "a b"}, "pattern"),
        ({"other": 1}, "Extra inputs"),
    ]:
        with pytest.raises(ValidationError, match=message):
            PluginsConfig.model_validate(data)
    with pytest.raises(ValueError, match="plugin identifier"):
        check_plugin_id("a" * 129)


def test_config_section() -> None:
    """The FOCA configuration has an optional `plugins` section."""
    from foca.models.config import Config  # noqa: PLC0415

    assert Config().plugins is None
    config = Config.model_validate({"plugins": {}})
    assert isinstance(config.plugins, PluginsConfig)
    assert isinstance(config.plugins.defaults.timeout, float | int)
    assert issubclass(EmptyModel, BaseModel)
