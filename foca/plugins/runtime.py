"""The plugin host of the process, without the web framework.

Gateway apps declare themselves when creating the FOCA app:

```python
from foca import Foca
from foca.plugins.runtime import GatewayApp

foca = Foca(
    config_file="config.yaml",
    custom_config_model="pro_tes.config_models.CustomConfig",
    gateway=GatewayApp(api="tes", validate=check_task, sanitize=sanitize_task),
)
```

Controllers and Celery tasks then get the host of their process with
:py:func:`get_host`. The host, with its thread pool, is created on first use
in each process, i.e., after Gunicorn or Celery forked their workers, so
that no worker inherits threads of its parent; it is closed at exit.

A process serves one gateway app: FOCA refuses to configure another one
while the host of the first one is in use (cf. :py:func:`configure`).
"""

import atexit
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from foca.plugins.backends import (
    BackendList,
    BackendListConfig,
    RegistryEntry,
    ServiceEntry,
)
from foca.plugins.base import Plugin
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import PluginHost
from foca.plugins.loader import find_plugin_classes
from foca.plugins.model import OutboundRequest

BackendEntries = (
    Sequence[ServiceEntry | RegistryEntry | Mapping[str, Any]] | BackendListConfig
)
"""Entries of a backend list, as models or mappings."""


@dataclass(frozen=True)
class GatewayApp:
    """Settings of a gateway app for its plugin host.

    Attributes:
        api: API of the app, e.g., `tes` or `wes`.
        validate: Payload check (cf. :py:class:`foca.plugins.PluginHost`).
        sanitize: Payload sanitation (cf. :py:class:`foca.plugins.PluginHost`).
        validate_outbound: Outbound check (cf.
            :py:class:`foca.plugins.PluginHost`).
        name: Name of the app, for provenance (e.g., `proTES`).
        version: Version of the app, for provenance.
        backends: Function that gets the entries of the app's backend list
            from the FOCA configuration, e.g.,
            `lambda conf: conf.custom.tes.backends` (cf.
            :py:func:`get_backend_list`).
    """

    api: str
    validate: Callable[[Any], None] | None = None
    sanitize: Callable[[Any], Any] | None = None
    validate_outbound: Callable[[OutboundRequest], None] | None = None
    name: str | None = None
    version: str | None = None
    # the argument is the app's `foca.models.config.Config`, whose app
    # sections (e.g., `custom`) are not typed
    backends: Callable[[Any], BackendEntries] | None = None


class _Runtime:
    """Settings and host of the process."""

    def __init__(self) -> None:
        """Create an empty runtime."""
        self.app: GatewayApp | None = None
        self.host: PluginHost | None = None
        self.lock = threading.Lock()

    def forget(self) -> None:
        """Forget the host, e.g., in a forked process (without closing it)."""
        self.host = None
        self.lock = threading.Lock()


_runtime = _Runtime()
os.register_at_fork(after_in_child=_runtime.forget)


def configure(app: GatewayApp | None) -> None:
    """Set the gateway settings of the process (done by `Foca(...)`).

    Args:
        app: Settings; `None` for apps that are no gateways.

    Raises:
        RuntimeError: The host of other settings is in use (one gateway app
            per process; cf. :py:func:`reset`).
    """
    with _runtime.lock:
        if _runtime.host is not None and app != _runtime.app:
            raise RuntimeError(
                "The plugin host of another gateway app is in use: a process "
                "serves one gateway app"
            )
        _runtime.app = app


def reset() -> None:
    """Close the host of the process and forget the settings, e.g., in tests."""
    with _runtime.lock:
        if _runtime.host is not None:
            _runtime.host.close()
        _runtime.app = None
        _runtime.host = None


def _close_at_exit() -> None:
    """Close the host of the process at exit, if it was created."""
    host = _runtime.host
    if host is not None:
        _runtime.host = None
        host.close()


atexit.register(_close_at_exit)


def create_host() -> PluginHost:
    """Create a plugin host from the configuration of the current FOCA app.

    Returns:
        New host; the caller closes it.

    Raises:
        RuntimeError: No gateway app is configured (cf. :py:func:`configure`),
            or no FOCA app has been created.
        ValueError: The plugins cannot be loaded (cf.
            :py:class:`foca.plugins.PluginHost`).
    """
    from foca.config.access import get_config  # noqa: PLC0415 (import cycle)

    app = _runtime.app
    if app is None:
        raise RuntimeError("No gateway app configured: pass Foca(gateway=...)")
    return PluginHost.from_config(
        get_config(),
        api=app.api,
        validate=app.validate,
        sanitize=app.sanitize,
        validate_outbound=app.validate_outbound,
        app_info={
            key: value
            for key, value in (("name", app.name), ("version", app.version))
            if value
        },
    )


def get_host() -> PluginHost:
    """Get the plugin host of the process, creating it on first use.

    Returns:
        Host.

    Raises:
        RuntimeError: No gateway app is configured, or no FOCA app has been
            created.
        ValueError: The plugins cannot be loaded.
    """
    host = _runtime.host
    if host is not None:
        return host
    with _runtime.lock:
        if _runtime.host is None:
            _runtime.host = create_host()
        return _runtime.host


def current_host() -> PluginHost | None:
    """Get the plugin host of the process if it was created, without creating it.

    Returns:
        Host, or `None` before its first use (cf. :py:func:`get_host`).
    """
    return _runtime.host


def plugin_classes() -> tuple[PluginsConfig, dict[str, type[Plugin[Any, Any]]]]:
    """Get the plugin configuration and classes, without instantiating plugins.

    Used before workers fork (e.g., to compute the service info when the
    app is created), so that no plugin is set up in the parent process.

    Returns:
        Plugin configuration of the current FOCA app (empty if not
        configured) and the classes of its enabled plugins.

    Raises:
        RuntimeError: No gateway app is configured, or no FOCA app has been
            created.
        ValueError: The plugins cannot be loaded.
    """
    from foca.config.access import get_config  # noqa: PLC0415 (import cycle)

    app = _runtime.app
    if app is None:
        raise RuntimeError("No gateway app configured: pass Foca(gateway=...)")
    conf = getattr(get_config(), "plugins", None) or PluginsConfig()
    return conf, find_plugin_classes(conf, app.api)


def get_backend_list() -> BackendList:
    """Get the backend list of the gateway app.

    The list is built from the configuration of the current FOCA app with
    the app's API and `server.public_url`; it is cheap to create, as the
    registry caches are shared per process.

    Returns:
        Backend list.

    Raises:
        RuntimeError: No gateway app is configured, it has no `backends`
            function, or no FOCA app has been created.
        ValueError: The entries are invalid (cf.
            :py:class:`foca.plugins.backends.BackendList`), or the list has
            registry entries but `server.public_url` is not set, which
            keeps the gateway from being one of its own backends.
    """
    from foca.config.access import get_config  # noqa: PLC0415 (import cycle)

    app = _runtime.app
    if app is None or app.backends is None:
        raise RuntimeError(
            "No backend list configured: pass Foca(gateway=GatewayApp(backends=...))"
        )
    config = get_config()
    backends = BackendList(
        app.backends(config), api=app.api, public_url=config.server.public_url
    )
    if backends.public_url is None and backends.has_registries():
        raise ValueError(
            "Backend lists with registry entries need `server.public_url`, so"
            " that the gateway is never one of its own backends"
        )
    return backends
