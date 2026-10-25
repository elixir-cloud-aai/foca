"""Load allowlisted plugins from installed entry points.

Only entry points of the group `foca.plugins` whose names are listed in
`plugins.available` (and enabled) are imported, so the import-time code of
other installed plugins never runs. Plugins are loaded once per process, at
startup; any problem stops the app with an error (fail fast).
"""

import hashlib
import importlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import (
    EntryPoint,
    PackageNotFoundError,
    distribution,
    entry_points,
)
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from pydantic import ValidationError

from foca.plugins.base import Plugin, check_plugin_class
from foca.plugins.config import (
    BUILTIN_PLUGINS,
    AvailablePluginConfig,
    PluginsConfig,
)

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "foca.plugins"
"""Entry point group of gateway plugins."""

LEGACY_KEYS = ("middlewares",)
"""Configuration keys of earlier gateway middleware stacks (e.g., proTES)."""

_LEGACY_HINT = (
    "Gateway middlewares were replaced by FOCA plugins: remove {key!r} and"
    " configure 'plugins.available' (installed plugins by entry point name)"
    " and 'plugins.pipeline' (steps, each with alternatives); see 'Gateway"
    " plugins' in FOCA's documentation"
)


@dataclass(frozen=True)
class LoadedPlugin:
    """Plugin loaded in this process.

    Attributes:
        id: Identifier of the plugin (entry point name).
        cls: Plugin class.
        instance: Plugin instance of the process.
        distribution: Name of the distribution that provides the plugin.
        version: Version of the distribution.
        entry_point: Object reference of the entry point
            (`module:attribute`).
        record_digest: SHA-256 digest of the distribution's `RECORD` file
            (which lists the hashes of all installed files), as
            `sha256:<hex>`, if available; identifies the installed code.
        available: Allowlist entry of the plugin.
    """

    id: str
    cls: type[Plugin[Any, Any]]
    instance: Plugin[Any, Any]
    distribution: str | None
    version: str | None
    entry_point: str
    record_digest: str | None
    available: AvailablePluginConfig


def check_legacy_keys(config: object) -> None:
    """Refuse configurations with keys of earlier middleware stacks.

    Args:
        config: FOCA configuration (e.g., :py:class:`foca.models.config.Config`),
            whose top level and `custom` section are checked.

    Raises:
        ValueError: The configuration has a key of `LEGACY_KEYS`, with a
            hint on how to migrate.
    """
    for prefix, section in (("", config), ("custom.", _get(config, "custom"))):
        for key in LEGACY_KEYS:
            if _get(section, key) is not None:
                raise ValueError(_LEGACY_HINT.format(key=f"{prefix}{key}"))


def _get(section: object, key: str) -> object:
    """Get a key of a configuration section, which may be a model or mapping.

    Args:
        section: Section, if any.
        key: Key.

    Returns:
        Value, or `None` if not set.
    """
    if isinstance(section, Mapping):
        return section.get(key)
    return getattr(section, key, None)


def _record_digest(entry_point: EntryPoint) -> str | None:
    """Get the digest of the `RECORD` file of an entry point's distribution.

    Args:
        entry_point: Entry point.

    Returns:
        `sha256:<hex>` of the file, or `None` if there is none.
    """
    dist = getattr(entry_point, "dist", None)
    record = dist.read_text("RECORD") if dist is not None else None
    if record is None:
        return None
    return f"sha256:{hashlib.sha256(record.encode()).hexdigest()}"


def _find(plugin_id: str, candidates: Iterable[EntryPoint]) -> EntryPoint:
    """Find the single entry point of a plugin.

    Args:
        plugin_id: Identifier of the plugin.
        candidates: Entry points of the group.

    Returns:
        The entry point.

    Raises:
        ValueError: There is no entry point with the name, or more than one
            (from different distributions).
    """
    found = {
        (ep.value, getattr(getattr(ep, "dist", None), "name", None)): ep
        for ep in candidates
        if ep.name == plugin_id
    }
    if not found:
        raise ValueError(f"Plugin {plugin_id!r} is not installed")
    if len(found) > 1:
        raise ValueError(
            f"Plugin {plugin_id!r} is provided by several entry points:"
            f" {sorted(value for value, _ in found)}"
        )
    return next(iter(found.values()))


@dataclass(frozen=True)
class _BuiltinEntryPoint:
    """Entry point of a built-in plugin (cf. `BUILTIN_PLUGINS`).

    Built-ins are no entry points of FOCA's metadata, so that
    `BUILTIN_PLUGINS` is their only source.

    Attributes:
        name: Plugin identifier.
        value: Object reference (`module:attribute`).
        dist: FOCA's distribution, if installed (for versions and RECORD
            digests in provenance).
    """

    name: str
    value: str
    dist: Any = None

    def load(self) -> Any:
        """Import the plugin class.

        Returns:
            The class.
        """
        module, _, attribute = self.value.partition(":")
        return getattr(importlib.import_module(module), attribute)


def _find_builtin(plugin_id: str, candidates: Iterable[EntryPoint]) -> Any:
    """Find the entry point of a built-in plugin.

    Args:
        plugin_id: Identifier of the plugin.
        candidates: Entry points of the group.

    Returns:
        Entry point built from the plugin's object reference.

    Raises:
        ValueError: Another distribution provides an entry point with the
            identifier (all of them are checked; entry points of FOCA's own,
            older metadata with the same reference are ignored).
    """
    value = BUILTIN_PLUGINS[plugin_id]
    for entry_point in candidates:
        if entry_point.name != plugin_id:
            continue
        name = getattr(getattr(entry_point, "dist", None), "name", None)
        if entry_point.value != value or canonicalize_name(name or "") != "foca":
            raise ValueError(
                f"Plugin {plugin_id!r} is built into FOCA; distribution {name!r}"
                " must not provide it"
            )
    try:
        dist: Any = distribution("foca")
    except PackageNotFoundError:  # e.g., source checkouts that are not installed
        dist = None
    return _BuiltinEntryPoint(plugin_id, value, dist)


def _check_pins(
    plugin_id: str, entry_point: EntryPoint, available: AvailablePluginConfig
) -> tuple[str | None, str | None]:
    """Check the distribution and version pins of a plugin.

    Args:
        plugin_id: Identifier of the plugin.
        entry_point: Entry point of the plugin.
        available: Allowlist entry of the plugin.

    Returns:
        Name and version of the distribution, if known.

    Raises:
        ValueError: The distribution or its version do not match the pins.
    """
    dist = getattr(entry_point, "dist", None)
    name = getattr(dist, "name", None)
    version = getattr(dist, "version", None)
    if available.distribution is not None and (
        name is None
        or canonicalize_name(name) != canonicalize_name(available.distribution)
    ):
        raise ValueError(
            f"Plugin {plugin_id!r} is provided by distribution {name!r}, not"
            f" {available.distribution!r}"
        )
    if available.version is not None and (
        version is None
        or not SpecifierSet(available.version).contains(version, prereleases=True)
    ):
        raise ValueError(
            f"Plugin {plugin_id!r} has version {version!r}, which does not match"
            f" {available.version!r}"
        )
    return name, version


def close_plugins(plugins: Iterable[LoadedPlugin]) -> None:
    """Close plugin instances, logging (not raising) their errors.

    Args:
        plugins: Loaded plugins.
    """
    for plugin in plugins:
        try:
            plugin.instance.close()
        except Exception:
            logger.exception("Plugin %s failed to close", plugin.id)


def load_plugins(
    conf: PluginsConfig,
    api: str,
    find_entry_points: Callable[[], Iterable[Any]] | None = None,
) -> dict[str, LoadedPlugin]:
    """Load and instantiate the enabled plugins of the allowlist.

    Args:
        conf: Plugin configuration.
        api: API of the app (e.g., `tes`), which plugins must support.
        find_entry_points: Function returning the entry points of the group
            `foca.plugins` (`importlib.metadata.EntryPoint` or objects with
            the same attributes); defaults to those of the installed
            distributions.

    Returns:
        Loaded plugins, by identifier, in the order of `conf.available`.

    Plugins loaded before an error are closed.

    Raises:
        ValueError: A plugin is not installed, provided several times, does
            not match its pins, is not a valid plugin for the API (cf.
            :py:func:`foca.plugins.base.check_plugin_class`), or its admin
            settings are invalid.
    """
    if find_entry_points is None:

        def find_entry_points() -> Iterable[Any]:
            return entry_points(group=ENTRY_POINT_GROUP)

    candidates = list(find_entry_points())
    loaded: dict[str, LoadedPlugin] = {}
    try:
        for plugin_id, available in conf.available.items():
            if not available.enabled:
                logger.info("Plugin %s is disabled", plugin_id)
                continue
            entry_point = (
                _find_builtin(plugin_id, candidates)
                if plugin_id in BUILTIN_PLUGINS
                else _find(plugin_id, candidates)
            )
            distribution, version = _check_pins(plugin_id, entry_point, available)
            cls = check_plugin_class(entry_point.load(), plugin_id=plugin_id, api=api)
            try:
                config = cls.Config.model_validate(available.config)
            except ValidationError as exc:
                raise ValueError(
                    f"Invalid config of plugin {plugin_id!r}: {exc}"
                ) from exc
            loaded[plugin_id] = LoadedPlugin(
                id=plugin_id,
                cls=cls,
                instance=cls(plugin_id, config),
                distribution=distribution,
                version=version,
                entry_point=entry_point.value,
                record_digest=_record_digest(entry_point),
                available=available,
            )
            logger.info(
                "Plugin %s loaded (%s %s)",
                plugin_id,
                distribution or "?",
                version or "?",
            )
    except BaseException:
        close_plugins(loaded.values())
        raise
    return loaded
