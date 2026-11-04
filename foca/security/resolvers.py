"""Helpers for Connexion resolvers that wrap other resolvers."""

from collections.abc import Callable
from typing import Any

from connexion.resolver import Resolver


def _normalize_resolver(resolver: Resolver | Callable[..., Any] | None) -> Resolver:
    """Get the resolver to resolve functions with.

    Same normalization as Connexion's `App.add_api()`.

    Args:
        resolver: Resolver (any object with a `resolve()` method), or a
            function resolving operation identifiers to functions, as
            accepted by Connexion.

    Returns:
        Connexion's default resolver if `resolver` is `None`, a resolver with
        the function if it is callable, and any other object as it is.
    """
    if resolver is None:
        return Resolver()
    if callable(resolver):
        return Resolver(function_resolver=resolver)
    return resolver
