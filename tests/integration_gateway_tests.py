"""Integration tests for the gateway example app.

Run against the app, its worker and their services in containers (see
`examples/gateway/docker-compose.yaml`), with the plugin installed in the
image as a distribution: unlike the tests in `tests/plugins`, nothing is faked
or in-process here.
"""

import hashlib
import hmac
import json
import os
from typing import Any

import requests

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8081")
# Seconds; requests to the worker wait for its result, and it may still start
TIMEOUT = 60
PLUGIN = "org.example.gateway.stamp"
# Key secret, as in `examples/gateway/docker-compose.yaml`
KEY_SECRET = "not-a-secret-for-the-example-only"  # noqa: S105
# Namespace of the plugin's store entries: its distribution
NAMESPACE = "foca-gateway-example-plugin"


def _send(path: str, message: str = "hello") -> dict[str, Any]:
    """Send a message to the gateway.

    Args:
        path: Path of the operation.
        message: Message.

    Returns:
        Parsed response.
    """
    response = requests.post(
        url=f"{GATEWAY_URL}{path}", json={"message": message}, timeout=TIMEOUT
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def test_plugin_changes_request_in_app() -> None:
    """The installed plugin changes the request in the app's process."""
    payload = _send("/messages", "from a client")["payload"]
    assert payload["message"] == "from a client"
    assert payload["stamp"] == "stamped by the plugin"
    assert not payload["process"].startswith("ForkPoolWorker")


def test_plugin_changes_request_in_worker() -> None:
    """The installed plugin changes the request in a Celery worker."""
    payload = _send("/messages/worker", "for the worker")["payload"]
    assert payload["message"] == "for the worker"
    assert payload["stamp"] == "stamped by the plugin"
    assert payload["process"].startswith("ForkPoolWorker")


def test_store_is_shared() -> None:
    """App and worker share the plugin's store in MongoDB."""
    first = _send("/messages")["payload"]
    second = _send("/messages/worker")["payload"]
    third = _send("/messages")["payload"]
    assert first["store_mode"] == "shared"
    assert second["store_mode"] == "shared"
    assert first["count"] < second["count"] < third["count"]


def test_key_secret_reaches_app_and_worker() -> None:
    """App and worker key their store with the secret from their environment."""
    message = json.dumps([NAMESPACE, "example"]).encode()
    digest = hmac.new(KEY_SECRET.encode(), message, hashlib.sha256).hexdigest()
    assert _send("/messages")["payload"]["store_key"] == f"hk:{digest}"
    assert _send("/messages/worker")["payload"]["store_key"] == f"hk:{digest}"


def test_provenance_names_installed_plugin() -> None:
    """Provenance records the plugin that ran and the accepted attempt."""
    for path in ("/messages", "/messages/worker"):
        provenance = _send(path)["provenance"]
        assert provenance["engine"]["app"] == {
            "name": "gateway-example",
            "version": "1.0.0",
        }
        assert PLUGIN in provenance["plugins"].values()
        assert all(key.startswith("sha256:") for key in provenance["plugins"])
        execution = next(
            item for item in provenance["executions"] if item.get("plugin_id") == PLUGIN
        )
        assert execution["phase"] == "request"
        assert execution["outcome"] == "ok"
        assert "/payload/stamp" in execution["changes"]["paths"]
        assert [item["outcome"] for item in provenance["attempts"]] == ["accepted"]


def test_invalid_request_400() -> None:
    """Requests that don't match the spec don't reach the plugin."""
    response = requests.post(
        url=f"{GATEWAY_URL}/messages", json={"other": "x"}, timeout=TIMEOUT
    )
    assert response.status_code == 400
