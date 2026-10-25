"""Tests of the per-process plugin host (`foca.plugins.runtime`)."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from yaml import safe_dump

from foca import Foca
from foca import foca as foca_module
from foca.plugins import (
    BackendResponse,
    NotAccepted,
    OutcomeUnknown,
    loader,
    runtime,
)
from foca.plugins.model import OutboundRequest
from foca.plugins.runtime import GatewayApp
from tests.plugins.fakes import fake_entry_points, make_host
from tests.plugins.test_plugin_engine import BACKENDS, PRINCIPAL, inbound


@pytest.fixture
def fake_plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use the fake plugins as installed entry points; reset the runtime."""
    monkeypatch.setattr(loader, "entry_points", lambda group: fake_entry_points()())
    yield
    runtime.reset()


def write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    """Write a configuration file.

    Args:
        tmp_path: Directory.
        data: Configuration.

    Returns:
        Path of the file.
    """
    path = tmp_path / "config.yaml"
    path.write_text(safe_dump(data))
    return path


def test_gateway_app(tmp_path: Path, fake_plugins: None) -> None:
    """Gateway apps check their plugins on startup and get a host per process."""
    config = write_config(
        tmp_path,
        {"plugins": {"available": {"test.tag": {}}, "pipeline": ["test.tag"]}},
    )
    foca = Foca(config_file=config, gateway=GatewayApp(api="tes"))
    foca.create_app()
    host = runtime.get_host()
    assert runtime.get_host() is host
    assert list(host.plugins) == ["test.tag"]
    runtime._runtime.forget()
    current = runtime.get_host()
    assert current is not host
    host.close()
    runtime.configure(GatewayApp(api="tes"))
    assert runtime.get_host() is current
    with pytest.raises(RuntimeError, match="one gateway app"):
        runtime.configure(GatewayApp(api="wes"))
    with pytest.raises(RuntimeError, match="one gateway app"):
        Foca(config_file=config)
    runtime._close_at_exit()
    assert runtime._runtime.host is None
    runtime._close_at_exit()
    runtime.configure(GatewayApp(api="wes"))


def test_gateway_app_invalid_plugins(tmp_path: Path, fake_plugins: None) -> None:
    """Gateway apps with invalid plugins don't start."""
    config = write_config(tmp_path, {"plugins": {"available": {"test.none": {}}}})
    foca = Foca(config_file=config, gateway=GatewayApp(api="tes"))
    with pytest.raises(ValueError, match="not installed"):
        foca.create_app()


def test_plugins_without_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apps that are no gateways warn about a plugins section."""
    config = write_config(tmp_path, {"plugins": {}})
    runtime.reset()
    warnings: list[str] = []
    monkeypatch.setattr(
        foca_module.logger, "warning", lambda message, *args: warnings.append(message)
    )
    Foca(config_file=config).create_app()
    assert any("no gateway" in message for message in warnings)


def test_legacy_keys_before_custom_model(tmp_path: Path) -> None:
    """Legacy keys are refused before the app's custom model validates them."""
    config = write_config(tmp_path, {"custom": {"middlewares": [["x"]]}})
    with pytest.raises(ValueError, match=r"custom\.middlewares"):
        Foca(
            config_file=config,
            custom_config_model="tests.test_files.model_valid.CustomConfig",
            gateway=GatewayApp(api="tes"),
        )
    runtime.reset()
    assert Foca(config_file=config).gateway is None


def test_no_gateway() -> None:
    """Hosts need a gateway app."""
    runtime.reset()
    with pytest.raises(RuntimeError, match="No gateway app"):
        runtime.get_host()


@pytest.mark.filterwarnings("ignore:This process .* fork:DeprecationWarning")
def test_fork_forgets_host(fake_plugins: None, tmp_path: Path) -> None:
    """Forked processes create their own host."""
    config = write_config(tmp_path, {"plugins": {}})
    Foca(config_file=config, gateway=GatewayApp(api="tes")).create_app()
    host = runtime.get_host()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover (child process)
        os.write(write, b"1" if runtime._runtime.host is None else b"0")
        os._exit(0)
    os.waitpid(pid, 0)
    assert os.read(read, 1) == b"1"
    assert runtime.get_host() is host


def test_sanitize_errors_propagate() -> None:
    """Errors of the app's sanitation stop the request unchanged."""

    class TooLarge(Exception):
        code = 413

    def sanitize(payload: Any) -> Any:
        raise TooLarge("inline content")

    host = make_host([{"plugin": "test.tag"}], sanitize=sanitize)
    run = host.start(inbound(), PRINCIPAL)
    with pytest.raises(TooLarge):
        run.request()
    assert run.executions[0].outcome == "error"


def test_validate_outbound() -> None:
    """Outbound requests that the app rejects are not attempted."""

    def validate_outbound(outbound: OutboundRequest) -> None:
        if outbound.backend.id == "a":
            raise ValueError("cannot serialize")

    host = make_host([], validate_outbound=validate_outbound)
    run = host.start(inbound(), PRINCIPAL)
    attempt = next(run.attempts(BACKENDS))
    assert attempt.backend.id == "b"
    assert run.attempt_records[0].reason == "invalid_outbound"
    attempt.succeeded(BackendResponse(status=200))


def test_outcome_exceptions() -> None:
    """Apps report outcomes with exceptions carrying reason codes."""
    host = make_host([])
    run = host.start(inbound(), PRINCIPAL)
    attempts = run.attempts(BACKENDS)
    next(attempts).report(NotAccepted("http_403", "forbidden"))
    next(attempts).report(OutcomeUnknown("read_timeout"))
    with pytest.raises(StopIteration):
        next(attempts)
    assert [(r.outcome, r.reason) for r in run.attempt_records] == [
        ("not_accepted", "http_403"),
        ("unknown", "read_timeout"),
    ]
    with pytest.raises(ValueError, match="Reason"):
        NotAccepted("HTTP 403")
