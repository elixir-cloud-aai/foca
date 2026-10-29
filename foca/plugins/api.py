"""Discovery of gateway plugins: `GET <base>/plugins` and the service info.

Clients (e.g., SDKs, dashboards) that choose pipelines per request list the
plugins and presets they may use with `GET <base>/plugins` (authenticated)
and `GET <base>/plugins/{plugin_id}`. Apps serve both by adding the path
items of :py:data:`SPEC_PATH` (`foca/plugins/api.yaml`) to their API
specification, under their base path (e.g., `/ga4gh/tes/v1/plugins`), and
allow them in their access control policies.

The public service info gets generic capability descriptors only, without
plugin identifiers, distributions or versions, which would map the code a
gateway runs (cf. :py:func:`service_info_fields`).

The listing is plain data of the configuration, the loaded plugins and the
caller; only the controllers use the web framework, via
:py:mod:`foca.plugins.adapter`.
"""

from collections.abc import Iterable
from importlib.resources import files
from typing import Any

from foca.plugins import runtime
from foca.plugins.engine import PluginHost, _has_role, _is_authenticated
from foca.plugins.errors import GatewayError
from foca.plugins.model import Principal
from foca.plugins.records import ResolvedStep

SPEC_PATH = str(files("foca.plugins").joinpath("api.yaml"))
"""Path of the OpenAPI 3 specification of the plugin listing."""


class PluginNotFound(GatewayError):
    """The plugin is unknown, or not available to the caller."""

    code = 404
    title = "Not Found"
    slug = "plugin-not-found"


def _may_choose(host: PluginHost, principal: Principal) -> bool:
    """Check whether a caller may choose pipelines per request.

    Args:
        host: Plugin host.
        principal: Caller.

    Returns:
        Whether client pipelines are enabled, the caller is authenticated
        and has their roles (it may then still have nothing to choose).
    """
    clients = host.conf.client_pipelines
    return (
        clients.enabled
        and _is_authenticated(principal)
        and _has_role(clients.roles, principal)
    )


def _presets(host: PluginHost, principal: Principal) -> dict[str, Any]:
    """Get the presets that a caller may choose.

    Args:
        host: Plugin host.
        principal: Caller.

    Returns:
        Resolved steps of the presets, by name; none unless the caller may
        choose pipelines and presets are allowed.
    """
    if not (
        _may_choose(host, principal) and "presets" in host.conf.client_pipelines.allowed
    ):
        return {}
    return {
        name: steps
        for name, steps in host.presets.items()
        if host.may_use_preset(steps, principal)
    }


def _may_use(host: PluginHost, plugin_id: str, principal: Principal) -> bool:
    """Check whether a caller may choose a plugin in its own pipelines.

    Args:
        host: Plugin host.
        plugin_id: Identifier of the plugin.
        principal: Caller.

    Returns:
        Whether the caller may send pipelines (`inline`), and the plugin is
        loaded (enabled), selectable by clients and, if restricted to roles,
        allowed for one of the caller's roles; as selection checks it.
    """
    loaded = host.plugins.get(plugin_id)
    if (
        loaded is None
        or not _may_choose(host, principal)
        or "inline" not in host.conf.client_pipelines.allowed
    ):
        return False
    available = loaded.available
    return available.client_selectable and _has_role(available.roles, principal)


def describe_plugin(host: PluginHost, plugin_id: str) -> dict[str, Any]:
    """Describe a loaded plugin for clients.

    Args:
        host: Plugin host.
        plugin_id: Identifier of a loaded plugin.

    Returns:
        Identifier, title, description, group and subgroup (from identifiers
        `<group>.<subgroup>.<name>`), phases, capabilities, maturity,
        distribution, version, third-party data transfers, the data that
        the plugin stores and where (`storage`: `shared`, `local` or `off`;
        cf. :py:data:`foca.plugins.store.StoreMode`), and the JSON Schema
        of the parameters; never the admin settings.
    """
    loaded = host.plugins[plugin_id]
    cls = loaded.cls
    title, description = cls.summary()
    parts = plugin_id.split(".")
    return {
        "id": plugin_id,
        "title": title,
        "description": description,
        "group": parts[-3] if len(parts) >= 3 else None,
        "subgroup": parts[-2] if len(parts) >= 3 else None,
        "phases": list(cls.phases()),
        "capabilities": sorted(cls.capabilities),
        "maturity": cls.maturity,
        "distribution": loaded.distribution,
        "version": loaded.version,
        "data_transfers": list(cls.data_transfers),
        "data_stored": list(cls.data_stored),
        "storage": host.store_settings(plugin_id).mode,
        "params_schema": cls.Params.model_json_schema(),
    }


def _steps(steps: Iterable[ResolvedStep]) -> list[dict[str, str | list[str]]]:
    """Describe resolved steps without parameters.

    Args:
        steps: Resolved steps.

    Returns:
        Identifier and plugins of the alternatives of each step.
    """
    return [
        {"id": step.id, "plugins": [alt.plugin for alt in step.alternatives]}
        for step in steps
    ]


def list_plugins(host: PluginHost, principal: Principal) -> dict[str, Any]:
    """List what a caller may choose in per-request pipelines.

    Args:
        host: Plugin host.
        principal: Caller.

    Returns:
        `plugins` the caller may choose in its own pipelines (none unless it
        may send them), `presets` it may choose (none unless presets are
        allowed for it), the steps of the `default_pipeline`, and the
        constraints of `client_pipelines` (with `allowed_for_caller`, and
        the identifiers of the `prepend` and `append` steps),
        `client_backends` and the `tag_prefix`: what selection accepts
        (cf. :py:meth:`foca.plugins.PluginHost.resolve`). Callers without
        any choice (`allowed_for_caller` false) get no steps, as step
        identifiers default to plugin identifiers.
    """
    conf = host.conf
    clients = conf.client_pipelines
    presets = _presets(host, principal)
    # whether selection accepts any choice of the caller; otherwise, steps
    # are not shown, as their identifiers default to plugin identifiers
    allowed = bool(presets) or (
        _may_choose(host, principal) and "inline" in clients.allowed
    )
    return {
        "plugins": [
            describe_plugin(host, plugin_id)
            for plugin_id in host.plugins
            if _may_use(host, plugin_id, principal)
        ],
        "presets": [
            {"name": name, "steps": _steps(steps)} for name, steps in presets.items()
        ],
        "default_pipeline": _steps(host.default_pipeline.steps) if allowed else [],
        "client_pipelines": {
            "enabled": clients.enabled,
            "allowed_for_caller": allowed,
            "allowed": list(clients.allowed),
            "max_steps": clients.max_steps,
            "max_alternatives": clients.max_alternatives,
            "max_size": clients.max_size,
            "prepend": [step.id for step in clients.prepend] if allowed else [],
            "append": [step.id for step in clients.append] if allowed else [],
        },
        "client_backends": {
            "enabled": conf.client_backends.enabled,
            "max_items": conf.client_backends.max_items,
        },
        "tag_prefix": conf.tags.prefix,
    }


def get_plugin(
    host: PluginHost, plugin_id: str, principal: Principal
) -> dict[str, Any]:
    """Describe one plugin that a caller may choose.

    Args:
        host: Plugin host.
        plugin_id: Identifier of the plugin.
        principal: Caller.

    Returns:
        Description (cf. :py:func:`describe_plugin`).

    Raises:
        PluginNotFound: The plugin is unknown, disabled, not selectable or
            not allowed for the caller, or the caller may not send its own
            pipelines (the same error for all, so that callers cannot probe
            the setup).
    """
    if not _may_use(host, plugin_id, principal):
        raise PluginNotFound(f"Plugin not available: {plugin_id}")
    return describe_plugin(host, plugin_id)


def service_info_fields(
    host: PluginHost | None = None, plugins_url: str | None = None
) -> dict[str, Any]:
    """Get the service info extension of a gateway, for public discovery.

    Apps add it in their function of `service_info.runtime_fields`.

    Args:
        host: Plugin host; defaults to that of the process if it was
            created (cf. :py:func:`foca.plugins.runtime.get_host`), else the
            plugin classes are read without instantiating them, so that
            computing the service info when the app is created sets up no
            plugin before workers fork.
        plugins_url: URL of `GET <base>/plugins`, if served.

    Returns:
        Field `gateway` with `plugins_url`, whether clients may choose
        pipelines and backends, and the union of the capability descriptors
        of the enabled plugins; no plugin identifiers, distributions or
        versions.
    """
    if host is None:
        host = runtime.current_host()
    if host is None:
        conf, classes = runtime.plugin_classes()
    else:
        conf = host.conf
        classes = {key: loaded.cls for key, loaded in host.plugins.items()}
    capabilities = sorted(
        {capability for cls in classes.values() for capability in cls.capabilities}
    )
    gateway: dict[str, Any] = {
        "client_pipelines": conf.client_pipelines.enabled,
        "client_backends": conf.client_backends.enabled,
        "capabilities": capabilities,
    }
    if plugins_url is not None:
        gateway["plugins_url"] = plugins_url
    return {"gateway": gateway}


def listFocaPlugins() -> Any:
    """List the plugins of the current caller (`GET <base>/plugins`).

    Returns:
        Listing (cf. :py:func:`list_plugins`).
    """
    from foca.plugins import adapter  # noqa: PLC0415 (web framework, on use)

    return list_plugins(runtime.get_host(), adapter.current_principal())


def getFocaPlugin(plugin_id: str) -> Any:
    """Describe one plugin (`GET <base>/plugins/{plugin_id}`).

    Args:
        plugin_id: Identifier of the plugin.

    Returns:
        Description, or a `404` problem response.
    """
    from foca.plugins import adapter  # noqa: PLC0415 (web framework, on use)

    try:
        return get_plugin(runtime.get_host(), plugin_id, adapter.current_principal())
    except PluginNotFound as exc:
        return adapter.problem(exc)
