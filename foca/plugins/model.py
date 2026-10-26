"""Framework-neutral data model of gateway requests, as plugins see them.

Plain dataclasses, without Flask, Werkzeug, Connexion or Starlette types:
FOCA builds them from the request of the web framework (cf.
:py:mod:`foca.plugins.adapter`), so that plugins run unchanged when FOCA
moves to another framework.

A gateway request has three legs and one decision (cf. the phases of
:py:class:`foca.plugins.base.Plugin`):

1. the inbound request of the client (:py:class:`InboundRequest`);
2. the routing decision: candidate backends, in order (:py:class:`Backend`);
3. one outbound request per attempt (:py:class:`OutboundRequest`);
4. the response of the backend per attempt (:py:class:`BackendResponse`).
"""

import copy
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Literal

# Headers that the engine never hands to plugins as headers: credentials of
# the client, which are available as `Principal.token` instead, so that they
# are not forwarded to backends by accident
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


class Headers(MutableMapping[str, str]):
    """Mutable, case-insensitive mapping of HTTP header names to values.

    Keeps the spelling of names as last set and the order of insertion.
    Repeated headers are joined with commas (RFC 9110, section 5.3), so each
    name has one value.

    Examples:
        >>> headers = Headers({"Content-Type": "application/json"})
        >>> headers["content-type"]
        'application/json'
        >>> "CONTENT-TYPE" in headers
        True
    """

    def __init__(self, items: Mapping[str, str] | None = None) -> None:
        """Create headers.

        Args:
            items: Initial headers, if any.
        """
        self._items: dict[str, tuple[str, str]] = {}
        if items is not None:
            self.update(items)

    def __getitem__(self, name: str) -> str:
        """Get the value of a header.

        Args:
            name: Header name, in any case.

        Returns:
            Value of the header.
        """
        return self._items[name.lower()][1]

    def __setitem__(self, name: str, value: str) -> None:
        """Set a header.

        Args:
            name: Header name.
            value: Value of the header.

        Raises:
            TypeError: The name or value is not a string.
            ValueError: The name or value contains line breaks or NUL
                characters, which would split or end the header.
        """
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("Header names and values must be strings")
        if any(char in f"{name}{value}" for char in "\r\n\x00"):
            raise ValueError("Header names and values must not contain line breaks")
        self._items[name.lower()] = (name, value)

    def __delitem__(self, name: str) -> None:
        """Remove a header.

        Args:
            name: Header name, in any case.
        """
        del self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the header names, as last set.

        Returns:
            Iterator over the names.
        """
        return (name for name, _ in self._items.values())

    def __len__(self) -> int:
        """Count the headers.

        Returns:
            Number of headers.
        """
        return len(self._items)

    def __repr__(self) -> str:
        """Represent the headers.

        Returns:
            Representation, with the header names only (values may be
            sensitive).
        """
        return f"Headers({list(self)!r})"

    def __eq__(self, other: object) -> bool:
        """Compare with other headers or a mapping, case-insensitively.

        Args:
            other: Other headers or mapping.

        Returns:
            Whether both have the same names (ignoring case) and values.
        """
        if not isinstance(other, Mapping):
            return NotImplemented
        return {name.lower(): value for name, value in self.items()} == {
            str(name).lower(): value for name, value in other.items()
        }

    __hash__ = None  # type: ignore[assignment]

    def copy(self) -> "Headers":
        """Copy the headers.

        Returns:
            Independent copy.
        """
        return Headers(self)


class FrozenMapping(Mapping[str, Any]):
    """Read-only mapping that can be copied, deep-copied and pickled.

    Unlike :py:class:`types.MappingProxyType`, plugins may store it (e.g.,
    the claims of :py:class:`Principal`) in their state, which the engine
    deep-copies per call.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        """Wrap a copy of a mapping.

        Args:
            data: Items; none if not given.
        """
        self._data: dict[str, Any] = dict(data or {})

    def __getitem__(self, key: str) -> Any:
        """Get the value of a key.

        Args:
            key: Key.

        Returns:
            Value.
        """
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the keys.

        Returns:
            Iterator of the keys.
        """
        return iter(self._data)

    def __len__(self) -> int:
        """Count the keys.

        Returns:
            Number of keys.
        """
        return len(self._data)

    def __repr__(self) -> str:
        """Represent the mapping.

        Returns:
            Representation.
        """
        return f"FrozenMapping({self._data!r})"

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenMapping":
        """Deep-copy the mapping.

        Args:
            memo: Objects copied so far.

        Returns:
            Independent read-only copy.
        """
        return FrozenMapping(copy.deepcopy(self._data, memo))

    def __reduce__(self) -> tuple[type["FrozenMapping"], tuple[dict[str, Any]]]:
        """Support pickling (and `copy.copy`).

        Returns:
            Class and arguments to recreate the mapping.
        """
        return FrozenMapping, (self._data,)


@dataclass(frozen=True)
class Principal:
    """Authenticated caller of a request.

    The caller is identified as by access control and its ownership
    helpers: `subject` and `subject_type` together are the identity, as
    the same identifier may be a user and a service client.

    Attributes:
        subject: Identity of the caller: the user's subject (cf.
            :py:func:`foca.security.access_control.get_current_subject`),
            or the client ID of a service client (cf.
            :py:func:`foca.security.access_control.get_current_client`).
        subject_type: Type of the subject: `user` or `client`; `None` if
            the caller is not identified. Defaults to `user` if there is a
            subject.
        acting_for: User that a service client acts for, as named in the
            header `auth.on_behalf_of_header` (cf.
            :py:func:`foca.security.access_control.get_on_behalf_of_user`);
            resources created for the request belong to this user (e.g.,
            the owner of a task).
        issuer: Issuer of the caller's token, if known.
        groups: Roles of the caller, as access control applies them (cf.
            :py:func:`foca.security.access_control.get_current_roles`):
            mapped from the service client and from group claims, and
            granted via global role rules.
        claims: Validated claims of the caller's token.
        token: The caller's bearer token, e.g., for token exchange or
            pass-through by a plugin; never forwarded by the engine itself.
    """

    subject: str | None = None
    subject_type: Literal["user", "client"] | None = None
    acting_for: str | None = None
    issuer: str | None = None
    groups: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict, hash=False)
    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Default the type of a subject to `user`.

        Raises:
            ValueError: A subject type is given without a subject.
        """
        if self.subject is None:
            if self.subject_type is not None:
                raise ValueError("A subject type needs a subject")
        elif self.subject_type is None:
            # frozen dataclass
            object.__setattr__(self, "subject_type", "user")


@dataclass
class InboundRequest:
    """Request of a client to the gateway, after its security checks.

    Attributes:
        method: HTTP method, uppercase.
        path: Path below the root path of the app, e.g., `/ga4gh/tes/v1/tasks`.
        route: Path template of the operation, e.g., `/ga4gh/tes/v1/tasks`.
        path_params: Path parameters.
        query: Query parameters, each with its values in order.
        headers: Headers, without credentials (cf. `CREDENTIAL_HEADERS`).
        media_type: Media type of the body, without parameters.
        payload: Parsed body, e.g., the task of `POST /tasks` (a `dict`).
    """

    method: str
    path: str
    route: str = ""
    path_params: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: Headers = field(default_factory=Headers)
    media_type: str = "application/json"
    payload: Any = None


@dataclass(frozen=True)
class Location:
    """Geographic location of a backend, as configured by an admin.

    Attributes:
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.
    """

    latitude: float
    longitude: float


@dataclass(frozen=True)
class Backend:
    """Backend that requests may be forwarded to.

    Attributes:
        id: Identifier of the backend, unique among the candidates of a
            request; the normalized base URL unless configured otherwise.
        url: Normalized base URL, as configured or discovered.
        source: Whether the backend was configured or discovered in a
            service registry.
        group: Identifier of the backend list entry that yielded the backend
            (e.g., of a service registry), if not configured directly.
        labels: Labels set by the admin, e.g., `{"region": "eu-north"}`.
        location: Location set by the admin, if any.
        auth: Name of the credentials to use for the backend, as configured
            (e.g., `client_credentials`, `basic` or `none`).
    """

    id: str
    url: str
    source: Literal["configured", "discovered"] = "configured"
    group: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict, hash=False)
    location: Location | None = None
    auth: str = "none"


@dataclass
class OutboundRequest:
    """Request of the gateway to a backend, for one attempt.

    Credentials of the backend are added by the app after the plugins ran,
    so plugins never see them; they may choose other configured credentials
    by name (`auth`).

    Attributes:
        backend: Backend of the attempt; plugins must not change it.
        method: HTTP method, uppercase.
        path: Path below the base URL of the backend, starting with `/`.
        query: Query parameters, each with its values in order.
        headers: Headers, without credentials.
        payload: Body for this backend (a copy per attempt), serialized by
            the app.
        auth: Name of the credentials to use; `None` for those of the
            backend.
    """

    backend: Backend
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: Headers = field(default_factory=Headers)
    payload: Any = None
    auth: str | None = None


@dataclass
class BackendResponse:
    """Response of a backend to an attempt, and the response to the client.

    Plugins may change only the response to the client (`client_payload`,
    `client_headers`); the gateway alone decides whether the backend
    accepted the request.

    Attributes:
        status: HTTP status of the backend's response.
        headers: Headers of the backend's response.
        payload: Parsed body of the backend's response.
        client_payload: Body of the response to the client.
        client_headers: Headers of the response to the client.
    """

    status: int
    headers: Headers = field(default_factory=Headers)
    payload: Any = None
    client_payload: Any = None
    client_headers: Headers = field(default_factory=Headers)
