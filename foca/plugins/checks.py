"""Outbound requests, `Via` headers and checks of hook outputs."""

import copy
from collections.abc import Mapping
from typing import Any

from foca.plugins.model import (
    Backend,
    BackendResponse,
    Headers,
    InboundRequest,
    OutboundRequest,
)


def _via_names(headers: Mapping[str, str]) -> list[str]:
    """Get the names of the proxies in a `Via` header.

    Args:
        headers: Headers.

    Returns:
        `received-by` names, lowercase (RFC 9110, section 7.6.3).
    """
    names = []
    for entry in headers.get("Via", "").split(","):
        parts = entry.split()
        if len(parts) >= 2:
            names.append(parts[1].lower())
    return names


def default_outbound(backend: Backend, inbound: InboundRequest) -> OutboundRequest:
    """Build the outbound request of an attempt from the inbound request.

    Args:
        backend: Backend of the attempt.
        inbound: Inbound request, after the `request` phase.

    Returns:
        Request with the method, path, query and a copy of the payload of
        the inbound request; no headers of the client are forwarded.
    """
    return OutboundRequest(
        backend=backend,
        method=inbound.method,
        path=inbound.path,
        query=copy.deepcopy(inbound.query),
        headers=Headers(),
        payload=copy.deepcopy(inbound.payload),
    )


def _check_route(result: Any, candidates: list[Backend]) -> list[Backend]:
    """Check the output of a `route` hook.

    Args:
        result: Return value of the hook.
        candidates: Candidates given to the hook.

    Returns:
        The candidates returned, as the original objects.

    Raises:
        TypeError: The result is not a list of backends.
        ValueError: The result has backends that are not candidates, or
            duplicates.
    """
    if not isinstance(result, list) or not all(isinstance(b, Backend) for b in result):
        raise TypeError("Route hooks return a list of backends")
    by_id = {backend.id: backend for backend in candidates}
    ids = [backend.id for backend in result]
    if len(set(ids)) != len(ids):
        raise ValueError("Route hooks must not return backends twice")
    unknown = [backend_id for backend_id in ids if backend_id not in by_id]
    if unknown or any(by_id[b.id] != b for b in result):
        raise ValueError("Route hooks may only return given candidates, unchanged")
    return [by_id[backend_id] for backend_id in ids]


def _check_forward(
    candidate: OutboundRequest, given: OutboundRequest
) -> OutboundRequest:
    """Check the output of a `forward` hook.

    Args:
        candidate: Outbound request after the hook.
        given: Outbound request given to the hook.

    Returns:
        The outbound request.

    Raises:
        ValueError: The backend was changed, or the method or path is
            invalid.
        TypeError: The headers are not :py:class:`foca.plugins.model.Headers`.
    """
    if candidate.backend != given.backend:
        raise ValueError("Forward hooks must not change the backend")
    if not isinstance(candidate.headers, Headers):
        raise TypeError("Forward hooks must keep Headers")
    if not isinstance(candidate.method, str) or not candidate.method.isalpha():
        raise ValueError("Invalid method")
    candidate.method = candidate.method.upper()
    path = candidate.path
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Paths start with a single '/'")
    if any(char in path for char in "?#\\") or any(ord(char) < 33 for char in path):
        raise ValueError("Paths must not contain queries, fragments or controls")
    return candidate


def _check_response(
    candidate: BackendResponse, given: BackendResponse
) -> BackendResponse:
    """Check the output of a `response` hook.

    Args:
        candidate: Response after the hook.
        given: Response given to the hook.

    Returns:
        The response.

    Raises:
        ValueError: The backend's status, headers or payload were changed.
    """
    if (
        candidate.status != given.status
        or candidate.headers != given.headers
        or candidate.payload != given.payload
    ):
        raise ValueError("Response hooks may only change the client's response")
    return candidate
