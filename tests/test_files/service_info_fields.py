"""Functions returning service info fields at runtime, for tests."""

from foca import get_config

NOT_CALLABLE = {"environment": "test"}


def runtime_fields() -> dict:
    """Return fields computed at runtime, from the app's configuration."""
    return {
        "environment": f"port-{get_config().server.port}",
        "description": "From runtime",
        "x-runtime": {"empty": None},
    }


def version_field() -> dict:
    """Return the version only."""
    return {"version": "2.0.0-runtime"}


def invalid_fields() -> dict:
    """Return an invalid field."""
    return {"createdAt": "yesterday"}


def not_a_dict() -> list:
    """Return no dictionary."""
    return ["environment", "test"]
