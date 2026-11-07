"""Test client and request contexts, independent of the web framework.

:py:class:`Client` sends requests to an app, with the API of `httpx` (and of
Starlette's `TestClient`), so that tests of apps don't use the test tooling of
Flask, which Connexion 3 bypasses. :py:func:`request_context` sets up a
request without HTTP, for unit tests of code that reads it via
:py:mod:`foca.context` or :py:mod:`foca.security.auth`.

```python
from foca.testing import Client, request_context

with Client(app) as client:
    response = client.get("/pets", params={"limit": 10})
    assert response.json() == []

with request_context("/pets", token_info={"sub": "alice"}, app=app):
    ...
```
"""

import json as json_module
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

from connexion import App
from flask import Flask
from werkzeug.test import TestResponse

from foca.context import Headers, _set_token_info

HeaderTypes = Mapping[str, str] | Iterable[tuple[str, str]]
"""Headers: a mapping, or names and values, e.g., for repeated headers."""

_ParamValue = str | int | float | bool
ParamTypes = (
    Mapping[str, _ParamValue | Sequence[_ParamValue]]
    | Iterable[tuple[str, _ParamValue]]
    | str
)
"""Query parameters: a mapping, with sequences for repeated parameters, names
and values, or an encoded query string."""


def _get_flask_app(app: App | Flask) -> Flask:
    """Get the Flask app of an app.

    Args:
        app: App, as :py:meth:`foca.Foca.create_app` returns it, or a Flask
            app.

    Returns:
        The Flask app.
    """
    return app if isinstance(app, Flask) else app.app


def _encode_params(params: ParamTypes) -> str:
    """Encode query parameters.

    Args:
        params: Query parameters.

    Returns:
        Query string, without `?`.
    """
    if isinstance(params, str):
        return params.lstrip("?")
    if isinstance(params, Mapping):
        items: list[tuple[str, _ParamValue]] = []
        for name, value in params.items():
            if isinstance(value, Sequence) and not isinstance(value, str):
                items.extend((name, item) for item in value)
            else:
                items.append((name, value))
        params = items
    return urlencode([(name, _format_param(value)) for name, value in params])


def _format_param(value: _ParamValue) -> str:
    """Format the value of a query parameter as `httpx` does.

    Args:
        value: Value.

    Returns:
        The value as a string; booleans in lower case.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _with_params(url: str, params: ParamTypes | None) -> str:
    """Add query parameters to a URL.

    Args:
        url: URL or path, possibly with a query string.
        params: Query parameters, or `None` for none.

    Returns:
        The URL, with the parameters after those it has.
    """
    if params is None:
        return url
    query = _encode_params(params)
    if not query:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _header_items(headers: HeaderTypes | None) -> list[tuple[str, str]]:
    """Get the names and values of headers.

    Args:
        headers: Headers, or `None` for none.

    Returns:
        Names and values, in order.
    """
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return list(headers.items())
    return list(headers)


class Response:
    """Response to a request of :py:class:`Client`, as in `httpx`.

    Attributes:
        status_code: HTTP status code.
        headers: Headers, case-insensitive and read-only.
        content: Body, as bytes.
    """

    def __init__(self, status_code: int, headers: Headers, content: bytes) -> None:
        """Hold a response.

        Args:
            status_code: HTTP status code.
            headers: Headers.
            content: Body.
        """
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @classmethod
    def _from_werkzeug(cls, response: TestResponse) -> Self:
        """Take a response of Werkzeug's test client.

        Args:
            response: Response, which is closed afterwards.

        Returns:
            The response.
        """
        try:
            return cls(
                status_code=response.status_code,
                headers=Headers(response.headers.items()),
                content=response.get_data(),
            )
        finally:
            response.close()

    @property
    def text(self) -> str:
        """Body, decoded.

        Returns:
            The body, decoded with the charset of the `Content-Type` header,
            or as UTF-8, with undecodable bytes replaced.
        """
        return self.content.decode(self._charset(), errors="replace")

    def _charset(self) -> str:
        """Get the charset of the body.

        Returns:
            Charset of the `Content-Type` header, or `utf-8` if there is none
            or it is unknown.
        """
        for parameter in self.headers.get("content-type", "").split(";")[1:]:
            name, _, value = parameter.strip().partition("=")
            if name.lower() == "charset":
                charset = value.strip().strip('"')
                try:
                    "".encode(charset)
                except LookupError:
                    break
                return charset
        return "utf-8"

    def json(self) -> Any:
        """Parse the body as JSON, whatever the `Content-Type` header.

        Returns:
            The parsed body.

        Raises:
            json.JSONDecodeError: Raised if the body isn't JSON.
        """
        return json_module.loads(self.text)

    def __repr__(self) -> str:
        """Represent the response.

        Returns:
            Representation with the status code.
        """
        return f"<Response [{self.status_code}]>"


class Client:
    """Client sending requests to an app, as in tests, without a server.

    Its API is that of `httpx` and of Starlette's `TestClient`. Use it as a
    context manager, so that the app starts and stops as when served.

    Args:
        app: App, as :py:meth:`foca.Foca.create_app` returns it; a Flask
            app also works.
        follow_redirects: Whether to follow redirects; requests may override
            it.
    """

    def __init__(self, app: App | Flask, *, follow_redirects: bool = False) -> None:
        """Make a client of an app.

        Args:
            app: App, as :py:meth:`foca.Foca.create_app` returns it; a Flask
                app also works.
            follow_redirects: Whether to follow redirects; requests may
                override it.
        """
        self.follow_redirects = follow_redirects
        self._client = _get_flask_app(app).test_client()

    def __enter__(self) -> Self:
        """Start using the client.

        Returns:
            The client.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop using the client.

        Args:
            exc_type: Type of the exception raised in the block, if any.
            exc_value: Exception raised in the block, if any.
            traceback: Traceback of the exception, if any.
        """
        self.close()

    def close(self) -> None:
        """Release what the client holds; on Connexion 2, nothing."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a request.

        Args:
            method: HTTP method.
            url: Path of the request, below the root path of the app, possibly
                with a query string.
            params: Query parameters, after those in `url`.
            headers: Headers.
            json: Body, serialized as JSON, with the `Content-Type`
                `application/json` unless set in `headers`.
            content: Raw body; a string is encoded as UTF-8.
            data: Form fields, encoded as `application/x-www-form-urlencoded`.
            follow_redirects: Whether to follow redirects; by default, as set
                for the client.

        Returns:
            Response.

        Raises:
            ValueError: Raised if more than one of `json`, `content` and
                `data` is given.
        """
        bodies = [body for body in (json, content, data) if body is not None]
        if len(bodies) > 1:
            raise ValueError("Give only one of json, content and data")
        header_items = _header_items(headers)
        body: bytes | str | Mapping[str, str] | None = None
        if json is not None:
            body = json_module.dumps(json)
            if not any(name.lower() == "content-type" for name, _ in header_items):
                header_items.append(("Content-Type", "application/json"))
        elif content is not None:
            body = content
        elif data is not None:
            body = dict(data)
        response = self._client.open(
            _with_params(url, params),
            method=method.upper(),
            headers=header_items,
            data=body,
            follow_redirects=(
                self.follow_redirects if follow_redirects is None else follow_redirects
            ),
        )
        return Response._from_werkzeug(response)

    def get(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a `GET` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    def head(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a `HEAD` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "HEAD",
            url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    def options(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send an `OPTIONS` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "OPTIONS",
            url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    def delete(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a `DELETE` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "DELETE",
            url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    def post(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a `POST` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            json: Body, serialized as JSON.
            content: Raw body.
            data: Form fields.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "POST",
            url,
            params=params,
            headers=headers,
            json=json,
            content=content,
            data=data,
            follow_redirects=follow_redirects,
        )

    def put(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a `PUT` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            json: Body, serialized as JSON.
            content: Raw body.
            data: Form fields.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "PUT",
            url,
            params=params,
            headers=headers,
            json=json,
            content=content,
            data=data,
            follow_redirects=follow_redirects,
        )

    def patch(
        self,
        url: str,
        *,
        params: ParamTypes | None = None,
        headers: HeaderTypes | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> Response:
        """Send a `PATCH` request.

        Args:
            url: Path, as for :py:meth:`request`.
            params: Query parameters.
            headers: Headers.
            json: Body, serialized as JSON.
            content: Raw body.
            data: Form fields.
            follow_redirects: Whether to follow redirects.

        Returns:
            Response.
        """
        return self.request(
            "PATCH",
            url,
            params=params,
            headers=headers,
            json=json,
            content=content,
            data=data,
            follow_redirects=follow_redirects,
        )


@contextmanager
def request_context(
    path: str = "/",
    method: str = "GET",
    headers: HeaderTypes | None = None,
    token_info: Mapping[str, Any] | None = None,
    *,
    app: App | Flask | None = None,
    params: ParamTypes | None = None,
    root_path: str = "",
    client_address: str | None = "127.0.0.1",
) -> Iterator[None]:
    """Set up a request without HTTP, for unit tests.

    Code in the block sees the request via :py:mod:`foca.context` and
    :py:mod:`foca.security.auth`, as when an app handles it, and the app's
    configuration. Nothing handles the request: no route, security check or
    controller runs.

    Args:
        path: Path of the request, below the root path, possibly with a query
            string.
        method: HTTP method.
        headers: Headers.
        token_info: Token info, as if Connexion had validated a token, e.g.,
            `{"sub": "alice"}`; none if `None`.
        app: App, as :py:meth:`foca.Foca.create_app` returns it, or a Flask
            app; an empty app if `None`.
        params: Query parameters, after those in `path`.
        root_path: Root path of the app, e.g., `/tes`, or empty for none.
        client_address: Address of the client, as the test client gives it,
            or `None` if unknown.

    Yields:
        Nothing; the request is set up in the block.
    """
    flask_app = Flask(__name__) if app is None else _get_flask_app(app)
    environ_base = {} if client_address is None else {"REMOTE_ADDR": client_address}
    with flask_app.test_request_context(
        _with_params(path, params),
        method=method.upper(),
        headers=_header_items(headers),
        base_url=f"http://localhost{root_path}",
        environ_base=environ_base,
    ):
        if token_info is not None:
            _set_token_info(token_info)
        yield
