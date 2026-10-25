"""Tests of the plugin listing (`foca.plugins.api`)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from foca import Foca
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config
from foca.plugins import InvalidSelection, Principal, adapter, api, runtime
from foca.plugins.engine import PluginHost
from foca.plugins.pytest_plugin import RegisterPlugins
from foca.plugins.runtime import GatewayApp
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
    assert listing["default_pipeline"] == [{"id": "test.tag", "plugins": ["test.tag"]}]
    clients = listing["client_pipelines"]
    assert clients["enabled"]
    assert not clients["allowed_for_caller"]
    assert clients["max_steps"] == 4
    assert clients["prepend"] == []
    assert clients["append"] == ["foca.routing.ordered"]
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
    json.dumps(admin)
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
    prepend = [step.get("id") for step in client_pipelines.get("prepend", [])]
    assert listing["client_pipelines"]["prepend"] == prepend
    if client_pipelines.get("enabled") is False:
        assert not listing["client_pipelines"]["allowed_for_caller"]
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
    monkeypatch.setattr(runtime, "get_host", lambda: host)
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
