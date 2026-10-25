"""Per-request choices of clients: pipelines and backends, via reserved tags.

Clients choose the pipeline and the backends of a request in two reserved
tags of the request's payload (TES and WES `tags`, a string map, so the
choice is spec-compliant and works with existing clients), under the
configurable prefix `plugins.tags.prefix` (default `gateway.`):

- `gateway.pipeline`: the name of a preset (`plugins.presets`), or a
  pipeline as JSON (a list of steps, cf.
  :py:class:`foca.plugins.config.StepConfig`, without `timeout`,
  `on_failure` and `if_missing_phase`);
- `gateway.backends`: a JSON object `{"type": "order" | "allow" | "forbid",
  "backends": [<backend identifiers or URLs>]}`.

All other tags under the prefix are written by the gateway only; clients
setting them get `400`, so that tags under the prefix can be trusted.
"""

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from foca.plugins.config import AlternativeConfig, StepConfig
from foca.plugins.errors import InvalidSelection
from foca.plugins.model import Backend
from foca.utils.urls import normalize_url

PIPELINE_TAG = "pipeline"
"""Name of the tag with the client's pipeline, after the prefix."""

BACKENDS_TAG = "backends"
"""Name of the tag with the client's backend selection, after the prefix."""

MAX_BACKENDS_SIZE = 8192
"""Maximum size of the backends tag, in bytes."""

_MAX_BACKEND_ITEM_LENGTH = 2048


class ClientStepConfig(BaseModel):
    """Step of a client's pipeline: admin-only keys are not allowed.

    Args:
        id: Identifier of the step; defaults to the first plugin.
        plugin: Plugin of the short form (one alternative).
        params: Parameters of the short form.
        alternatives: Alternatives, in order.

    Attributes:
        id: Identifier of the step, if set.
        plugin: Plugin of the short form, if used.
        params: Parameters of the short form.
        alternatives: Alternatives, if not in the short form.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    plugin: str | None = None
    params: dict[str, Any] = {}
    alternatives: list[AlternativeConfig] | None = None

    def to_step(self) -> StepConfig:
        """Convert to a step configuration.

        Returns:
            Step with the admin defaults.
        """
        data: dict[str, Any] = self.model_dump(exclude_none=True)
        if self.plugin is None:
            data.pop("params", None)
        return StepConfig.model_validate(data)


class BackendSelection(BaseModel):
    """Client's choice of backends (tag `gateway.backends`).

    Args:
        type: `order`: the listed backends first, in the client's order,
            then the other candidates in the server's order; `allow`: only
            the listed backends, in the server's order; `forbid`: all but
            the listed backends, in the server's order.
        backends: Identifiers or URLs of backends.

    Attributes:
        type: Kind of selection.
        backends: Identifiers or URLs of backends.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["order", "allow", "forbid"]
    backends: list[str] = Field(min_length=1, max_length=1024)

    @field_validator("backends")
    @classmethod
    def _check_items(cls, backends: list[str]) -> list[str]:
        """Check the length of the items.

        Args:
            backends: Items.

        Returns:
            The items.

        Raises:
            ValueError: An item is empty or too long.
        """
        if any(not 0 < len(item) <= _MAX_BACKEND_ITEM_LENGTH for item in backends):
            raise ValueError(
                f"Backends are strings of 1 to {_MAX_BACKEND_ITEM_LENGTH} characters"
            )
        return backends


@dataclass(frozen=True)
class ClientSelection:
    """Choices of a client for a request, from its tags.

    Attributes:
        pipeline: Value of the pipeline tag, as sent, if any.
        backends: Backend selection, if any.
    """

    pipeline: str | None = None
    backends: BackendSelection | None = None


def parse_client_tags(
    tags: object,
    prefix: str,
    max_pipeline_size: int,
    max_backends: int,
    pipelines_enabled: bool = True,
    backends_enabled: bool = True,
) -> ClientSelection:
    """Read the client's choices from the tags of a payload.

    Args:
        tags: Tags of the payload (a string map), if any.
        prefix: Reserved prefix (`plugins.tags.prefix`).
        max_pipeline_size: Maximum size of the pipeline tag, in bytes.
        max_backends: Maximum number of backends in the backends tag.
        pipelines_enabled: Whether clients may choose pipelines.
        backends_enabled: Whether clients may choose backends.

    Returns:
        Choices; empty if there are no tags.

    Raises:
        InvalidSelection: The tags are not a mapping, a tag under the prefix
            other than the pipeline and backends tags is set
            (`reserved-tag`), a choice is disabled, or a tag value is
            invalid.
    """
    if tags is None:
        return ClientSelection()
    if not isinstance(tags, Mapping):
        raise InvalidSelection("Tags are a map of strings", slug="reserved-tag")
    allowed = {f"{prefix}{PIPELINE_TAG}", f"{prefix}{BACKENDS_TAG}"}
    reserved = sorted(
        str(key) for key in tags if str(key).startswith(prefix) and key not in allowed
    )
    if reserved:
        raise InvalidSelection(
            f"Tags under {prefix!r} are reserved for the gateway: {reserved[0]}",
            slug="reserved-tag",
        )
    pipeline = tags.get(f"{prefix}{PIPELINE_TAG}")
    raw = tags.get(f"{prefix}{BACKENDS_TAG}")
    if pipeline is not None and not pipelines_enabled:
        raise InvalidSelection("Choosing pipelines per request is disabled")
    if raw is not None and not backends_enabled:
        raise InvalidSelection(
            "Choosing backends per request is disabled", slug="invalid-backends"
        )
    if pipeline is not None and (
        not isinstance(pipeline, str) or _size(pipeline) > max_pipeline_size
    ):
        raise InvalidSelection(
            f"Pipelines are UTF-8 strings of at most {max_pipeline_size} bytes"
        )
    backends = None
    if raw is not None:
        backends = _parse_backends(raw, max_backends)
    return ClientSelection(pipeline=pipeline, backends=backends)


def _size(value: str) -> float:
    """Get the size of a tag value in UTF-8.

    Args:
        value: Tag value.

    Returns:
        Bytes; infinite if the value is no valid Unicode (e.g., has unpaired
        surrogates), so that it exceeds every limit.
    """
    try:
        return len(value.encode())
    except UnicodeEncodeError:
        return float("inf")


def _parse_backends(raw: object, max_backends: int) -> BackendSelection:
    """Parse the backends tag.

    Args:
        raw: Value of the tag.
        max_backends: Maximum number of backends.

    Returns:
        Backend selection.

    Raises:
        InvalidSelection: The value is not a valid selection.
    """
    if isinstance(raw, str) and _size(raw) > MAX_BACKENDS_SIZE:
        raise InvalidSelection(
            f"Backend selections are UTF-8 strings of at most {MAX_BACKENDS_SIZE}"
            " bytes",
            slug="invalid-backends",
        )
    try:
        if not isinstance(raw, str):
            raise TypeError("not a string")
        selection = BackendSelection.model_validate(json.loads(raw))
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidSelection(
            'Backends are selected as JSON {"type": "order" | "allow" | "forbid",'
            ' "backends": [...]}',
            slug="invalid-backends",
        ) from exc
    if len(selection.backends) > max_backends:
        raise InvalidSelection(
            f"At most {max_backends} backends can be selected",
            slug="invalid-backends",
        )
    return selection


MAX_SAFE_INTEGER = 2**53 - 1
"""Largest integer that JSON numbers represent exactly (I-JSON, RFC 7493)."""

MAX_JSON_DEPTH = 32
"""Maximum nesting depth of client pipelines (arrays and objects)."""


def _reject_constant(name: str) -> None:
    """Reject the JSON extensions `NaN`, `Infinity` and `-Infinity`.

    Args:
        name: Constant.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"{name} is not allowed")


def check_i_json(data: Any) -> None:
    """Check that parsed JSON is I-JSON (RFC 7493): finite, exact numbers.

    Numbers that hashes (RFC 8785) cannot represent exactly would make the
    records of different pipelines collide, or fail.

    Args:
        data: Parsed JSON.

    Raises:
        ValueError: A number is not finite, an integer is beyond
            :py:data:`MAX_SAFE_INTEGER`, a string has unpaired surrogates,
            or arrays and objects are nested more than
            :py:data:`MAX_JSON_DEPTH` levels deep.
    """
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, str):
            _check_string(item)
        elif isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                raise ValueError("Integers are at most 2^53 - 1")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Numbers must be finite")
        elif isinstance(item, dict | list):
            if depth > MAX_JSON_DEPTH:
                raise ValueError(f"Nesting is at most {MAX_JSON_DEPTH} levels deep")
            children: Iterable[Any]
            if isinstance(item, dict):
                for key in item:
                    _check_string(key)
                children = item.values()
            else:
                children = item
            stack.extend((child, depth + 1) for child in children)


def _check_string(value: str) -> None:
    """Check that a string is valid Unicode (no unpaired surrogates).

    Args:
        value: String.

    Raises:
        ValueError: The string has unpaired surrogates.
    """
    try:
        value.encode()
    except UnicodeEncodeError:
        raise ValueError("Strings must be valid Unicode") from None


def parse_client_pipeline(value: str) -> list[StepConfig]:
    """Parse a client's pipeline given as JSON.

    Args:
        value: Value of the pipeline tag.

    Returns:
        Steps.

    Raises:
        InvalidSelection: The value is not a valid pipeline.
    """
    try:
        data = json.loads(value, parse_constant=_reject_constant)
        if not isinstance(data, list):
            raise TypeError("not a list")
        check_i_json(data)
        steps = [ClientStepConfig.model_validate(item).to_step() for item in data]
    except (TypeError, ValueError, RecursionError) as exc:
        detail = _first_error(exc)
        raise InvalidSelection(f"Invalid pipeline: {detail}") from exc
    return steps


def _first_error(exc: BaseException) -> str:
    """Describe a parsing error briefly, for clients.

    Args:
        exc: Error.

    Returns:
        First validation error, or a generic message.
    """
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        return f"{location}: {error['msg']}" if location else str(error["msg"])
    if isinstance(exc, json.JSONDecodeError):
        return "not a preset name or JSON"
    if isinstance(exc, ValueError):
        return str(exc)
    return "a pipeline is a list of steps"


def _key(item: str) -> str:
    """Get the comparison key of a backend item: its normalized URL if valid.

    Args:
        item: Backend identifier or URL.

    Returns:
        Normalized URL, or the item as given.
    """
    try:
        return normalize_url(item)
    except ValueError:
        return item


def apply_backend_selection(
    candidates: Sequence[Backend], selection: BackendSelection
) -> tuple[list[Backend], list[str]]:
    """Apply a client's backend selection to the routed candidates.

    Items that are not candidates (unknown, unverified, of another type, or
    removed by routing) are discarded; the server never adds backends.

    Args:
        candidates: Candidates after the `route` phase, in order.
        selection: Client's selection.

    Returns:
        Candidates after the selection, in order, and the items that matched
        no candidate.
    """
    keys: dict[str, Backend] = {}
    for backend in candidates:
        keys.setdefault(backend.id, backend)
        keys.setdefault(_key(backend.url), backend)
    listed: list[Backend] = []
    discarded: list[str] = []
    for item in selection.backends:
        match = keys.get(item) or keys.get(_key(item))
        if match is None:
            discarded.append(item)
        elif match not in listed:
            listed.append(match)
    if selection.type == "order":
        return listed + [b for b in candidates if b not in listed], discarded
    if selection.type == "allow":
        return [b for b in candidates if b in listed], discarded
    return [b for b in candidates if b not in listed], discarded
