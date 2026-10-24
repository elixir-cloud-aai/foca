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
that no worker inherits threads of its parent.
"""

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from foca.plugins.engine import PluginHost
from foca.plugins.model import OutboundRequest


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
    """

    api: str
    validate: Callable[[Any], None] | None = None
    sanitize: Callable[[Any], Any] | None = None
    validate_outbound: Callable[[OutboundRequest], None] | None = None
    name: str | None = None
    version: str | None = None


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
    """Set the gateway settings of the process (done by `Foca(gateway=...)`).

    A host created before is closed.

    Args:
        app: Settings; `None` for apps that are no gateways.
    """
    with _runtime.lock:
        if _runtime.host is not None:
            _runtime.host.close()
        _runtime.app = app
        _runtime.host = None


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
