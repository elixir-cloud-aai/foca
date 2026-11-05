"""Tests that FOCA's security code reads requests only via FOCA's accessors."""

import ast
from pathlib import Path

import pytest

import foca

_PACKAGE = Path(foca.__file__).parent

# Modules reading the request via `foca.context` and `foca.security.auth`
_MODULES = sorted(
    [
        *(_PACKAGE / "security").rglob("*.py"),
        _PACKAGE / "plugins" / "adapter.py",
    ]
)

# Names giving the request or its context, by module
_FORBIDDEN = {
    "flask": {"request", "request_ctx", "has_request_context", "_request_ctx_stack"},
    "flask.globals": {"request", "request_ctx", "_cv_request", "_request_ctx_stack"},
    "connexion": {"request", "context"},
    "connexion.context": None,
    "connexion.lifecycle": None,
}


def _forbidden_uses(path: Path) -> list[str]:
    """Find uses of the request objects of Flask and Connexion in a module.

    Args:
        path: Path of the module.

    Returns:
        Descriptions of the imports and attribute accesses found.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module in _FORBIDDEN:
            names = _FORBIDDEN[node.module]
            found.extend(
                f"from {node.module} import {alias.name}"
                for alias in node.names
                if names is None or alias.name in names
            )
        elif isinstance(node, ast.Import):
            found.extend(
                f"import {alias.name}"
                for alias in node.names
                if alias.name in _FORBIDDEN and _FORBIDDEN[alias.name] is None
            )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _FORBIDDEN
            and node.attr in (_FORBIDDEN[node.value.id] or ())
        ):
            found.append(f"{node.value.id}.{node.attr}")
    return found


@pytest.mark.parametrize(
    "path", _MODULES, ids=[str(path.relative_to(_PACKAGE)) for path in _MODULES]
)
def test_no_framework_request(path: Path) -> None:
    """Security modules don't read the request objects of the framework."""
    assert _forbidden_uses(path) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from flask import g, request", ["from flask import request"]),
        (
            "from flask.globals import request_ctx",
            ["from flask.globals import request_ctx"],
        ),
        ("from connexion.context import x", ["from connexion.context import x"]),
        ("import connexion.context", ["import connexion.context"]),
        ("import flask\nflask.request.path", ["flask.request"]),
        ("import connexion\nconnexion.request", ["connexion.request"]),
        ("from flask import g, current_app\nimport flask\nflask.g", []),
    ],
)
def test_finds_uses(tmp_path: Path, source: str, expected: list[str]) -> None:
    """Imports and attribute accesses of the request objects are found."""
    path = tmp_path / "module.py"
    path.write_text(source, encoding="utf-8")
    assert _forbidden_uses(path) == expected
