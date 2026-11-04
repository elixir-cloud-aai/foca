"""Smoke test of the gateway example app, in-process.

Keeps the example working where containers don't run; the plugin is registered
as a fake entry point here. `tests/integration_gateway_tests.py` tests it in
real containers, with the plugin installed.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from flask.testing import FlaskClient

from foca import Foca
from foca.plugins import runtime
from foca.plugins.pytest_plugin import RegisterPlugins
from tests.mock_data import MongoClients

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "gateway"
PLUGIN = "org.example.gateway.stamp"


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    mongo_clients: MongoClients,
    fake_plugin_entry_points: RegisterPlugins,
) -> Iterator[FlaskClient]:
    """Client of the example app, with in-memory MongoDB and fake entry point."""
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR / "plugin"))
    for name in ["controllers", "service", "tasks", "gateway_example_plugin"]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    monkeypatch.setenv("FOCA_PLUGINS_STORE_KEY_SECRET", "secret")
    from gateway_example_plugin import Stamp  # noqa: PLC0415
    from service import GATEWAY  # noqa: PLC0415

    fake_plugin_entry_points({PLUGIN: Stamp})
    foca = Foca(config_file=EXAMPLE_DIR / "config.yaml", gateway=GATEWAY)
    yield foca.create_app().app.test_client()
    runtime.reset()


def test_message(client: FlaskClient) -> None:
    """The plugin stamps messages, counts them and keys them with the secret."""
    for count in (1, 2):
        response = client.post("/messages", json={"message": "hi"})
        assert response.status_code == 200
        assert response.json is not None
        payload = response.json["payload"]
        assert payload["message"] == "hi"
        assert payload["stamp"] == "stamped by the plugin"
        assert payload["count"] == count
        assert payload["store_mode"] == "shared"
        assert payload["store_key"].startswith("hk:")
        provenance = response.json["provenance"]
        assert PLUGIN in provenance["plugins"].values()
        assert [item["outcome"] for item in provenance["attempts"]] == ["accepted"]


def test_invalid_message(client: FlaskClient) -> None:
    """Requests that don't match the spec are refused."""
    assert client.post("/messages", json={"other": "x"}).status_code == 400


def test_worker_not_answering(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that doesn't answer in time gives 503, not an error in the logs."""
    import controllers  # noqa: PLC0415
    from celery.exceptions import TimeoutError as ResultTimeout  # noqa: PLC0415

    celery = mock.Mock()
    celery.send_task.return_value.get.side_effect = ResultTimeout
    monkeypatch.setattr(controllers, "_celery_client", lambda: celery)
    response = client.post("/messages/worker", json={"message": "hi"})
    assert response.status_code == 503
    assert response.json == {"title": "Service Unavailable", "status": 503}


def test_task(client: FlaskClient) -> None:
    """The task of the worker runs the pipeline, too."""
    from tasks import process_message  # noqa: PLC0415

    result = process_message.run({"message": "hi"})
    assert result["payload"]["stamp"] == "stamped by the plugin"
    assert result["payload"]["store_key"].startswith("hk:")
