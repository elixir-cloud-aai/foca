"""Routes of an app, from the operations of its API specifications.

The table holds what FOCA and apps need to know about the routes of an app,
without asking the web framework: it is filled while specifications are
registered, so it is complete before the first request, whenever the framework
builds its own routes.

Paths have one meaning throughout: path templates include the base path of the
API and exclude the root path of the app (`SCRIPT_NAME` or ASGI's `root_path`),
and paths to match are request paths, i.e., with the same parts and not
percent-encoded. It is what access control policies and the ownership of
resources are keyed by, cf. :py:func:`foca.utils.urls.get_resource_path`.

Connexion 2 is only known to :py:func:`_install_route_table`.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NamedTuple, cast

from connexion.apis.flask_api import FlaskApi
from connexion.apis.flask_utils import flaskify_path
from connexion.apps.flask_app import FlaskApp, IntegerConverter, NumberConverter
from flask import current_app
from werkzeug.exceptions import HTTPException
from werkzeug.routing import Map, Rule
from werkzeug.routing.exceptions import RoutingException

# Key of the app's extensions holding its routes
_ROUTES_EXTENSION = "foca_routes"

_EXTENSION_PREFIX = "x-foca-"


@dataclass(frozen=True)
class Route:
    """Route to an operation of an API specification.

    Attributes:
        method: HTTP method, in upper case.
        path: Path template, as in the specification, with the base path of
            the API, e.g., `/ga4gh/tes/v1/tasks/{id}`.
        rule: Path template as routed, e.g., `/ga4gh/tes/v1/tasks/<id>`.
        operation_id: Identifier of the operation, as resolved.
        security: Effective security requirements of the operation.
        extensions: Fields of the operation starting with `x-foca-`.
        function: Function that the operation's resolver returned, with the
            wrappers of FOCA's resolvers, e.g., for enforcing access control;
            use `inspect.unwrap()` for the controller.
    """

    method: str
    path: str
    rule: str
    operation_id: str | None
    security: list[dict[str, Any]] | None
    extensions: Mapping[str, Any]
    function: Callable[..., Any]


class RouteMatch(NamedTuple):
    """Route found for a path and method.

    Attributes:
        route: The route.
        params: Values of its path parameters, as passed to controllers, i.e.,
            decoded and converted to the parameters' types, under the names
            of the route's `rule`.
    """

    route: Route
    params: dict[str, Any]


@dataclass
class _RouteTable:
    """Routes of an app.

    Attributes:
        routes: Routes, in the order of registration.
        rules: Rules of the routes, matching paths like those of the framework.
    """

    routes: list[Route] = field(default_factory=list)
    rules: Map = field(
        default_factory=lambda: Map(
            converters={"float": NumberConverter, "int": IntegerConverter}
        )
    )

    def add(self, route: Route) -> None:
        """Add a route.

        Args:
            route: The route. Like Flask's, its rule also answers `OPTIONS` by
                itself, unless the operation is one, so that the order of the
                rules for a path counts in the same way.
        """
        self.routes.append(route)
        self.rules.add(
            Rule(
                route.rule,
                endpoint=len(self.routes) - 1,
                methods={route.method, "OPTIONS"},
            )
        )


def _add_route(app: Any, route: Route) -> None:
    """Add a route to the table of an app.

    Args:
        app: Flask app.
        route: The route.
    """
    app.extensions.setdefault(_ROUTES_EXTENSION, _RouteTable()).add(route)


class _RecordingMarker:
    """Marks the API classes that record their operations."""


def _install_route_table(cnx_app: Any) -> None:
    """Make a Connexion 2 app record the operations of its APIs as routes.

    Args:
        cnx_app: Connexion app; this covers all calls of its `add_api()`, also
            by apps. Nothing is done if it is installed already, or for other
            objects, e.g., stand-ins in tests.
    """
    if not isinstance(cnx_app, FlaskApp) or issubclass(
        cnx_app.api_cls, _RecordingMarker
    ):
        return
    flask_app = cnx_app.app

    class _RecordingApi(cnx_app.api_cls, _RecordingMarker):
        """API recording its operations in the route table of the app."""

        def _add_operation_internal(
            self, method: str, path: str, operation: Any
        ) -> None:
            """Add an operation to the API and the route table.

            Args:
                method: HTTP method.
                path: Path template, relative to the base path of the API.
                operation: Connexion operation.
            """
            super()._add_operation_internal(method, path, operation)
            prefix = (self.base_path or "").rstrip("/")
            rule = flaskify_path(path, operation.get_path_parameter_types())
            fields = self.specification.get_operation(path, method) or {}
            # Operations that failed to resolve have a function answering so
            resolution = getattr(operation, "_resolution", None)
            _add_route(
                flask_app,
                Route(
                    method=method.upper(),
                    path=f"{prefix}/{path.lstrip('/')}",
                    rule=f"{prefix}/{rule.lstrip('/')}",
                    operation_id=operation.operation_id,
                    security=operation.security,
                    extensions=MappingProxyType(
                        {
                            key: value
                            for key, value in fields.items()
                            if key.startswith(_EXTENSION_PREFIX)
                        }
                    ),
                    function=(
                        operation.function
                        if resolution is None
                        else resolution.function
                    ),
                ),
            )

    cnx_app.api_cls = cast("type[FlaskApi]", _RecordingApi)


def get_routes(app: Any = None) -> list[Route]:
    """Get the routes to the operations of an app.

    Args:
        app: Flask app; by default, the current one.

    Returns:
        Routes, in the order of registration. Others that an app or Connexion
        adds (e.g., the Swagger UI) have no operation, and aren't included, so
        checks on this table don't cover them.
    """
    table = _get_table(app)
    return [] if table is None else list(table.routes)


def match(path: str, method: str, app: Any = None) -> RouteMatch | None:
    """Find the route to the operation serving a path and method.

    It finds what a request would be routed to, e.g., for `HEAD` the route of
    a `GET` operation. Routes that aren't operations are unknown to it, so it
    also doesn't see one taking precedence, e.g., the served specification over
    `/{any}` in an API at `/`.

    Args:
        path: Request path: with the base path of the API, without the root
            path of the app, not percent-encoded.
        method: HTTP method.
        app: Flask app; by default, the current one.

    Returns:
        The route and its path parameters, or `None` if no operation serves
        the path and method directly, e.g., if it would redirect, or for the
        `OPTIONS` requests that answer by themselves.
    """
    found = _match_rule(path, method, app)
    if found is None or found[1]:
        return None
    return found[0]


def _match_rule(
    path: str, method: str, app: Any = None
) -> tuple[RouteMatch, bool] | None:
    """Find the rule serving a path and method.

    Args:
        path: Request path (cf. :py:func:`match`).
        method: HTTP method.
        app: Flask app; by default, the current one.

    Returns:
        The route with its path parameters, and whether the request gets an
        automatic answer to `OPTIONS` rather than reaching the operation, or
        `None` if no rule serves the path and method directly.
    """
    table = _get_table(app)
    method = method.upper()
    # Werkzeug would normalize these, unlike requests
    if table is None or not method or not path.startswith("/") or path.startswith("//"):
        return None
    try:
        endpoint, params = table.rules.bind("localhost").match(path, method=method)
    except (HTTPException, RoutingException):
        return None
    route = table.routes[cast("int", endpoint)]
    return RouteMatch(
        route, dict(params)
    ), method == "OPTIONS" and route.method != "OPTIONS"


def _get_table(app: Any = None) -> _RouteTable | None:
    """Get the route table of an app.

    Args:
        app: Flask app; by default, the current one.

    Returns:
        The table, if it has one.
    """
    extensions = (current_app if app is None else app).extensions
    return cast("_RouteTable | None", extensions.get(_ROUTES_EXTENSION))
