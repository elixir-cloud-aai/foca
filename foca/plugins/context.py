"""Context of a plugin call: request data, parameters, deadline and records."""

import copy
import json
import logging
import math
import time
from typing import Any

from pydantic import BaseModel

from foca.plugins.model import Principal
from foca.plugins.selection import check_i_json
from foca.utils.http import GuardedClient

MAX_ANNOTATIONS_SIZE = 2048
"""Maximum size of the annotations of one call, as compact JSON, in bytes."""

# Types of annotation values: JSON scalars, or short lists of them
_SCALARS = (str, int, float, bool, type(None))


def _is_scalar(value: Any) -> bool:
    """Check whether a value is a JSON scalar.

    Args:
        value: Value.

    Returns:
        Whether the value is a string, number (finite), boolean or `None`.
    """
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, _SCALARS)


_MAX_LIST_LENGTH = 16
_MAX_KEY_LENGTH = 64


class Context:
    """Context of one call of a plugin hook.

    Passed explicitly to every hook, so that plugins need neither the web
    framework's request context nor globals.

    Attributes:
        api: API of the app, e.g., `tes`.
        request_id: Identifier of the request, for logs.
        task_id: Identifier of the gateway's resource (e.g., the task), once
            created; `None` before.
        principal: Authenticated caller.
        params: Validated parameters of the plugin in this step.
        deadline: `time.monotonic()` value by which the hook must return.
        state: Data private to this plugin for this request, kept across
            phases (e.g., from `request` to `forward`) if the call succeeds.
        log: Logger of the plugin, with the plugin and request identifiers.
        http: Outbound HTTP client bounded by the deadline: public
            addresses and `https` only, no redirects, limited response size
            (cf. :py:class:`foca.utils.http.GuardedClient` and
            `plugins.defaults.http`); `None` only in contexts built by
            hand.
        annotations: Annotations recorded by the call (cf. `record()`).
    """

    def __init__(
        self,
        *,
        api: str,
        plugin_id: str,
        principal: Principal,
        params: BaseModel,
        original: Any,
        deadline: float,
        state: dict[str, Any],
        request_id: str | None = None,
        task_id: str | None = None,
        http: GuardedClient | None = None,
    ) -> None:
        """Create the context of a call.

        Args:
            api: API of the app.
            plugin_id: Identifier of the plugin.
            principal: Authenticated caller.
            params: Validated parameters.
            original: Payload of the inbound request before any plugin ran.
            deadline: `time.monotonic()` value by which the hook must return.
            state: Data private to the plugin for this request (a copy).
            request_id: Identifier of the request.
            task_id: Identifier of the gateway's resource, if created.
            http: Outbound HTTP client, if any.
        """
        self.api = api
        self.request_id = request_id
        self.task_id = task_id
        self.principal = principal
        self.params = params
        self.deadline = deadline
        self.state = state
        self.http = http
        self.annotations: dict[str, Any] = {}
        self._original = original
        self.log = logging.LoggerAdapter(
            logging.getLogger(f"foca.plugins.{plugin_id}"),
            {"plugin": plugin_id, "request_id": request_id},
        )

    @property
    def original(self) -> Any:
        """Get the payload of the inbound request before any plugin ran.

        Returns:
            A copy of the payload, so changes have no effect.
        """
        return copy.deepcopy(self._original)

    def remaining(self) -> float:
        """Get the seconds left until the deadline.

        Returns:
            Seconds, at least 0.
        """
        return max(0.0, self.deadline - time.monotonic())

    def record(self, key: str, value: Any) -> None:
        """Record an annotation of the call, e.g., for provenance.

        Annotations must not contain personal data. Values are JSON scalars
        or lists of at most 16 scalars; all annotations of a call take at
        most `MAX_ANNOTATIONS_SIZE` bytes as compact JSON.

        Args:
            key: Name of the annotation, at most 64 characters.
            value: Value.

        Raises:
            ValueError: The key or value is invalid (also if it is no I-JSON,
                e.g., integers beyond 2^53 - 1), or the annotations get too
                large.
        """
        if not isinstance(key, str) or not 0 < len(key) <= _MAX_KEY_LENGTH:
            raise ValueError("Annotation keys must be strings of 1 to 64 characters")
        if isinstance(value, list | tuple):
            if len(value) > _MAX_LIST_LENGTH or not all(map(_is_scalar, value)):
                raise ValueError("Annotation lists hold at most 16 JSON scalars")
            value = list(value)
        elif not _is_scalar(value):
            raise ValueError("Annotation values must be JSON scalars or lists")
        check_i_json({key: value})  # e.g., exact integers, valid Unicode
        annotations = {**self.annotations, key: value}
        size = len(json.dumps(annotations, separators=(",", ":")).encode())
        if size > MAX_ANNOTATIONS_SIZE:
            raise ValueError(f"Annotations exceed {MAX_ANNOTATIONS_SIZE} bytes")
        self.annotations = annotations
