"""Test kit for gateway plugins: harness, builders, fake HTTP and contract.

Framework-free: plugins are tested through the real engine
(:py:class:`foca.plugins.PluginHost`), without app, database or network.

Examples:
    >>> from foca.plugins.routing import OrderedRouter
    >>> harness = PluginHarness(OrderedRouter, params={"labels": {"eu": "1"}})
    >>> backends = [make_backend("a"), make_backend("b", labels={"eu": "1"})]
    >>> [backend.id for backend in harness.run_route(backends)]
    ['b']
    >>> harness.close()

For apps, the pytest fixture `fake_plugin_entry_points` of
:py:mod:`foca.plugins.pytest_plugin` registers fake plugins as installed
entry points.
"""

import ast
import copy
import inspect
import ipaddress
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from json import dumps
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

from foca.plugins.base import Plugin, check_plugin_class
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import PipelineRun, PluginHost
from foca.plugins.errors import NoBackendAvailable, PipelineRejected
from foca.plugins.loader import LoadedPlugin
from foca.plugins.model import (
    Backend,
    BackendResponse,
    Headers,
    InboundRequest,
    OutboundRequest,
    Principal,
)
from foca.plugins.records import Execution
from foca.utils.http import (
    _METHOD,
    AddressNotAllowed,
    DeadlineExceeded,
    GuardedClient,
    HttpResponse,
    InvalidRequestURL,
    RequestNotSent,
    ResponseTooLarge,
    _split,
    is_public_address,
)

FRAMEWORK_MODULES = frozenset(
    {"connexion", "fastapi", "flask", "quart", "starlette", "werkzeug"}
)
"""Top-level modules of web frameworks that plugins must not import."""

FAILED_OUTCOMES = frozenset({"error", "invalid_output", "timeout"})
"""Outcomes of hook calls that break the plugin contract."""

THREAD_TIMEOUT = 60.0
"""Seconds that the concurrency check waits for its threads."""

PLUGIN_ID = "test.plugin"
"""Default identifier of plugins in the harness."""


# Builders


def make_principal(
    subject: str | None = "user",
    *,
    groups: Iterable[str] = (),
    claims: Mapping[str, Any] | None = None,
    token: str | None = None,
    issuer: str | None = "https://login.example.org",
) -> Principal:
    """Build a caller.

    Args:
        subject: Subject.
        groups: Groups (roles).
        claims: Claims; default to `sub` and `iss`.
        token: Access token.
        issuer: Issuer.

    Returns:
        Principal.
    """
    if claims is None:
        claims = {"sub": subject, "iss": issuer}
    return Principal(
        subject=subject,
        issuer=issuer,
        groups=tuple(groups),
        claims=claims,
        token=token,
    )


def make_inbound(
    payload: Any = None,
    *,
    method: str = "POST",
    path: str = "/ga4gh/tes/v1/tasks",
    route: str = "/tasks",
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, list[str]] | None = None,
) -> InboundRequest:
    """Build an inbound request.

    Args:
        payload: Payload; defaults to a minimal TES task.
        method: HTTP method.
        path: Path of the request.
        route: Route of the API operation.
        headers: Headers.
        query: Query parameters.

    Returns:
        Inbound request.
    """
    if payload is None:
        payload = {"executors": [{"image": "alpine", "command": ["true"]}]}
    return InboundRequest(
        method=method,
        path=path,
        route=route,
        headers=Headers(headers or {}),
        query=dict(query or {}),
        payload=payload,
    )


def make_backend(
    id: str = "a",
    url: str | None = None,
    *,
    labels: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Backend:
    """Build a backend.

    Args:
        id: Identifier.
        url: URL; defaults to `https://<id>.example.org/ga4gh/tes/v1`.
        labels: Labels.
        kwargs: Other fields of :py:class:`foca.plugins.Backend`.

    Returns:
        Backend.
    """
    return Backend(
        id=id,
        url=url or f"https://{id}.example.org/ga4gh/tes/v1",
        labels=dict(labels or {}),
        **kwargs,
    )


def make_outbound(
    payload: Any = None,
    *,
    backend: Backend | None = None,
    method: str = "POST",
    path: str = "/tasks",
    headers: Mapping[str, str] | None = None,
) -> OutboundRequest:
    """Build an outbound request.

    Args:
        payload: Payload.
        backend: Backend; defaults to `make_backend()`.
        method: HTTP method.
        path: Path below the backend's URL.
        headers: Headers.

    Returns:
        Outbound request.
    """
    return OutboundRequest(
        backend=backend or make_backend(),
        method=method,
        path=path,
        headers=Headers(headers or {}),
        payload=payload,
    )


def make_response(
    status: int = 200,
    payload: Any = None,
    *,
    client_payload: Any = None,
    headers: Mapping[str, str] | None = None,
    client_headers: Mapping[str, str] | None = None,
) -> BackendResponse:
    """Build a response of a backend.

    Args:
        status: HTTP status.
        payload: Payload of the backend; defaults to `{"id": "remote-1"}`.
        client_payload: Payload for the client; defaults to the payload.
        headers: Headers of the backend.
        client_headers: Headers for the client.

    Returns:
        Response.
    """
    if payload is None:
        payload = {"id": "remote-1"}
    return BackendResponse(
        status=status,
        headers=Headers(headers or {}),
        payload=payload,
        client_payload=payload if client_payload is None else client_payload,
        client_headers=Headers(client_headers or {}),
    )


# Fake HTTP


@dataclass(frozen=True)
class HttpCall:
    """Request sent with :py:class:`FakeHttp`.

    Attributes:
        method: HTTP method.
        url: URL.
        params: Query parameters.
        headers: Headers.
        json: Body to send as JSON.
        data: Body to send as is.
        remaining: Seconds that were left for the request.
    """

    method: str
    url: str
    params: Mapping[str, Any] | None
    headers: Mapping[str, str] | None
    json: Any
    data: bytes | str | None
    remaining: float


@dataclass
class _FakeHttpState:
    """State shared by the clients of one :py:class:`FakeHttp`.

    Attributes:
        responses: Canned responses or errors, by method and URL.
        calls: Requests, in order.
        lock: Lock of the state.
    """

    responses: dict[tuple[str, str], list[HttpResponse | Exception]] = field(
        default_factory=dict
    )
    calls: list[HttpCall] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class FakeHttp(GuardedClient):
    """Stand-in for `ctx.http`: records requests, returns canned responses.

    It sends nothing. Like :py:class:`foca.utils.http.GuardedClient`, it
    refuses other schemes than `https` (unless `allow_http`), invalid
    methods, hosts that are IP addresses that are not public (unless
    `allow_private_addresses`; host names are not resolved), requests
    after the deadline, and canned bodies beyond `max_response_size`.
    Query parameters are encoded as by `requests`. Requests without a
    canned response fail with :py:class:`foca.utils.http.RequestNotSent`
    (`no_canned_response`).

    Pass `client` as `http_client` of a host (as
    :py:class:`PluginHarness` does): the clients of all calls then share
    the responses and calls.

    Examples:
        >>> http = FakeHttp()
        >>> http.add("GET", "https://geo.example.org/", json={"lat": 1})
        >>> http.get("https://geo.example.org/").json()
        {'lat': 1}
        >>> [call.url for call in http.calls]
        ['https://geo.example.org/']
    """

    def __init__(self, *, _state: _FakeHttpState | None = None, **kwargs: Any) -> None:
        """Create the client.

        Args:
            _state: State shared with other clients (cf. `client()`).
            kwargs: Arguments of :py:class:`foca.utils.http.GuardedClient`.
        """
        super().__init__(**kwargs)
        self._state = _state or _FakeHttpState()

    @property
    def calls(self) -> list[HttpCall]:
        """Get the requests sent with this client and the clients it made.

        Returns:
            Requests, in order.
        """
        with self._state.lock:
            return list(self._state.calls)

    def client(self, **kwargs: Any) -> "FakeHttp":
        """Make a client that shares responses and calls with this one.

        Args:
            kwargs: Arguments of :py:class:`foca.utils.http.GuardedClient`
                (e.g., the call's `deadline`).

        Returns:
            Client.
        """
        return FakeHttp(_state=self._state, **kwargs)

    def add(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        json: Any = None,
        content: bytes = b"",
        headers: Mapping[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Add a canned response or error; each is returned once, in order.

        The last one of a method and URL is returned again for further
        requests.

        Args:
            method: HTTP method.
            url: URL, with the query string if the request has one.
            status: HTTP status.
            json: Body, as JSON.
            content: Body, if not `json`.
            headers: Headers.
            error: Error to raise instead, e.g.,
                :py:class:`foca.utils.http.RequestMaybeSent`.
        """
        result: HttpResponse | Exception
        if error is not None:
            result = error
        else:
            body = content if json is None else dumps(json).encode()
            result = HttpResponse(
                status=status,
                headers={key.lower(): value for key, value in (headers or {}).items()},
                content=body,
            )
        with self._state.lock:
            self._state.responses.setdefault((method.upper(), url), []).append(result)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: bytes | str | None = None,
        auth: Any = None,
    ) -> HttpResponse:
        """Record a request and return its canned response.

        Args:
            method: HTTP method.
            url: URL.
            params: Query parameters, appended to the URL for the lookup.
            headers: Headers.
            json: Body to send as JSON.
            data: Body to send as is.
            auth: Authentication (ignored).

        Returns:
            The canned response.

        Raises:
            InvalidRequestURL: The URL or method is invalid, or the scheme
                is not allowed.
            AddressNotAllowed: The host is an IP address that is not
                public.
            DeadlineExceeded: The deadline passed.
            RequestNotSent: There is no canned response.
            ResponseTooLarge: The canned body exceeds `max_response_size`.
            Exception: The canned error.
        """
        scheme, host, _ = _split(url)
        if scheme not in (("https", "http") if self.allow_http else ("https",)):
            raise InvalidRequestURL(f"Scheme not allowed: {scheme or '(none)'}")
        if not isinstance(method, str) or not _METHOD.fullmatch(method):
            raise InvalidRequestURL("Invalid HTTP method")
        if not self.allow_private_addresses:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None  # host names are not resolved
            if address is not None and not is_public_address(address):
                raise AddressNotAllowed(
                    f"Host of {url} has an address that is not public: {host}"
                )
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceeded(f"No time left for {url}")
        if params:
            query = urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        with self._state.lock:
            self._state.calls.append(
                HttpCall(method.upper(), url, params, headers, json, data, remaining)
            )
            queue = self._state.responses.get((method.upper(), url))
            result = (queue.pop(0) if len(queue) > 1 else queue[0]) if queue else None
        if result is None:
            raise RequestNotSent(
                f"No canned response for {method} {url}", reason="no_canned_response"
            )
        if isinstance(result, Exception):
            raise result
        if len(result.content) > self.max_response_size:
            raise ResponseTooLarge(
                f"Response of {url} exceeds {self.max_response_size} bytes"
            )
        return result


# Harness


class PluginHarness:
    """Run the hooks of one plugin through the real engine.

    The pipeline has one step with the plugin (`test`), with its
    parameters; `ctx.http` is a :py:class:`FakeHttp`. Each `run_*` method
    starts a new run (also in `run`, per thread), runs the earlier phases
    first (e.g., `request` before `route`), and returns the output of its
    phase; the hook calls are in `executions`. Harnesses may be used from
    several threads at once.

    Attributes:
        plugin: Loaded plugin (one instance for all runs).
        host: Plugin host.
        http: Fake HTTP client of the calls.
    """

    def __init__(
        self,
        plugin: type[Plugin[Any, Any]],
        *,
        config: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        plugin_id: str = PLUGIN_ID,
        api: str = "tes",
        on_failure: str = "fail",
        timeout: float | None = None,
        http: FakeHttp | None = None,
        defaults: Mapping[str, Any] | None = None,
        **host_kwargs: Any,
    ) -> None:
        """Create the harness.

        Args:
            plugin: Plugin class.
            config: Admin settings of the plugin.
            params: Parameters of the plugin in the step.
            plugin_id: Identifier of the plugin.
            api: API of the app.
            on_failure: `on_failure` of the step (`fail` or `skip`).
            timeout: Timeout of the step, in seconds; defaults to
                `plugins.defaults.step_timeout`.
            http: Fake HTTP client; defaults to one without responses.
            defaults: `plugins.defaults` (e.g., `workers`).
            host_kwargs: Other arguments of
                :py:class:`foca.plugins.PluginHost` (e.g., `validate`).

        Raises:
            ValueError: The class is not a valid plugin for the API, or the
                config or parameters are invalid.
        """
        step: dict[str, Any] = {
            "id": "test",
            "plugin": plugin_id,
            "params": dict(params or {}),
            "on_failure": on_failure,
        }
        if timeout is not None:
            step["timeout"] = timeout
        conf = PluginsConfig.model_validate(
            {
                "available": {plugin_id: {"config": dict(config or {})}},
                "pipeline": [step],
                "client_pipelines": {"append": []},
                "defaults": dict(defaults or {}),
            }
        )
        cls = check_plugin_class(plugin, plugin_id=plugin_id, api=api)
        self.plugin = LoadedPlugin(
            id=plugin_id,
            cls=cls,
            instance=cls(plugin_id, cls.Config.model_validate(dict(config or {}))),
            distribution=None,
            version=None,
            entry_point=_reference(cls),
            record_digest=None,
            available=conf.available[plugin_id],
        )
        self.http = http or FakeHttp()
        # closes the plugin if it fails
        self.host = PluginHost(
            conf,
            api=api,
            plugins={plugin_id: self.plugin},
            http_client=self.http.client,
            **host_kwargs,
        )
        self._local = threading.local()

    @property
    def run(self) -> PipelineRun | None:
        """Get the last run of this thread.

        Returns:
            Run, if any.
        """
        return getattr(self._local, "run", None)

    @property
    def executions(self) -> list[Execution]:
        """Get the hook calls of the last run.

        Returns:
            Records of the calls, in order.
        """
        run = self.run
        return list(run.executions) if run is not None else []

    def start(
        self,
        inbound: InboundRequest | None = None,
        principal: Principal | None = None,
        **kwargs: Any,
    ) -> PipelineRun:
        """Start a run.

        Args:
            inbound: Inbound request; defaults to `make_inbound()`.
            principal: Caller; defaults to `make_principal()`.
            kwargs: Other arguments of
                :py:meth:`foca.plugins.PluginHost.start`.

        Returns:
            Run.
        """
        kwargs.setdefault("request_id", "test-request")
        run = self.host.start(
            inbound or make_inbound(), principal or make_principal(), **kwargs
        )
        self._local.run = run
        return run

    def run_request(
        self,
        inbound: InboundRequest | None = None,
        principal: Principal | None = None,
    ) -> InboundRequest:
        """Run the `request` phase.

        Args:
            inbound: Inbound request; defaults to `make_inbound()`.
            principal: Caller; defaults to `make_principal()`.

        Returns:
            The inbound request after the phase.
        """
        return self.start(inbound, principal).request()

    def run_route(
        self,
        candidates: Sequence[Backend] | None = None,
        inbound: InboundRequest | None = None,
        principal: Principal | None = None,
        *,
        request: bool = True,
    ) -> list[Backend]:
        """Run the `request` and `route` phases.

        Args:
            candidates: Candidates; default to backends `a`, `b` and `c`.
            inbound: Inbound request; defaults to `make_inbound()`.
            principal: Caller; defaults to `make_principal()`.
            request: Whether to run the `request` phase first.

        Returns:
            Candidates after the `route` phase.
        """
        run = self._started(inbound, principal, request)
        return run.route(_candidates(candidates))

    def run_forward(
        self,
        backend: Backend | None = None,
        inbound: InboundRequest | None = None,
        principal: Principal | None = None,
        *,
        request: bool = True,
    ) -> OutboundRequest | None:
        """Run the `request` and `forward` phases for one backend.

        The attempt is then reported as failed (`test_done`).

        Args:
            backend: Backend; defaults to `make_backend()`.
            inbound: Inbound request; defaults to `make_inbound()`.
            principal: Caller; defaults to `make_principal()`.
            request: Whether to run the `request` phase first.

        Returns:
            The outbound request after the phase, or `None` if the
            `forward` step failed (cf. `executions`).
        """
        run = self._started(inbound, principal, request)
        try:
            for attempt in run.attempts([backend or make_backend()]):
                attempt.failed("test_done")
                return attempt.outbound
        except NoBackendAvailable:
            pass
        return None

    def run_response(
        self,
        response: BackendResponse | None = None,
        backend: Backend | None = None,
        inbound: InboundRequest | None = None,
        principal: Principal | None = None,
        *,
        request: bool = True,
    ) -> BackendResponse:
        """Run the `request`, `forward` and `response` phases.

        Args:
            response: Response of the backend; defaults to
                `make_response()`.
            backend: Backend; defaults to `make_backend()`.
            inbound: Inbound request; defaults to `make_inbound()`.
            principal: Caller; defaults to `make_principal()`.
            request: Whether to run the `request` phase first.

        Returns:
            The response after the phase.

        Raises:
            NoBackendAvailable: The `forward` step failed.
        """
        run = self._started(inbound, principal, request)
        for attempt in run.attempts([backend or make_backend()]):
            return attempt.succeeded(response or make_response(), remote_id="remote-1")
        raise AssertionError("unreachable")  # pragma: no cover

    def _started(
        self,
        inbound: InboundRequest | None,
        principal: Principal | None,
        request: bool,
    ) -> PipelineRun:
        """Start a run, and run its `request` phase if wanted.

        Args:
            inbound: Inbound request, if any.
            principal: Caller, if any.
            request: Whether to run the `request` phase.

        Returns:
            Run.
        """
        run = self.start(inbound, principal)
        if request:
            run.request()
        return run

    def close(self) -> None:
        """Close the host and the plugin."""
        self.host.close()

    def __enter__(self) -> "PluginHarness":
        """Use the harness as a context manager.

        Returns:
            The harness.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the harness.

        Args:
            exc_type: Type of the exception, if any.
            exc: Exception, if any.
            traceback: Traceback, if any.
        """
        self.close()


def _candidates(candidates: Sequence[Backend] | None) -> list[Backend]:
    """Get the candidates of a route phase.

    Args:
        candidates: Candidates, if given.

    Returns:
        The candidates, or backends `a`, `b` and `c`.
    """
    if candidates is None:
        return [make_backend(name) for name in "abc"]
    return list(candidates)


def _reference(target: Any) -> str:
    """Get the object reference of a class.

    Args:
        target: Class or other object.

    Returns:
        `module:qualname`.
    """
    module = getattr(target, "__module__", None) or "unknown"
    name = getattr(target, "__qualname__", None) or type(target).__name__
    return f"{module}:{name}"


# Contract


class ContractViolation(AssertionError):
    """A plugin breaks the plugin contract.

    Attributes:
        problems: Descriptions of the problems.
    """

    def __init__(self, plugin: str, problems: Sequence[str]) -> None:
        """Create the error.

        Args:
            plugin: Reference of the plugin.
            problems: Descriptions of the problems.
        """
        self.problems = list(problems)
        lines = "\n".join(f"- {problem}" for problem in self.problems)
        super().__init__(f"Plugin {plugin} breaks the plugin contract:\n{lines}")


def framework_imports(plugin: type[Any]) -> list[str] | None:
    """Find imports of web frameworks in the module of a plugin.

    A lint, not a guarantee: only the module that defines the class is
    scanned (not the modules it imports), for `import` statements and calls
    of `__import__()` and `import_module()` with literal names.

    Args:
        plugin: Plugin class.

    Returns:
        Imported framework modules (cf. `FRAMEWORK_MODULES`), sorted;
        `None` if the module source is not available.
    """
    try:
        source = inspect.getsource(inspect.getmodule(plugin))  # type: ignore[arg-type]
    except (OSError, TypeError):
        return None
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif _is_dynamic_import(node):
            names = [node.args[0].value]  # type: ignore[attr-defined]
        else:
            continue
        found.update(name for name in names if name.split(".")[0] in FRAMEWORK_MODULES)
    return sorted(found)


def _is_dynamic_import(node: ast.AST) -> bool:
    """Check whether a node imports a module by literal name at runtime.

    Args:
        node: Node of a syntax tree.

    Returns:
        Whether it is a call of `__import__()` or `import_module()` (also
        as an attribute, e.g., `importlib.import_module()`) whose first
        argument is a string literal.
    """
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    first = node.args[0]
    return (
        name in ("__import__", "import_module")
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
    )


def check_plugin_contract(
    plugin: type[Plugin[Any, Any]],
    *,
    config: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    api: str = "tes",
    plugin_id: str = PLUGIN_ID,
    inbound: InboundRequest | None = None,
    candidates: Sequence[Backend] | None = None,
    response: BackendResponse | None = None,
    http: FakeHttp | None = None,
    threads: int = 20,
) -> None:
    """Check that a plugin meets the plugin contract.

    Checks the class (cf. :py:func:`foca.plugins.base.check_plugin_class`),
    a title (docstring or `title`), the JSON Schema of `Config` (and that
    those of `Config` and `Params` serialize), that its module imports no
    web framework, and that its hooks succeed (or reject, or do not apply)
    on the sample inputs through the engine: once, and with one instance
    from several threads at once.
    Phases after `request` run after the plugin's `request` hook, so that
    they get its `ctx.state`, or without it if it rejected the sample or
    failed, so that this does not hide them; rejections are fine, except in
    the `response` phase, where they count as errors. The engine itself
    enforces copy semantics (hooks get copies, `ctx.original` is a copy,
    failed calls change nothing) and route subsets (other backends give
    `invalid_output` outcomes).

    Args:
        plugin: Plugin class.
        config: Admin settings for the checks.
        params: Parameters for the checks.
        api: API of the app.
        plugin_id: Identifier of the plugin.
        inbound: Sample inbound request; defaults to `make_inbound()`.
        candidates: Sample candidates; default to backends `a`, `b`, `c`.
        response: Sample response; defaults to `make_response()`.
        http: Fake HTTP client with the responses the plugin needs.
        threads: Number of threads of the concurrency check.

    Raises:
        ContractViolation: The plugin breaks the contract; lists all
            problems found.
        ValueError: `threads` is less than 1.
    """
    if threads < 1:
        raise ValueError(f"threads must be at least 1, not {threads}")
    reference = _reference(plugin)
    problems: list[str] = []
    try:
        check_plugin_class(plugin, plugin_id=plugin_id, api=api)
    except ValueError as exc:
        raise ContractViolation(reference, [str(exc)]) from exc
    if plugin.title is None and not (plugin.__doc__ or "").strip():
        problems.append("No title: add a docstring or set `title`")
    for name in ("Config", "Params"):
        try:
            dumps(getattr(plugin, name).model_json_schema())
        except Exception as exc:
            problems.append(f"`{name}` exports no JSON Schema: {exc}")
    imports = framework_imports(plugin)
    if imports is None:
        problems.append(
            f"Source of module {plugin.__module__!r} not found: framework"
            " imports not checked"
        )
    elif imports:
        problems.append(
            f"Imports web frameworks {imports}: use `foca.plugins.model` data"
        )
    try:
        harness = PluginHarness(
            plugin,
            config=config,
            params=params,
            api=api,
            plugin_id=plugin_id,
            http=http,
            defaults={"workers": threads},
        )
    except Exception as exc:
        problems.append(f"Not usable with the given config and params: {exc}")
        raise ContractViolation(reference, problems) from exc
    try:
        problems.extend(
            _check_hooks(harness, inbound, candidates, response, label="once")
        )
        problems.extend(_check_threads(harness, inbound, candidates, response, threads))
    finally:
        harness.close()
    if problems:
        raise ContractViolation(reference, problems)


def _check_hooks(
    harness: PluginHarness,
    inbound: InboundRequest | None,
    candidates: Sequence[Backend] | None,
    response: BackendResponse | None,
    label: str,
) -> list[str]:
    """Run each phase of a plugin once, and list failed calls.

    Args:
        harness: Harness of the plugin.
        inbound: Sample inbound request, if any.
        candidates: Sample candidates, if any.
        response: Sample response, if any.
        label: Label of the check, for messages.

    Returns:
        Problems.
    """
    phases = harness.plugin.cls.phases()
    # later phases after the request hook, so that they get its state, but
    # without it if it rejected the sample or failed, so that this does not
    # hide them
    after = {"request": True}
    runs: list[tuple[str, Callable[[], Any]]] = [
        ("request", lambda: harness.run_request(_copy(inbound)))
    ]
    if "route" in phases:
        runs.append(
            (
                "route",
                lambda: harness.run_route(
                    candidates, _copy(inbound), request=after["request"]
                ),
            )
        )
    if "forward" in phases:
        runs.append(
            (
                "forward",
                lambda: harness.run_forward(
                    None, _copy(inbound), request=after["request"]
                ),
            )
        )
    if "response" in phases:
        runs.append(
            (
                "response",
                lambda: harness.run_response(
                    _copy(response), None, _copy(inbound), request=after["request"]
                ),
            )
        )
    problems = []
    for phase, run in runs:
        error = None
        try:
            run()
        except PipelineRejected:
            after["request"] = after["request"] and phase != "request"
            continue
        except Exception as exc:
            error = f"{phase} ({label}): {type(exc).__name__}: {exc}"
        if phase == "request" and (
            error or any(e.outcome in FAILED_OUTCOMES for e in harness.executions)
        ):
            after["request"] = False
        failed = [
            f"{execution.phase} ({label}): outcome {execution.outcome}"
            f" ({execution.error})"
            + (
                ": rejections count as errors in the response phase"
                if execution.outcome == "rejected"
                else ""
            )
            for execution in harness.executions
            if execution.outcome in FAILED_OUTCOMES
            or (execution.phase == "response" and execution.outcome == "rejected")
        ]
        problems.extend(failed or ([error] if error else []))
    return problems


def _check_threads(
    harness: PluginHarness,
    inbound: InboundRequest | None,
    candidates: Sequence[Backend] | None,
    response: BackendResponse | None,
    threads: int,
) -> list[str]:
    """Run the phases of a plugin from several threads at once.

    Args:
        harness: Harness of the plugin.
        inbound: Sample inbound request, if any.
        candidates: Sample candidates, if any.
        response: Sample response, if any.
        threads: Number of threads.

    Returns:
        Problems, once each.
    """
    barrier = threading.Barrier(threads)
    found: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        try:
            barrier.wait(timeout=THREAD_TIMEOUT)
            problems = _check_hooks(harness, inbound, candidates, response, "threads")
        except Exception as exc:  # e.g., threading.BrokenBarrierError
            problems = [f"threads: {type(exc).__name__}: {exc}"]
        with lock:
            found.extend(problems)

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for worker in workers:
        worker.start()
    end = time.monotonic() + THREAD_TIMEOUT
    for worker in workers:
        worker.join(max(0.0, end - time.monotonic()))
    if any(worker.is_alive() for worker in workers):
        found.append(f"threads: calls did not end within {THREAD_TIMEOUT} s")
    return sorted(set(found))


def _copy(value: Any) -> Any:
    """Copy a sample input, so that each run gets its own.

    Args:
        value: Sample, or `None`.

    Returns:
        A deep copy, or `None`.
    """
    return copy.deepcopy(value)


# Entry points


@dataclass
class FakeDistribution:
    """Distribution of fake entry points.

    Attributes:
        name: Distribution name.
        version: Version.
        record: Content of the `RECORD` file, if any.
    """

    name: str = "fake-plugins"
    version: str = "1.2.0"
    record: str | None = "fake/__init__.py,sha256=abc,10\n"

    def read_text(self, filename: str) -> str | None:
        """Read a metadata file.

        Args:
            filename: File name.

        Returns:
            The `RECORD` file, or `None`.
        """
        return self.record if filename == "RECORD" else None


@dataclass
class FakeEntryPoint:
    """Entry point of a fake plugin, of the group `foca.plugins`.

    Attributes:
        name: Name (plugin identifier).
        target: Object that the entry point refers to.
        dist: Distribution.
        loaded: Number of loads.
        group: Entry point group.
    """

    name: str
    target: Any
    dist: FakeDistribution | None = field(default_factory=FakeDistribution)
    loaded: int = 0
    group: str = "foca.plugins"

    @property
    def value(self) -> str:
        """Get the object reference.

        Returns:
            `module:qualname` of the target.
        """
        return _reference(self.target)

    def load(self) -> Any:
        """Load the target.

        Returns:
            The target.
        """
        self.loaded += 1
        return self.target


def fake_entry_points(
    plugins: Mapping[str, Any],
    dist: FakeDistribution | None = None,
) -> Callable[[], list[FakeEntryPoint]]:
    """Get a function listing fake entry points.

    Pass it as `find_entry_points` of
    :py:func:`foca.plugins.loader.load_plugins`.

    Args:
        plugins: Targets (plugin classes) by plugin identifier.
        dist: Distribution of the entry points; defaults to
            `fake-plugins` 1.2.0.

    Returns:
        Function returning the entry points (the same objects each time).
    """
    eps = [
        FakeEntryPoint(name, target, dist=dist or FakeDistribution())
        for name, target in plugins.items()
    ]
    return lambda: list(eps)
