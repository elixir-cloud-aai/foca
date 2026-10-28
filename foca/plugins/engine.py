"""Engine that runs the plugin pipelines of gateway requests.

Apps create one :py:class:`PluginHost` per process, at startup, and one
:py:class:`PipelineRun` per gateway request (e.g., `POST /tasks`), after
authentication, validation, access control and their own sanitation:

```python
host = PluginHost.from_config(get_config(), api="tes", validate=check_task)

run = host.start(inbound, principal)          # e.g., from foca.plugins.adapter
inbound = run.request()                       # phase `request`
candidates = run.route(backends)              # phase `route`
for attempt in run.attempts(candidates):      # phase `forward`, per backend
    try:
        result = send(attempt.outbound, timeout=attempt.remaining())
    except NotAccepted as exc:                # not accepted: next backend;
        attempt.report(exc)                   # OutcomeUnknown (a subclass):
        continue                              # stop, without raising
    response = attempt.succeeded(result, remote_id=result.payload["id"])
    break
if run.final_reason == "outcome_unknown":     # a backend may have it
    ...
record = run.provenance()                     # e.g., with ProvenanceStore
```

Semantics:

- Each alternative of a step works on a copy of its input, which is kept
  only if the alternative succeeds; otherwise the next alternative runs on
  the unchanged input.
- `NotApplicable`, other exceptions, timeouts and invalid output (e.g., a
  payload that the app's `validate` rejects) make the engine try the next
  alternative; `Rejected` stops the request at once.
- When all alternatives of a step failed, the request fails
  (:py:class:`PipelineFailed`, `500`), unless the step has
  `on_failure: skip`. In the `forward` phase, the attempt fails instead and
  the next backend is tried; in the `response` phase, the step is skipped,
  as the backend accepted the request already.
- Hooks run in a bounded thread pool and are abandoned at their deadline;
  as they work on copies, abandoned hooks cannot change the request.
- Outbound requests carry a `Via` header with the gateway's name, and
  inbound requests that already passed this gateway are refused
  (:py:class:`LoopDetected`); backends at or below the gateway's own public
  URL are never candidates.
"""

import copy
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name
from pydantic import ValidationError

from foca.plugins.base import (
    API_VERSION,
    InvalidParams,
    NotApplicable,
    PluginError,
    Rejected,
)
from foca.plugins.checks import (
    _check_forward,
    _check_response,
    _check_route,
    _via_names,
    default_outbound,
)
from foca.plugins.config import (
    BUILTIN_PLUGINS,
    DEFAULT_ROUTER,
    GATEWAY_ID_PATTERN,
    PRESET_PATTERN,
    PluginsConfig,
    StepConfig,
    check_pipeline,
)
from foca.plugins.context import Context
from foca.plugins.errors import (
    ForwardTimeout,
    GatewayError,
    InvalidSelection,
    InvalidVia,
    LoopDetected,
    NoBackendAvailable,
    PipelineFailed,
    PipelineRejected,
)
from foca.plugins.executor import _Executor
from foca.plugins.loader import (
    LoadedPlugin,
    check_legacy_keys,
    close_plugins,
    load_plugins,
)
from foca.plugins.model import (
    Backend,
    BackendResponse,
    FrozenMapping,
    InboundRequest,
    OutboundRequest,
    Principal,
)
from foca.plugins.outcomes import (
    _REASON,
    NotAccepted,
    OutcomeUnknown,
    _AppError,
    _DeadlineExceededError,
    _InvalidOutputError,
    _NoEnabledPluginError,
    _StepFailedError,
    describe_error,
    sanitize_error,
)
from foca.plugins.provenance import (
    MAX_RUN_CHANGES_SIZE,
    build_key,
    clean_url,
    content_hash,
    describe_changes,
    limit_record,
)
from foca.plugins.records import (
    AttemptOutcome,
    AttemptRecord,
    Execution,
    ResolvedAlternative,
    ResolvedPipeline,
    ResolvedStep,
    _foca_version,
    _utc_now,
)
from foca.plugins.selection import (
    ClientSelection,
    apply_backend_selection,
    parse_client_pipeline,
    parse_client_tags,
)
from foca.plugins.store import (
    STORE_COLLECTION,
    MemoryStoreBackend,
    MongoStoreBackend,
    Store,
    StoreBackend,
    StoreMode,
    StoreSettings,
)
from foca.plugins.tags import check_reserved_tags
from foca.utils.http import GuardedClient

# Imported for type checking only: the config models import this module
if TYPE_CHECKING:
    from foca.models.config import MongoConfig

logger = logging.getLogger(__name__)

MAX_VIA_LENGTH = 1024
"""Maximum length of outbound `Via` headers (the inbound header plus this
gateway's entry), in characters."""

_GATEWAY_ID = re.compile(GATEWAY_ID_PATTERN)


def _default_gateway_id(public_url: str | None) -> str | None:
    """Derive the name of the gateway in `Via` headers from its public URL.

    Args:
        public_url: Public URL of the app, if known.

    Returns:
        Host (IPv6 addresses in brackets) and port (if any) of the URL,
        lowercase, without user information; `None` if there is no URL, or
        the result does not match the pattern of `plugins.gateway_id`.
    """
    if not public_url:
        return None
    try:
        parts = urlsplit(public_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    name = f"{host}:{port}" if port is not None else host
    return name if _GATEWAY_ID.fullmatch(name) else None


def _store_namespace(plugin: LoadedPlugin) -> str:
    """Get the namespace of a plugin's store entries.

    Args:
        plugin: Loaded plugin.

    Returns:
        `foca` for built-in plugins; otherwise the normalized name of the
        plugin's distribution (PEP 503), shared by all plugins of the
        distribution; without known distribution (e.g., in tests),
        `plugin:` and the plugin identifier, which no distribution name
        can be.
    """
    if plugin.id in BUILTIN_PLUGINS:
        return "foca"
    if plugin.distribution:
        return canonicalize_name(plugin.distribution)
    return f"plugin:{plugin.id}"


def _store_settings(
    plugins: Mapping[str, LoadedPlugin],
    conf: PluginsConfig,
    backend: StoreBackend,
) -> dict[str, StoreSettings]:
    """Get the store settings of the loaded plugins.

    Args:
        plugins: Loaded plugins, by identifier.
        conf: Plugin configuration.
        backend: Backend of the stores.

    Returns:
        Settings, by plugin identifier. Plugins whose store is turned off
        share one memory backend.
    """
    memory = MemoryStoreBackend()
    settings = {}
    for plugin_id, plugin in plugins.items():
        effective = plugin.available.store.apply(conf.defaults.store)
        mode: StoreMode
        plugin_backend: StoreBackend
        if not effective.enabled:
            mode, plugin_backend = "off", memory
        else:
            mode = "shared" if backend.shared else "local"
            plugin_backend = backend
        settings[plugin_id] = StoreSettings(
            namespace=_store_namespace(plugin),
            mode=mode,
            backend=plugin_backend,
            max_entry_size=effective.max_entry_size,
            max_ttl=effective.max_ttl,
        )
    modes: dict[str, set[str]] = {}
    for item in settings.values():
        modes.setdefault(item.namespace, set()).add(item.mode)
    for namespace, found in modes.items():
        if len(found) > 1:
            logger.warning(
                "Plugins of namespace %s keep their store entries apart (%s):"
                " turn the store on or off for all plugins of a distribution",
                namespace,
                ", ".join(sorted(found)),
            )
    return settings


def _mongo_store_backend(db: "MongoConfig | None") -> MongoStoreBackend | None:
    """Get the store backend of the app's database, if any.

    Args:
        db: `db` section of the FOCA configuration
            (:py:class:`foca.models.config.MongoConfig`), if any.

    Returns:
        Backend with the collection `STORE_COLLECTION` of the first
        database of `db.dbs`; `None` if no database is configured.
    """
    # imported here: the config models import the plugin modules
    from foca.database.register_mongodb import (  # noqa: PLC0415
        get_mongo_client,
        get_mongo_db_name,
    )

    if db is None or not db.dbs:
        return None
    name, db_conf = next(iter(db.dbs.items()))
    database = db_conf.client
    if database is None:  # not registered (yet), e.g., in tests
        database = get_mongo_client(db)[get_mongo_db_name(name)]
    return MongoStoreBackend(database[STORE_COLLECTION])


class _Default(Enum):
    """Sentinels of default arguments."""

    FROM_PAYLOAD = "from_payload"


_FROM_PAYLOAD: Final = _Default.FROM_PAYLOAD
"""Default of `PluginHost.start(tags=...)`: the tags of the payload (TES)."""


def _has_role(roles: Sequence[str], principal: Principal) -> bool:
    """Check whether a caller has one of some roles.

    Args:
        roles: Roles; any caller has them if empty.
        principal: Authenticated caller.

    Returns:
        Whether the roles are empty or the caller is in one of them.
    """
    return not roles or bool(set(roles) & set(principal.groups))


def _is_default_route_step(step: ResolvedStep) -> bool:
    """Check whether a step is the default route step without params.

    Args:
        step: Resolved step.

    Returns:
        Whether all alternatives are the default router with default params.
    """
    return all(
        alternative.plugin == DEFAULT_ROUTER
        and not alternative.params.model_dump(exclude_defaults=True)
        for alternative in step.alternatives
    )


def _has_step(steps: Sequence[ResolvedStep], step: ResolvedStep) -> bool:
    """Check whether steps have a step with the same effect as another.

    Args:
        steps: Resolved steps.
        step: Resolved step.

    Returns:
        Whether a step has the same alternatives (plugins and params),
        `on_failure` and `timeout` (identifiers may differ).
    """
    return any(
        kept.alternatives == step.alternatives
        and kept.on_failure == step.on_failure
        and kept.timeout == step.timeout
        for kept in steps
    )


def _is_authenticated(principal: Principal) -> bool:
    """Check whether a caller is authenticated.

    Args:
        principal: Caller.

    Returns:
        Whether a token with the caller's identity was validated (i.e.,
        the caller has a subject); only such callers may choose pipelines
        and backends per request.
    """
    return principal.subject is not None


class PluginHost:
    """Plugins of a process, and the factory of pipeline runs.

    Attributes:
        conf: Plugin configuration.
        api: API of the app, e.g., `tes`.
        plugins: Loaded plugins, by identifier.
        public_url: Public URL of the gateway, normalized, if known.
        gateway_id: Name of the gateway in `Via` headers, if known.
        app_info: Name and version of the app, for provenance.
        default_pipeline: Resolved default pipeline.
        presets: Resolved presets, by name; presets that need a disabled
            plugin are left out.
    """

    def __init__(
        self,
        conf: PluginsConfig,
        *,
        api: str,
        validate: Callable[[Any], None] | None = None,
        sanitize: Callable[[Any], Any] | None = None,
        validate_outbound: Callable[[OutboundRequest], None] | None = None,
        public_url: str | None = None,
        plugins: Mapping[str, LoadedPlugin] | None = None,
        app_info: Mapping[str, str] | None = None,
        http_client: Callable[..., GuardedClient] = GuardedClient,
        store_backend: StoreBackend | None = None,
    ) -> None:
        """Load the plugins and resolve the default pipeline.

        The app's callbacks (`validate`, `sanitize`, `validate_outbound`, and
        `build` of :py:meth:`PipelineRun.attempts`) are trusted code that
        runs outside the plugin deadlines, so they must be fast.

        Args:
            conf: Plugin configuration.
            api: API of the app (e.g., `tes`, `wes`), which plugins must
                support.
            validate: Function that checks the payload of an inbound request
                (e.g., against the app's model), raising an exception if it
                is invalid; called after each successful `request` hook. If
                it raises, the hook's output counts as invalid, and the next
                alternative is tried.
            sanitize: Function that returns the payload of an inbound
                request after the app's sanitation (e.g., removing
                credentials from URLs); called after each successful
                `request` hook, before `validate`. Its exceptions (e.g., a
                `413` for too much inline content) stop the request and
                reach the caller unchanged.
            validate_outbound: Function that checks the outbound request of
                an attempt after the `forward` phase (e.g., that the app can
                serialize its payload), raising an exception if it is
                invalid; the attempt then counts as not accepted (reason
                `invalid_outbound`), and the next backend is tried.
            public_url: Public URL of the gateway, normalized (e.g.,
                `server.public_url`).
            plugins: Loaded plugins, e.g., for tests (cf.
                :py:mod:`foca.plugins.testing`); by default, the plugins of
                the allowlist are loaded from their entry points.
            app_info: Name and version of the app, for provenance (e.g.,
                `{"name": "proTES", "version": "0.4.0"}`).
            http_client: Factory of the `ctx.http` clients of calls, called
                with the keyword arguments of
                :py:class:`foca.utils.http.GuardedClient` (e.g., a fake for
                tests, cf. :py:class:`foca.plugins.testing.FakeHttp`).
            store_backend: Backend of the plugins' stores (`ctx.store`),
                e.g., :py:class:`foca.plugins.store.MongoStoreBackend`; by
                default, each process keeps the entries in memory. Plugins
                whose store is turned off (`store.enabled`) keep theirs in
                memory anyway.

        Raises:
            ValueError: Plugins cannot be loaded (cf.
                :py:func:`foca.plugins.loader.load_plugins`), or the default
                pipeline is invalid; the plugins are closed then.
        """
        self.conf = conf
        self.api = api
        self._validate = validate
        self._sanitize = sanitize
        self._validate_outbound = validate_outbound
        self.public_url = public_url.rstrip("/") if public_url else None
        self.app_info: dict[str, str] = dict(app_info or {})
        self._http_client = http_client
        self.gateway_id = conf.gateway_id or _default_gateway_id(public_url)
        self.plugins: dict[str, LoadedPlugin] = (
            dict(plugins) if plugins is not None else load_plugins(conf, api=api)
        )
        self._store_backend: StoreBackend = store_backend or MemoryStoreBackend()
        self._stores = _store_settings(self.plugins, conf, self._store_backend)
        self._executor = _Executor(conf.defaults.workers)
        try:
            self.default_pipeline = ResolvedPipeline(
                steps=self.resolve_steps(conf.pipeline, origin="pipeline")
            )
            frame = conf.client_pipelines
            self._prepend = self.resolve_steps(frame.prepend, origin="prepend")
            self._append = [
                (step.if_missing_phase, resolved)
                for step in frame.append
                for resolved in self.resolve_steps([step], origin="append")
            ]
            self.presets: dict[str, tuple[ResolvedStep, ...]] = {}
            for name, steps in conf.presets.items():
                try:
                    self.presets[name] = self.resolve_steps(steps, origin="preset")
                except _NoEnabledPluginError as exc:
                    logger.warning("Preset %s is unavailable: %s", name, exc)
                    continue
                _, clashes = self._framed(self.presets[name])
                if clashes:
                    raise ValueError(
                        f"Step {clashes[0]!r} of presets.{name} has the identifier"
                        " of a step of 'client_pipelines.prepend' or 'append';"
                        " set an explicit `id`"
                    )
            self._warn_skippable_steps()
        except BaseException:
            self.close()
            raise

    def _warn_skippable_steps(self) -> None:
        """Warn about steps of the default pipeline that clients can skip.

        A client's pipeline or preset replaces the default pipeline, so a
        step of it runs for clients' choices only if the frame has the same
        step: a step of `client_pipelines.prepend`, or of `append` without
        `if_missing_phase` (which clients can make it drop), with the same
        alternatives (plugins and params), `on_failure` and `timeout`; or,
        if clients may choose presets only, if every preset has it. The
        default route step without params is exempt, as it only reorders
        the candidates (it filters none).
        """
        conf = self.conf.client_pipelines
        if not conf.enabled:
            return
        frame = [
            *self._prepend,
            *(step for phase, step in self._append if phase is None),
        ]
        # what clients can choose instead of the default pipeline
        choices = [()] if "inline" in conf.allowed else list(self.presets.values())
        skippable = [
            step.id
            for step in self.default_pipeline.steps
            if not _is_default_route_step(step)
            and any(not _has_step([*frame, *steps], step) for steps in choices)
        ]
        if skippable:
            logger.warning(
                "Client pipelines are enabled, so clients can skip steps of"
                " plugins.pipeline that client_pipelines.prepend (or append,"
                " without if_missing_phase) lacks: %s; repeat steps that must"
                " run for every request (e.g., policies) in prepend",
                ", ".join(skippable),
            )

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        api: str,
        validate: Callable[[Any], None] | None = None,
        sanitize: Callable[[Any], Any] | None = None,
        validate_outbound: Callable[[OutboundRequest], None] | None = None,
        app_info: Mapping[str, str] | None = None,
    ) -> "PluginHost":
        """Create the host from the FOCA configuration.

        Args:
            config: FOCA configuration (:py:class:`foca.models.config.Config`).
            api: API of the app.
            validate: Payload check (cf. :py:class:`PluginHost`).
            sanitize: Payload sanitation (cf. :py:class:`PluginHost`).
            validate_outbound: Outbound check (cf. :py:class:`PluginHost`).
            app_info: Name and version of the app (cf. :py:class:`PluginHost`).

        Returns:
            Host with the plugins of `plugins` (none if not configured); the
            plugins' stores keep their entries in the collection
            :py:data:`foca.plugins.store.STORE_COLLECTION` of the first
            database of `db.dbs`, if any, otherwise in memory.

        Raises:
            ValueError: The configuration has keys of earlier middleware
                stacks (cf. :py:func:`foca.plugins.loader.check_legacy_keys`),
                or the plugins cannot be loaded.
        """
        check_legacy_keys(config)
        conf = getattr(config, "plugins", None) or PluginsConfig()
        return cls(
            conf,
            api=api,
            validate=validate,
            sanitize=sanitize,
            validate_outbound=validate_outbound,
            public_url=config.server.public_url,
            app_info=app_info,
            store_backend=_mongo_store_backend(getattr(config, "db", None)),
        )

    def store_settings(self, plugin_id: str) -> StoreSettings:
        """Get the store settings of a loaded plugin.

        Args:
            plugin_id: Identifier of the plugin.

        Returns:
            Settings: namespace, mode, backend and limits.

        Raises:
            KeyError: The plugin is not loaded.
        """
        return self._stores[plugin_id]

    def store(self, plugin_id: str, deadline: float | None = None) -> Store:
        """Get the store of a loaded plugin, e.g., for tests and maintenance.

        Args:
            plugin_id: Identifier of the plugin.
            deadline: `time.monotonic()` value by which calls must end, if
                any.

        Returns:
            Store, as `ctx.store` of the plugin's hooks.

        Raises:
            KeyError: The plugin is not loaded.
        """
        return Store(self._stores[plugin_id], deadline=deadline)

    def setup_store(self) -> None:
        """Prepare the backend of the stores, e.g., create its indexes.

        Called once at startup (cf. `Foca.create_app()`); idempotent.

        Raises:
            pymongo.errors.PyMongoError: The database cannot be reached, or
                an index conflicts with an existing one.
        """
        self._store_backend.setup()

    def resolve_steps(
        self, steps: Sequence[StepConfig], origin: str
    ) -> tuple[ResolvedStep, ...]:
        """Resolve the steps of a pipeline: enabled plugins, validated params.

        Alternatives of disabled plugins are dropped; steps left without
        alternatives are dropped if they may be skipped.

        Args:
            steps: Steps, as configured.
            origin: Where the steps come from, for records.

        Returns:
            Resolved steps.

        Raises:
            ValueError: A plugin is not in the allowlist, the parameters of
                an alternative are invalid, or a step that may not be
                skipped has no enabled alternative.
        """
        resolved = []
        for step in steps:
            alternatives = []
            for alternative in step.alternatives:
                available = self.conf.available.get(alternative.plugin)
                if available is None:
                    raise ValueError(f"Plugin not available: {alternative.plugin}")
                loaded = self.plugins.get(alternative.plugin)
                if loaded is None:
                    continue
                invalid = (
                    f"Invalid params of plugin {alternative.plugin!r} in step"
                    f" {step.id!r}"
                )
                try:
                    params = loaded.cls.Params.model_validate(alternative.params)
                except ValidationError as exc:
                    raise ValueError(f"{invalid}: {exc}") from exc
                try:
                    loaded.instance.check_params(params)
                except InvalidParams as exc:
                    raise ValueError(f"{invalid}: {exc}") from exc
                except (KeyboardInterrupt, GeneratorExit):
                    raise
                except BaseException as exc:  # e.g., `SystemExit`, bugs
                    logger.exception(
                        "Plugin %s failed to check params", alternative.plugin
                    )
                    raise PluginError(
                        f"Plugin {alternative.plugin!r} failed to check params"
                    ) from exc
                alternatives.append(
                    ResolvedAlternative(plugin=alternative.plugin, params=params)
                )
            if not alternatives:
                if step.on_failure == "skip":
                    logger.warning("Step %s has no enabled plugin; dropped", step.id)
                    continue
                raise _NoEnabledPluginError(f"Step {step.id!r} has no enabled plugin")
            resolved.append(
                ResolvedStep(
                    id=step.id or "",
                    alternatives=tuple(alternatives),
                    timeout=step.timeout or self.conf.defaults.timeout,
                    on_failure=step.on_failure,
                    origin=origin,
                )
            )
        return tuple(resolved)

    def start(
        self,
        inbound: InboundRequest,
        principal: Principal,
        *,
        request_id: str | None = None,
        tags: object | _Default = _FROM_PAYLOAD,
    ) -> "PipelineRun":
        """Start the pipeline run of a request.

        The client's choices of pipeline and backends are read from the
        reserved tags (cf. :py:mod:`foca.plugins.selection`) and checked
        before any plugin runs.

        Args:
            inbound: Inbound request, after the app's checks and sanitation.
            principal: Authenticated caller.
            request_id: Identifier of the request, for logs.
            tags: Tags of the request (a string map, or `None`), e.g., the
                parsed `tags` form field of WES; required for APIs other
                than `tes`, whose default is the `tags` of the task.

        Returns:
            Run of the request.

        Raises:
            InvalidVia: The `Via` header of the request is so long that the
                outbound one, with this gateway's entry, would be longer than
                :py:data:`MAX_VIA_LENGTH` characters.
            LoopDetected: The request already passed this gateway.
            foca.plugins.selection.InvalidSelection: The tags are not a
                mapping, or the client's choices are invalid or not allowed
                (`400`), e.g., as the caller is not authenticated.
            TypeError: `tags` is not given for an API other than `tes`.
        """
        own = len(f", 1.1 {self.gateway_id}") if self.gateway_id is not None else 0
        if len(inbound.headers.get("Via", "")) > MAX_VIA_LENGTH - own:
            raise InvalidVia(f"Via header longer than {MAX_VIA_LENGTH} characters")
        if self.gateway_id is not None and self.gateway_id.lower() in _via_names(
            inbound.headers
        ):
            raise LoopDetected(f"Request already passed gateway {self.gateway_id}")
        if tags is _FROM_PAYLOAD:
            if self.api != "tes":
                raise TypeError(f"Pass the tags of {self.api} requests: start(tags=)")
            payload = inbound.payload
            tags = payload.get("tags") if isinstance(payload, Mapping) else None
        conf = self.conf
        selection = parse_client_tags(
            tags,
            prefix=conf.tags.prefix,
            max_pipeline_size=conf.client_pipelines.max_size,
            max_backends=conf.client_backends.max_items,
            pipelines_enabled=conf.client_pipelines.enabled,
            backends_enabled=conf.client_backends.enabled,
        )
        if selection.backends is not None and not _is_authenticated(principal):
            raise InvalidSelection(
                "Choosing backends needs an authenticated caller",
                slug="invalid-backends",
            )
        return PipelineRun(
            self,
            pipeline=self.resolve(selection.pipeline, principal),
            inbound=inbound,
            principal=principal,
            request_id=request_id,
            selection=selection,
        )

    def resolve(self, value: str | None, principal: Principal) -> ResolvedPipeline:
        """Resolve the pipeline of a request from the client's choice.

        Args:
            value: Value of the pipeline tag, if any.
            principal: Authenticated caller.

        Returns:
            Default pipeline without a choice; otherwise the preset or the
            client's pipeline, framed by `client_pipelines.prepend` and
            `append`.

        Raises:
            foca.plugins.selection.InvalidSelection: The choice is invalid
                or not allowed, e.g., as the caller is not authenticated
                (whether choices are enabled at all is checked when the tags
                are read, cf. :py:meth:`start`).
                Plugins that are unknown, not installed, disabled, not
                selectable or not allowed for the caller get the same
                message, as do presets that are unknown or need a role, so
                that clients cannot probe the setup.
        """
        if value is None:
            return self.default_pipeline
        conf = self.conf.client_pipelines
        if not _is_authenticated(principal):
            raise InvalidSelection("Choosing pipelines needs an authenticated caller")
        if not _has_role(conf.roles, principal):
            raise InvalidSelection("Choosing pipelines is not allowed")
        preset: str | None = None
        is_name = PRESET_PATTERN.fullmatch(value) is not None
        if is_name and "presets" in conf.allowed:
            if value not in self.presets or not self.may_use_preset(
                self.presets[value], principal
            ):
                raise InvalidSelection(f"Preset not available: {value}")
            preset = value
            resolved = self.presets[value]
        elif is_name:
            raise InvalidSelection("Presets are not allowed: pipelines are JSON")
        elif "inline" in conf.allowed:
            steps = parse_client_pipeline(value)
            self._check_client_steps(steps, principal)
            try:
                check_pipeline(steps)
                resolved = self.resolve_steps(steps, origin="client")
            except ValueError as exc:
                raise InvalidSelection(str(exc)) from exc
        else:
            raise InvalidSelection("Pipelines are chosen by preset name only")
        return ResolvedPipeline(
            steps=self._frame(resolved),
            source="preset" if preset is not None else "client",
            preset=preset,
        )

    def may_use_preset(
        self, steps: Sequence[ResolvedStep], principal: Principal
    ) -> bool:
        """Check whether a caller has the roles of the plugins of a preset.

        Presets are admin-authored, so they may use plugins that clients
        cannot choose themselves (`client_selectable: false`), but not
        plugins restricted to roles that the caller lacks.

        Args:
            steps: Resolved steps of the preset.
            principal: Authenticated caller.

        Returns:
            Whether the caller may choose the preset.
        """
        return all(
            _has_role(self.conf.available[alternative.plugin].roles, principal)
            for step in steps
            for alternative in step.alternatives
        )

    def _check_client_steps(
        self, steps: Sequence[StepConfig], principal: Principal
    ) -> None:
        """Check the limits and plugins of a client's pipeline.

        Args:
            steps: Steps.
            principal: Authenticated caller.

        Raises:
            foca.plugins.selection.InvalidSelection: The pipeline exceeds the
                limits, or uses a plugin that the caller may not choose.
        """
        conf = self.conf.client_pipelines
        if len(steps) > conf.max_steps:
            raise InvalidSelection(f"At most {conf.max_steps} steps are allowed")
        for step in steps:
            if len(step.alternatives) > conf.max_alternatives:
                raise InvalidSelection(
                    f"At most {conf.max_alternatives} alternatives are allowed"
                )
            for alternative in step.alternatives:
                available = self.conf.available.get(alternative.plugin)
                if (
                    available is None
                    or alternative.plugin not in self.plugins
                    or not available.client_selectable
                    or not _has_role(available.roles, principal)
                ):
                    raise InvalidSelection(
                        f"Plugin not available: {alternative.plugin}"
                    )

    def _frame(self, steps: tuple[ResolvedStep, ...]) -> tuple[ResolvedStep, ...]:
        """Frame the steps of a client's choice with the admin's steps.

        Args:
            steps: Resolved steps of the preset or client pipeline.

        Returns:
            `client_pipelines.prepend`, the steps and `client_pipelines.append`;
            appended steps with `if_missing_phase` only if no other step
            implements the phase.

        Raises:
            foca.plugins.selection.InvalidSelection: A step of the client
                has the identifier of a framing step that is kept.
        """
        framed, clashes = self._framed(steps)
        if clashes:
            raise InvalidSelection(
                "A step identifier is reserved for the gateway's steps; set an"
                " explicit `id`"
            )
        return framed

    def _framed(
        self, steps: tuple[ResolvedStep, ...]
    ) -> tuple[tuple[ResolvedStep, ...], list[str]]:
        """Frame steps, and find those with identifiers of kept frame steps.

        `if_missing_phase` of appended steps checks the given steps only
        (not the prepended ones).

        Args:
            steps: Resolved steps of a preset or client pipeline.

        Returns:
            Framed steps (cf. :py:meth:`_frame`), and the identifiers of the
            steps that clash with the prepended or kept appended steps.
        """
        framed = [*self._prepend, *steps]
        kept = list(self._prepend)
        for phase, step in self._append:
            # only the preset's or client's own steps count, not the frame
            if phase is not None and any(
                phase in self.plugins[alt.plugin].cls.phases()
                for existing in steps
                for alt in existing.alternatives
            ):
                continue
            framed.append(step)
            kept.append(step)
        reserved = {step.id for step in kept}
        return tuple(framed), sorted(step.id for step in steps if step.id in reserved)

    def is_own_url(self, url: str) -> bool:
        """Check whether a URL is at or below the gateway's public URL.

        Args:
            url: Normalized URL.

        Returns:
            Whether the URL is the public URL or below it.
        """
        if self.public_url is None:
            return False
        url = url.rstrip("/")
        return url == self.public_url or url.startswith(f"{self.public_url}/")

    def check_payload(self, payload: Any) -> Any:
        """Sanitize and validate a payload, as after each `request` hook.

        Args:
            payload: Payload.

        Returns:
            Sanitized payload.

        Raises:
            _AppError: Sanitation failed (wrapping the app's exception, which
                the engine raises to the caller).
        """
        if self._sanitize is not None:
            try:
                payload = self._sanitize(payload)
            except Exception as exc:
                raise _AppError(exc) from exc
        if self._validate is not None:
            self._validate(payload)
        return payload

    def call(self, function: Callable[[], Any], timeout: float) -> Any:
        """Call a hook in the bounded thread pool.

        Args:
            function: Function calling the hook.
            timeout: Seconds to wait.

        Returns:
            Result of the hook.
        """
        return self._executor.call(function, timeout)

    def close(self) -> None:
        """Close the plugins and stop the thread pool, e.g., at shutdown."""
        close_plugins(self.plugins.values())
        self._executor.shutdown()


class Attempt:
    """Forwarding attempt to one backend.

    Attributes:
        number: Number of the attempt, from 1.
        backend: Backend.
        outbound: Outbound request, after the `forward` phase.
        state: Outcome, once reported; `None` before.
    """

    def __init__(
        self,
        run: "PipelineRun",
        number: int,
        outbound: OutboundRequest,
        budget_end: float,
    ) -> None:
        """Create the attempt.

        Args:
            run: Run of the request.
            number: Number of the attempt.
            outbound: Outbound request.
            budget_end: `time.monotonic()` value by which forwarding must
                end.
        """
        self._run = run
        self.number = number
        self.backend = outbound.backend
        self.outbound = outbound
        self.state: AttemptOutcome | None = None
        self._budget_end = budget_end
        self._started = time.monotonic()
        self._started_at = _utc_now()

    def remaining(self) -> float:
        """Get the seconds left for this and further attempts.

        Returns:
            Seconds, at least 0; use them as timeout of the request.
        """
        return max(0.0, self._budget_end - time.monotonic())

    def _report(
        self, outcome: AttemptOutcome, reason: str | None, remote_id: str | None
    ) -> None:
        """Record the outcome of the attempt.

        Args:
            outcome: Outcome.
            reason: Reason code, if any.
            remote_id: Identifier of the resource at the backend, if any.

        Raises:
            RuntimeError: The outcome was reported before.
            ValueError: The reason code is invalid.
        """
        if self.state is not None:
            raise RuntimeError("The outcome of the attempt was reported already")
        if reason is not None and not _REASON.fullmatch(reason):
            raise ValueError("Reason codes match [a-z0-9_]{1,64}")
        self.state = outcome
        self._run.attempt_records.append(
            AttemptRecord(
                number=self.number,
                backend=self.backend.id,
                url=self.backend.url,
                outcome=outcome,
                reason=reason,
                remote_id=remote_id,
                started_at=self._started_at,
                duration_ms=round((time.monotonic() - self._started) * 1000, 3),
            )
        )

    def failed(self, reason: str) -> None:
        """Report that the backend certainly did not accept the request.

        The engine then tries the next backend.

        Args:
            reason: Reason code, e.g., `connect_error`, `tls_error`,
                `http_403` or `no_credentials`.
        """
        self._report("not_accepted", reason, None)

    def unknown(self, reason: str) -> None:
        """Report that the backend may have accepted the request.

        The engine then tries no other backend, so that the request is not
        executed twice (e.g., after a read timeout or a `5xx` status).

        Args:
            reason: Reason code, e.g., `read_timeout` or `http_502`.
        """
        self._report("unknown", reason, None)

    def report(self, outcome: NotAccepted) -> None:
        """Report the outcome of the attempt from an exception of the app.

        Args:
            outcome: :py:class:`OutcomeUnknown` (the backend may have
                accepted) or :py:class:`NotAccepted`.
        """
        if isinstance(outcome, OutcomeUnknown):
            self.unknown(outcome.reason)
        else:
            self.failed(outcome.reason)

    def succeeded(
        self, response: BackendResponse, remote_id: str | None = None
    ) -> BackendResponse:
        """Report that the backend accepted the request; run `response` hooks.

        Args:
            response: Response of the backend; `client_payload` and
                `client_headers` are what the client gets, unless plugins
                change them.
            remote_id: Identifier of the resource at the backend, if any;
                converted to a string (backends may send other JSON types).

        Returns:
            The response after the `response` phase.
        """
        self._report("accepted", None, None if remote_id is None else str(remote_id))
        return self._run.respond(response, attempt=self.number)


class PipelineRun:
    """Run of the pipeline of one gateway request.

    Attributes:
        host: Plugin host.
        pipeline: Resolved pipeline.
        inbound: Inbound request; after `request()`, the committed one.
        principal: Authenticated caller.
        request_id: Identifier of the request.
        run_id: Random identifier of the run, e.g., for records of failed
            runs without a request identifier.
        task_id: Identifier of the gateway's resource, once created; set by
            the app, passed to hooks of later phases.
        original: Payload of the inbound request before any plugin ran.
        executions: Records of the hook calls, in order.
        attempt_records: Records of the forwarding attempts, in order.
        candidates_in: Identifiers of the candidates before the `route`
            phase.
        candidates_out: Identifiers of the candidates after it and the
            client's backend selection.
        snapshots: Times of the registry reads of the candidates, by
            registry entry.
        selection: Client's choices, from the reserved tags.
        backends_discarded: Backends of the client's selection that were no
            candidates.
        final_reason: Why forwarding ended without an accepting backend
            (`forward_timeout`, `no_backend`, or `outcome_unknown` if a
            backend may have accepted), if it did.
    """

    def __init__(
        self,
        host: PluginHost,
        *,
        pipeline: ResolvedPipeline,
        inbound: InboundRequest,
        principal: Principal,
        request_id: str | None,
        selection: ClientSelection | None = None,
    ) -> None:
        """Create the run.

        Args:
            host: Plugin host.
            pipeline: Resolved pipeline.
            inbound: Inbound request.
            principal: Authenticated caller.
            request_id: Identifier of the request.
            selection: Client's choices, from the reserved tags.
        """
        self.host = host
        self.pipeline = pipeline
        self.inbound = inbound
        self.principal = principal
        self.request_id = request_id
        self.run_id = uuid.uuid4().hex
        self.task_id: str | None = None
        self.original = copy.deepcopy(inbound.payload)
        self._inbound_via = inbound.headers.get("Via")
        self.executions: list[Execution] = []
        self.attempt_records: list[AttemptRecord] = []
        self.candidates_in: list[str] = []
        self.candidates_out: list[str] = []
        self.snapshots: dict[str, str | None] = {}
        self.selection = selection or ClientSelection()
        # recorded in the provenance of the request (#488)
        self.backends_discarded: list[str] = []
        self.final_reason: str | None = None
        self._states: dict[str, dict[str, Any]] = {}
        self._changes_size = 0
        self._deadline = time.monotonic() + host.conf.defaults.pipeline_timeout

    def provenance(self) -> dict[str, Any]:
        """Describe the run so far as JSON data, for storage with the task.

        Holds no IP addresses, tokens, header values or claims; plugins are
        referenced by build key (cf. :py:class:`foca.plugins.provenance.ProvenanceStore`
        for storing builds and admin-defined pipelines once).

        Returns:
            Record with `engine` (FOCA, plugin API and app versions),
            `request_id`, the prospective `pipeline` (`source`, `preset`,
            `requested` as a reference to the client's tag, `resolved`,
            `resolved_hash`), the `plugins` that ran (names by build key),
            the `executions` of hooks, the `route` (candidates in and out),
            the client's backend `selection`, the forwarding `attempts` and
            the `final_reason`, if forwarding failed. URLs have no user info
            or query. Never raises for data that is no I-JSON: `resolved`
            and `resolved_hash` are then `None`, with `error: not_json`.
            Records larger than
            :py:data:`foca.plugins.provenance.MAX_RECORD_SIZE` are cut
            (cf. :py:func:`foca.plugins.provenance.limit_record`).
        """
        builds = {
            plugin_id: build_key(loaded)
            for plugin_id, loaded in self.host.plugins.items()
        }
        ran = {execution.plugin for execution in self.executions if execution.plugin}
        resolved: list[dict[str, Any]] | None = self.pipeline.describe()
        try:
            resolved_hash: str | None = content_hash(resolved)
        except (TypeError, ValueError, ArithmeticError, RecursionError):
            # e.g., parameters that are no I-JSON, which might not be stored
            resolved_hash = resolved = None
        prefix = self.host.conf.tags.prefix
        selection = self.selection.backends
        return limit_record(
            {
                "engine": {
                    "foca": _foca_version(),
                    "plugin_api": API_VERSION,
                    "app": dict(self.host.app_info),
                },
                "request_id": self.request_id,
                "pipeline": {
                    "source": self.pipeline.source,
                    "preset": self.pipeline.preset,
                    "requested": (
                        f"tags[{prefix}pipeline]"
                        if self.selection.pipeline is not None
                        else None
                    ),
                    "resolved": resolved,
                    "resolved_hash": resolved_hash,
                    **({} if resolved_hash else {"error": "not_json"}),
                },
                "plugins": {builds[plugin]: plugin for plugin in sorted(ran)},
                "executions": [
                    execution.describe(builds) for execution in self.executions
                ],
                "route": {
                    "candidates_in": [clean_url(item) for item in self.candidates_in],
                    "candidates_out": [clean_url(item) for item in self.candidates_out],
                    "snapshots": self.snapshots,
                },
                "selection": (
                    {
                        "type": selection.type,
                        "backends": [clean_url(item) for item in selection.backends],
                        "discarded": [
                            clean_url(item) for item in self.backends_discarded
                        ],
                    }
                    if selection is not None
                    else None
                ),
                "attempts": [
                    {
                        "number": record.number,
                        "backend": clean_url(record.backend),
                        "outcome": record.outcome,
                        "reason": record.reason,
                        "remote_id": record.remote_id,
                        "started_at": record.started_at,
                        "duration_ms": record.duration_ms,
                    }
                    for record in self.attempt_records
                ],
                "final_reason": self.final_reason,
            }
        )

    def _set_via(self, outbound: OutboundRequest) -> None:
        """Set the `Via` header of an outbound request (RFC 9110, 7.6.3).

        The header is the inbound request's, passed through verbatim, with
        this gateway's entry appended (together at most
        :py:data:`MAX_VIA_LENGTH` characters), whatever plugins or the app's
        builder set, so that loops through several gateways are detected.

        Args:
            outbound: Outbound request.
        """
        entries = [self._inbound_via] if self._inbound_via else []
        if self.host.gateway_id is not None:
            entries.append(f"1.1 {self.host.gateway_id}")
        if entries:
            outbound.headers["Via"] = ", ".join(entries)
        else:
            outbound.headers.pop("Via", None)

    def implements(self, phase: str) -> bool:
        """Check whether a step of the pipeline implements a phase.

        Args:
            phase: Phase.

        Returns:
            Whether a plugin of an alternative has the phase's hook.
        """
        return any(
            phase in self.host.plugins[alt.plugin].cls.phases()
            for step in self.pipeline.steps
            for alt in step.alternatives
        )

    def request(self) -> InboundRequest:
        """Run the `request` phase.

        Returns:
            The inbound request after the phase (also in `inbound`).

        Output that adds, changes or removes tags under the reserved prefix
        (cf. :py:func:`foca.plugins.tags.check_reserved_tags`) is invalid.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives of a required step failed.
            Exception: The app's `sanitize` failed (its exception).
        """
        prefix = self.host.conf.tags.prefix

        def check(candidate: InboundRequest, given: InboundRequest) -> InboundRequest:
            # plugins must not forge (or drop) gateway tags
            check_reserved_tags(given.payload, candidate.payload, prefix)
            candidate.payload = self.host.check_payload(candidate.payload)
            return candidate

        self.inbound = self._run_phase(
            "request", self.inbound, check=check, deadline=self._deadline
        )
        return self.inbound

    def route(
        self,
        candidates: Sequence[Backend],
        snapshots: Mapping[str, str | None] | None = None,
    ) -> list[Backend]:
        """Run the `route` phase.

        Backends at or below the gateway's public URL are removed first;
        the client's backend selection, if any, applies to the result (cf.
        :py:func:`foca.plugins.selection.apply_backend_selection`).

        Args:
            candidates: Candidate backends, in the server's order.
            snapshots: Times of the registry reads that the candidates come
                from, by registry entry, for provenance (cf. `snapshots` of
                :py:meth:`foca.plugins.backends.BackendList.expand`).

        Returns:
            Candidates after the phase, in order.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives of a required step failed.
        """
        own = [
            backend.id for backend in candidates if self.host.is_own_url(backend.url)
        ]
        if own:
            logger.warning("Backends at the gateway's own URL removed: %s", own)
        self.candidates_in = [backend.id for backend in candidates]
        self.snapshots = dict(snapshots or {})
        routed: list[Backend] = self._run_phase(
            "route",
            [backend for backend in candidates if backend.id not in own],
            check=_check_route,
            deadline=self._deadline,
        )
        if self.selection.backends is not None:
            routed, self.backends_discarded = apply_backend_selection(
                routed, self.selection.backends
            )
        self.candidates_out = [backend.id for backend in routed]
        return routed

    def attempts(
        self,
        candidates: Sequence[Backend],
        build: Callable[[Backend, InboundRequest], OutboundRequest] = default_outbound,
    ) -> Iterator[Attempt]:
        """Run the `forward` phase for each candidate, until one accepts.

        Args:
            candidates: Candidates, in order (e.g., from `route()`).
            build: Function building the outbound request for a backend
                (cf. :py:func:`default_outbound`).

        Yields:
            Attempts, after the `forward` phase; report the outcome of each
            (`failed()`, `unknown()` or `succeeded()`). Attempts whose
            `forward` step failed, or whose outbound request the app's
            `validate_outbound` rejects, are skipped (recorded as not
            accepted, reason `forward_plugin_failed` or `invalid_outbound`).
            After a success, or an attempt whose outcome is unknown, the
            loop ends without raising; in the latter case, `final_reason`
            is `outcome_unknown`, and the app must not retry elsewhere.

        Raises:
            ForwardTimeout: `plugins.defaults.forward_timeout` passed before
                a backend accepted.
            NoBackendAvailable: No backend accepted the request.
            PipelineRejected: A plugin rejected the request.
            RuntimeError: The outcome of an attempt was not reported.
        """
        budget_end = time.monotonic() + self.host.conf.defaults.forward_timeout
        for backend in candidates:
            if self.host.is_own_url(backend.url):
                continue
            if time.monotonic() >= budget_end:
                timeout = self.host.conf.defaults.forward_timeout
                self.final_reason = "forward_timeout"
                raise ForwardTimeout(
                    f"No backend accepted within {timeout} s", run=self
                )
            number = len(self.attempt_records) + 1
            started, started_at = time.monotonic(), _utc_now()
            reason = None
            try:
                outbound = self._run_phase(
                    "forward",
                    build(backend, self.inbound),
                    check=_check_forward,
                    deadline=budget_end,
                    attempt=number,
                )
                self._set_via(outbound)
                if self.host._validate_outbound is not None:
                    try:
                        self.host._validate_outbound(outbound)
                    except Exception as exc:
                        logger.warning(
                            "Outbound request to %s invalid: %s",
                            backend.id,
                            describe_error(exc),
                        )
                        reason = "invalid_outbound"
            except _StepFailedError:
                reason = "forward_plugin_failed"
            if reason is not None:
                self.attempt_records.append(
                    AttemptRecord(
                        number=number,
                        backend=backend.id,
                        url=backend.url,
                        outcome="not_accepted",
                        reason=reason,
                        remote_id=None,
                        started_at=started_at,
                        duration_ms=round((time.monotonic() - started) * 1000, 3),
                    )
                )
                continue
            attempt = Attempt(
                self, number=number, outbound=outbound, budget_end=budget_end
            )
            yield attempt
            if attempt.state is None:
                raise RuntimeError("Report the outcome of each attempt")
            if attempt.state == "unknown":
                self.final_reason = "outcome_unknown"
            if attempt.state != "not_accepted":
                return
        if time.monotonic() >= budget_end:
            timeout = self.host.conf.defaults.forward_timeout
            self.final_reason = "forward_timeout"
            raise ForwardTimeout(f"No backend accepted within {timeout} s", run=self)
        self.final_reason = "no_backend"
        raise NoBackendAvailable("No backend accepted the request", run=self)

    def respond(self, response: BackendResponse, attempt: int) -> BackendResponse:
        """Run the `response` phase, in reverse step order.

        Steps whose alternatives all fail are skipped, as the backend has
        accepted the request already.

        Args:
            response: Response of the backend.
            attempt: Number of the attempt.

        Returns:
            The response after the phase.
        """
        return self._run_phase(
            "response",
            response,
            check=_check_response,
            deadline=float("inf"),
            attempt=attempt,
            reverse=True,
        )

    def _run_phase(
        self,
        phase: str,
        value: Any,
        check: Callable[[Any, Any], Any],
        deadline: float,
        attempt: int | None = None,
        reverse: bool = False,
    ) -> Any:
        """Run the steps of a phase on a value.

        Args:
            phase: Phase.
            value: Input of the first step.
            check: Function checking the output of a call against its input,
                returning the output to commit, or raising an exception if
                it is invalid.
            deadline: `time.monotonic()` value by which the phase must end.
            attempt: Number of the forwarding attempt, if any.
            reverse: Whether to run the steps in reverse order.

        Returns:
            Output of the last step.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives of a required step failed
                (`request` and `route` phases).
            _StepFailedError: All alternatives of a required step failed
                (`forward` phase).
        """
        steps = reversed(self.pipeline.steps) if reverse else self.pipeline.steps
        for step in steps:
            value = self._run_step(phase, step, value, check, deadline, attempt)
        return value

    def _run_step(
        self,
        phase: str,
        step: ResolvedStep,
        value: Any,
        check: Callable[[Any, Any], Any],
        deadline: float,
        attempt: int | None,
    ) -> Any:
        """Run the alternatives of a step until one succeeds.

        Args:
            phase: Phase.
            step: Step.
            value: Input.
            check: Output check (cf. `_run_phase()`).
            deadline: `time.monotonic()` value by which the phase must end.
            attempt: Number of the forwarding attempt, if any.

        Returns:
            Output of the successful alternative, or the input if no
            alternative implements the phase or the step was skipped.

        Raises:
            PipelineRejected: A plugin rejected the request.
            PipelineFailed: All alternatives failed (`request`, `route`).
            _StepFailedError: All alternatives failed (`forward`).
        """
        tried = False
        for index, alternative in enumerate(step.alternatives):
            loaded = self.host.plugins[alternative.plugin]
            hook = getattr(loaded.instance, f"on_{phase}", None)
            if hook is None:
                continue
            tried = True
            execution, output = self._call(
                phase, step, index, alternative, hook, value, check, deadline, attempt
            )
            self.executions.append(execution)
            if execution.outcome == "ok":
                return output
        if not tried:
            return value
        if step.on_failure == "skip" or phase == "response":
            self.executions.append(
                Execution(
                    step=step.id,
                    phase=phase,
                    alternative=-1,
                    plugin=None,
                    outcome="skipped",
                    started_at=_utc_now(),
                    duration_ms=0.0,
                    attempt=attempt,
                )
            )
            return value
        if phase in ("request", "route"):
            raise PipelineFailed(step.id, run=self)
        raise _StepFailedError(step.id)

    def _call(
        self,
        phase: str,
        step: ResolvedStep,
        index: int,
        alternative: ResolvedAlternative,
        hook: Callable[[Any, Context], Any],
        value: Any,
        check: Callable[[Any, Any], Any],
        deadline: float,
        attempt: int | None,
    ) -> tuple[Execution, Any]:
        """Call the hook of one alternative on a copy of the input.

        Args:
            phase: Phase.
            step: Step.
            index: Index of the alternative.
            alternative: Alternative.
            hook: Hook of the plugin.
            value: Input.
            check: Output check (cf. `_run_phase()`).
            deadline: `time.monotonic()` value by which the phase must end.
            attempt: Number of the forwarding attempt, if any.

        Returns:
            Record of the call, and the checked output (`None` unless the
            call succeeded).

        Raises:
            PipelineRejected: The plugin rejected the request (except in
                the `response` phase, where rejections count as errors).
        """
        execution = Execution(
            step=step.id,
            phase=phase,
            alternative=index,
            plugin=alternative.plugin,
            outcome="ok",
            started_at=_utc_now(),
            duration_ms=0.0,
            attempt=attempt,
        )
        started = time.monotonic()
        output = None
        call_deadline = min(time.monotonic() + step.timeout, deadline)
        try:
            # copies per call, so that a failure fails this alternative only
            candidate = copy.deepcopy(value)
            state = copy.deepcopy(self._states.get(alternative.plugin, {}))
            ctx = Context(
                api=self.host.api,
                plugin_id=alternative.plugin,
                principal=replace(
                    self.principal,
                    claims=copy.deepcopy(FrozenMapping(self.principal.claims)),
                ),
                params=alternative.params.model_copy(deep=True),
                original=self.original,
                deadline=call_deadline,
                state=state,
                request_id=self.request_id,
                task_id=self.task_id,
                http=self.host._http_client(
                    deadline=call_deadline,
                    timeout=step.timeout,
                    **self.host.conf.defaults.http.model_dump(),
                ),
                store=self.host.store(alternative.plugin, deadline=call_deadline),
            )
        except Exception as exc:
            execution.outcome = "error"
            execution.error = sanitize_error(exc)
            execution.duration_ms = round((time.monotonic() - started) * 1000, 3)
            logger.warning(
                "Plugin %s not called in step %s (%s): %s",
                alternative.plugin,
                step.id,
                phase,
                describe_error(exc),
            )
            return execution, None
        try:
            result = self.host.call(
                lambda: hook(candidate, ctx), call_deadline - time.monotonic()
            )
            try:
                output = check(result if phase == "route" else candidate, value)
            except _AppError:
                raise
            except Exception as exc:
                logger.warning(
                    "Output of plugin %s in step %s (%s) invalid: %s",
                    alternative.plugin,
                    step.id,
                    phase,
                    describe_error(exc),
                )
                raise _InvalidOutputError(sanitize_error(exc)) from exc
        except _AppError as exc:
            execution.outcome = "error"
            execution.error = sanitize_error(exc.error)
            self._finish(execution, started, ctx)
            self.executions.append(execution)
            raise exc.error from None
        except Rejected as exc:
            execution.outcome = "rejected"
            execution.error = sanitize_error(exc)
            if phase != "response":
                self._finish(execution, started, ctx)
                self.executions.append(execution)
                raise PipelineRejected(exc, step=step.id, run=self) from exc
        except NotApplicable:
            execution.outcome = "not_applicable"
        except _DeadlineExceededError as exc:
            execution.outcome = "timeout"
            execution.error = str(exc)
        except _InvalidOutputError as exc:
            execution.outcome = "invalid_output"
            execution.error = str(exc)
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:  # e.g., `SystemExit` of a hook
            execution.outcome = "error"
            execution.error = sanitize_error(exc)
            logger.warning(
                "Plugin %s failed in step %s (%s): %s",
                alternative.plugin,
                step.id,
                phase,
                describe_error(exc),
            )
        self._finish(execution, started, ctx)
        if execution.outcome != "ok":
            return execution, None
        self._states[alternative.plugin] = state
        execution.changes = self._describe_changes(phase, value, output)
        return execution, output

    def _describe_changes(self, phase: str, before: Any, after: Any) -> dict[str, Any]:
        """Describe the changes of a call within the run's size budget.

        Never raises: records must not fail requests, which backends may
        have accepted already.

        Args:
            phase: Phase.
            before: Input of the call.
            after: Committed output of the call.

        Returns:
            Changes (cf. :py:func:`foca.plugins.provenance.describe_changes`);
            without paths, and `truncated: true`, once the paths of the run
            exceed :py:data:`foca.plugins.provenance.MAX_RUN_CHANGES_SIZE`
            bytes; `error: internal` if they cannot be described.
        """
        try:
            changes = describe_changes(
                phase, before, after, self.host.conf.provenance.secret_headers
            )
            size = len(json.dumps(changes.get("paths", [])).encode())
        except Exception:
            logger.exception("Changes of a %s hook not described", phase)
            return {"error": "internal"}
        if self._changes_size + size > MAX_RUN_CHANGES_SIZE:
            changes["paths"] = []
            changes["truncated"] = True
        else:
            self._changes_size += size
        return changes

    @staticmethod
    def _finish(execution: Execution, started: float, ctx: Context) -> None:
        """Complete the record of a call and close its HTTP client.

        Args:
            execution: Record.
            started: `time.monotonic()` value at the start of the call.
            ctx: Context of the call.
        """
        if ctx.http is not None:  # always set by the engine
            ctx.http.close()
        execution.duration_ms = round((time.monotonic() - started) * 1000, 3)
        execution.annotations = dict(ctx.annotations)


__all__ = [
    "MAX_VIA_LENGTH",
    "Attempt",
    "AttemptRecord",
    "Execution",
    "ForwardTimeout",
    "GatewayError",
    "InvalidSelection",
    "InvalidVia",
    "LoopDetected",
    "NoBackendAvailable",
    "NotAccepted",
    "OutcomeUnknown",
    "PipelineFailed",
    "PipelineRejected",
    "PipelineRun",
    "PluginHost",
    "ResolvedAlternative",
    "ResolvedPipeline",
    "ResolvedStep",
    "default_outbound",
    "describe_error",
    "sanitize_error",
]
