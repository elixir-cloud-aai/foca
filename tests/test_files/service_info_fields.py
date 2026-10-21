"""Functions returning service info fields at runtime, for tests."""

from typing import Any

from foca import get_config

NOT_CALLABLE = {"environment": "test"}
# Behaviour of `toggled_fields`, changed by tests (e.g., with
# `monkeypatch.setitem()`): the fields to return, or an exception to raise
TOGGLE: dict[str, Any] = {"fields": {}, "error": None}


def runtime_fields() -> dict[str, Any]:
    """Return fields computed at runtime, from the app's configuration."""
    return {
        "environment": f"port-{get_config().server.port}",
        "description": "From runtime",
        "x-runtime": {"empty": None},
    }


def version_field() -> dict[str, Any]:
    """Return the version only."""
    return {"version": "2.0.0-runtime"}


def invalid_fields() -> dict[str, Any]:
    """Return an invalid field."""
    return {"createdAt": "yesterday"}


def not_a_dict() -> list[str]:
    """Return no dictionary."""
    return ["environment", "test"]


def toggled_fields() -> Any:
    """Return the fields set in `TOGGLE`, or raise its error, if set."""
    if TOGGLE["error"] is not None:
        raise TOGGLE["error"]
    return TOGGLE["fields"]
