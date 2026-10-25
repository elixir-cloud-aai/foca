"""Tests of the plugin listing (`foca.plugins.api`)."""

import json
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any

import pytest
import yaml
from casbin import Enforcer

from foca import Foca
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config
from foca.plugins import InvalidSelection, Principal, adapter, api, runtime
from foca.plugins.engine import PluginHost
from foca.plugins.pytest_plugin import RegisterPlugins
from foca.plugins.runtime import GatewayApp
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from tests.plugins.fakes import FAKES, make_host
from tests.plugins.test_plugin_engine import inbound

ALICE = Principal(subject="alice", groups=("user",))
ADMIN = Principal(subject="root", groups=("user", "admin"))


def listing_host(**kwargs: Any) -> PluginHost:
    """Create a host with restricted and hidden plugins.

    Args:
        kwargs: Other arguments of `make_host`.

    Returns:
        Host.
    """
    return make_host(
        ["test.tag"],
        available={
            "test.tag": {},
            "test.skip": {"roles": ["admin"]},
            "test.fail": {"client_selectable": False},
            "test.reject": {"enabled": False},
        },
        presets={"tagged": [{"plugin": "test.tag", "params": {"key": "k"}}]},
        **kwargs,
    )


def test_list_plugins_by_role() -> None:
    """Callers see the selectable plugins that their roles allow."""
    host = listing_host(client_pipelines={"roles": ["admin"], "max_steps": 4})
    listing = api.list_plugins(host, ALICE)
    # Alice may not choose pipelines: nothing to choose
    assert listing["plugins"] == []
    assert listing["presets"] == []
    # no steps either: their identifiers default to plugin identifiers
    assert listing["default_pipeline"] == []
    clients = listing["client_pipelines"]
    assert clients["enabled"]
    assert not clients["allowed_for_caller"]
    assert clients["max_steps"] == 4
    assert clients["prepend"] == []
    assert clients["append"] == []
    assert "test." not in json.dumps(listing)
    assert "foca.routing" not in json.dumps(listing)
    assert listing["client_backends"] == {"enabled": True, "max_items": 32}
    assert listing["tag_prefix"] == "gateway."
    admin = api.list_plugins(host, ADMIN)
    assert [plugin["id"] for plugin in admin["plugins"]] == [
        "test.tag",
        "test.skip",
        "foca.routing.ordered",
    ]
    assert admin["presets"] == [
        {"name": "tagged", "steps": [{"id": "test.tag", "plugins": ["test.tag"]}]}
    ]
    assert admin["client_pipelines"]["allowed_for_caller"]
    assert admin["client_pipelines"]["append"] == ["foca.routing.ordered"]
    assert admin["default_pipeline"] == [{"id": "test.tag", "plugins": ["test.tag"]}]
    json.dumps(admin)
    host.close()


def test_listing_without_usable_preset() -> None:
    """Callers allowed presets only, but none of them, see no steps."""
    host = make_host(
        ["test.tag"],
        available={"test.tag": {}, "test.skip": {"roles": ["admin"]}},
        presets={"admins": ["test.skip"]},
        client_pipelines={"allowed": ["presets"]},
    )
    listing = api.list_plugins(host, ALICE)
    assert listing["presets"] == []
    assert listing["default_pipeline"] == []
    assert not listing["client_pipelines"]["allowed_for_caller"]
    admin = api.list_plugins(host, ADMIN)
    assert [preset["name"] for preset in admin["presets"]] == ["admins"]
    assert admin["client_pipelines"]["allowed_for_caller"]
    assert admin["default_pipeline"] == [{"id": "test.tag", "plugins": ["test.tag"]}]
    host.close()


@pytest.mark.parametrize(
    "client_pipelines",
    [
        {},
        {"allowed": ["inline"]},
        {"allowed": ["presets"]},
        {"roles": ["admin"]},
        {"enabled": False},
        {"prepend": [{"id": "first", "plugin": "test.header"}]},
    ],
)
@pytest.mark.parametrize("principal", [ALICE, ADMIN])
def test_listing_matches_selection(
    client_pipelines: dict[str, Any], principal: Principal
) -> None:
    """Listed plugins and presets are exactly those that selection accepts."""
    host = make_host(
        ["test.tag"],
        available={
            "test.tag": {},
            "test.header": {},
            "test.skip": {"roles": ["admin"]},
            "test.fail": {"client_selectable": False},
            "test.reject": {"enabled": False},
        },
        presets={
            "tagged": ["test.tag"],
            "admins": ["test.skip"],
            "hidden": ["test.fail"],
        },
        client_pipelines=client_pipelines,
    )
    listing = api.list_plugins(host, principal)
    listed = {plugin["id"] for plugin in listing["plugins"]}
    for plugin_id in [*host.plugins, "test.reject"]:
        pipeline = json.dumps([{"plugin": plugin_id}])
        assert accepts(host, pipeline, principal) == (plugin_id in listed), plugin_id
    presets = {preset["name"] for preset in listing["presets"]}
    for name in host.presets:
        assert accepts(host, name, principal) == (name in presets), name
    allowed = listing["client_pipelines"]["allowed_for_caller"]
    assert allowed == bool(listed or presets)
    expected = [step.get("id") for step in client_pipelines.get("prepend", [])]
    assert listing["client_pipelines"]["prepend"] == (expected if allowed else [])
    if client_pipelines.get("enabled") is False:
        assert not allowed
    host.close()


def accepts(host: PluginHost, value: str, principal: Principal) -> bool:
    """Check whether selection accepts a pipeline choice.

    Args:
        host: Host.
        value: Value of the pipeline tag.
        principal: Caller.

    Returns:
        Whether a request with the choice starts.
    """
    try:
        host.start(inbound({"tags": {"gateway.pipeline": value}}), principal)
    except InvalidSelection:
        return False
    return True


def test_presets_hidden_if_not_allowed() -> None:
    """Presets are not listed if clients may not choose them."""
    host = listing_host(client_pipelines={"allowed": ["inline"]})
    assert api.list_plugins(host, ALICE)["presets"] == []
    host.close()


def test_describe_plugin() -> None:
    """Descriptions have the class metadata and the parameter schema."""
    host = listing_host()
    plugin = api.get_plugin(host, "test.tag", ALICE)
    assert plugin["title"] == "Add a tag to the payload."
    assert plugin["group"] is None
    assert plugin["phases"] == ["request"]
    assert plugin["capabilities"] == ["tagging"]
    assert plugin["maturity"] == "stable"
    assert set(plugin["params_schema"]["properties"]) == {"key", "value"}
    assert "config" not in plugin
    host.close()


def test_group_and_subgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identifiers `<group>.<subgroup>.<name>` give group and subgroup."""
    host = listing_host()
    monkeypatch.setitem(
        host.plugins, "org.example.tes.policy.images", host.plugins["test.tag"]
    )
    plugin = api.describe_plugin(host, "org.example.tes.policy.images")
    assert (plugin["group"], plugin["subgroup"]) == ("tes", "policy")
    host.close()


@pytest.mark.parametrize("plugin_id", ["test.skip", "test.fail", "test.reject", "x"])
def test_plugin_not_found(plugin_id: str) -> None:
    """Unknown, restricted, hidden and disabled plugins give the same error."""
    host = listing_host()
    with pytest.raises(api.PluginNotFound) as exc_info:
        api.get_plugin(host, plugin_id, ALICE)
    assert exc_info.value.code == 404
    assert exc_info.value.slug == "plugin-not-found"
    host.close()


def test_service_info_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service info gets capabilities, no identifiers or versions."""
    host = listing_host(client_backends={"enabled": False})
    monkeypatch.setattr(runtime, "current_host", lambda: host)
    fields = api.service_info_fields(plugins_url="https://gw.example.org/plugins")
    assert fields == {
        "gateway": {
            "client_pipelines": True,
            "client_backends": False,
            "capabilities": ["tagging"],
            "plugins_url": "https://gw.example.org/plugins",
        }
    }
    assert "plugins_url" not in api.service_info_fields(host)["gateway"]
    assert "test." not in json.dumps(fields)
    host.close()


def test_service_info_fields_before_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_plugin_entry_points: RegisterPlugins,
) -> None:
    """Without a host in use, the service info sets up no plugin."""
    fake_plugin_entry_points(FAKES)
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "available": {"test.tag": {}, "test.reject": {"enabled": False}},
                    "pipeline": ["test.tag"],
                    "client_backends": {"enabled": False},
                }
            }
        )
    )
    Foca(config_file=config, gateway=GatewayApp(api="tes")).create_app()

    def no_host() -> PluginHost:
        raise AssertionError("host created")

    monkeypatch.setattr(runtime, "get_host", no_host)
    monkeypatch.setattr(runtime, "create_host", no_host)
    assert api.service_info_fields() == {
        "gateway": {
            "client_pipelines": True,
            "client_backends": False,
            "capabilities": ["tagging"],
        }
    }
    assert runtime.current_host() is None
    runtime.reset()


def test_service_info_fields_unconfigured() -> None:
    """Without a gateway app, the service info fields cannot be computed."""
    runtime.reset()
    with pytest.raises(RuntimeError, match="No gateway app"):
        api.service_info_fields()


def test_controllers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The controllers serve the listing of the current caller."""
    host = listing_host()
    monkeypatch.setattr(runtime, "get_host", lambda: host)
    monkeypatch.setattr(adapter, "current_principal", lambda: ALICE)
    app = create_connexion_app(Config()).app
    with app.test_request_context():
        assert "test.skip" not in [p["id"] for p in api.listPlugins()["plugins"]]
        assert api.getPlugin("test.tag")["id"] == "test.tag"
        response = api.getPlugin("test.skip")
        assert response.status_code == 404
        body = response.get_json()
        assert body["status"] == 404
        assert body["problem_type"].endswith("#plugin-not-found")
        assert "test.skip" in body["detail"]
    host.close()


def documented_policies() -> list[list[str]]:
    """Get the policies of the listing example in `docs/plugins.md`.

    Returns:
        Policy rules of the example.
    """
    text = (Path(__file__).parents[2] / "docs" / "plugins.md").read_text()
    marker = "allow the listing in the app's policies"
    block = text.split(marker, 1)[1].split("```yaml\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)["security"]["access_control"]["policies"]


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/ga4gh/tes/v1/plugins", True),
        ("/ga4gh/tes/v1/plugins/org.example.tag", True),
        ("/ga4gh/tes/v1/tasks/t1/provenance", True),
        ("/ga4gh/tes/v1/plugins/a/b", False),
        ("/ga4gh/tes/v1/tasks/t1", False),
    ],
)
def test_documented_policies(path: str, allowed: bool) -> None:
    """The documented rules allow both listing endpoints (keyMatch2)."""
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as model:
        enforcer = Enforcer(str(model))
    for ptype, *rule in documented_policies():
        enforcer.add_named_policy(ptype, *rule)
    enforcer.add_grouping_policy("user:alice", "user")
    assert enforcer.enforce("user:alice", path, "GET") is allowed
    assert not enforcer.enforce("user:alice", path, "POST")


def test_spec() -> None:
    """The specification is packaged and refers to the controllers."""
    spec = yaml.safe_load(Path(api.SPEC_PATH).read_text())
    operations = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
    }
    assert operations == {"foca.plugins.api.listPlugins", "foca.plugins.api.getPlugin"}
    for operation_id in operations:
        assert callable(getattr(api, operation_id.rsplit(".", 1)[1]))


def test_spec_served() -> None:
    """Apps can add the specification under their base path."""
    cnx_app = create_connexion_app(Config())
    cnx_app.add_api(api.SPEC_PATH, base_path="/ga4gh/tes/v1")
    client = cnx_app.app.test_client()
    assert client.get("/ga4gh/tes/v1/plugins").status_code == 401


def test_spec_merged(tmp_path: Path, fake_plugin_entry_points: RegisterPlugins) -> None:
    """Apps add the specification to theirs, as documented."""
    fake_plugin_entry_points(FAKES)
    petstore = Path(__file__).parents[1] / "test_files/openapi_3_petstore.original.yaml"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "api": {
                    "specs": [
                        {
                            "path": str(petstore),
                            "connexion": {"resolver_error": 501},
                        }
                    ]
                },
                "plugins": {"available": {"test.tag": {}}, "pipeline": ["test.tag"]},
            }
        )
    )
    foca = Foca(config_file=config, gateway=GatewayApp(api="tes"))
    spec = foca.conf.api.specs[0]
    files = spec.path if isinstance(spec.path, list) else [spec.path]
    spec.path = [Path(api.SPEC_PATH), *files]
    client = foca.create_app().app.test_client()
    response = client.get("/v2/plugins")
    assert response.status_code == 401
    assert json.loads(response.data) == {"title": "Unauthorized", "status": 401}
    # the app's own `info` wins (FOCA's file comes first)
    assert client.get("/v2/openapi.json").get_json()["info"]["title"] == (
        "Swagger Petstore"
    )
    runtime.reset()
