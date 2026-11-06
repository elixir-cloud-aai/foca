"""Tests that FOCA reads requests only via its accessors (`foca.context`)."""

import ast
from pathlib import Path

import pytest

import foca

_PACKAGE = Path(foca.__file__).parent

# The accessors, the only module reading the request of the framework
_ACCESSORS = _PACKAGE / "context.py"

# Modules reading the request via `foca.context` and `foca.security.auth`
_MODULES = sorted(path for path in _PACKAGE.rglob("*.py") if path != _ACCESSORS)

# Functions exempt from the rule, by module: the body-size limit is
# middleware, rewriting the raw input (`wsgi.input`) before Connexion reads it
_EXEMPT = {
    _PACKAGE / "factories" / "connexion_app.py": {"_check_body_size"},
}

# Names giving the request or its context, by module
_FORBIDDEN = {
    "flask": {"request", "request_ctx", "has_request_context", "_request_ctx_stack"},
    "flask.globals": {"request", "request_ctx", "_cv_request", "_request_ctx_stack"},
    "connexion": {"request", "context"},
    "connexion.context": None,
    "connexion.lifecycle": None,
}


def _is_forbidden(module: str, name: str) -> bool:
    """Check whether a name of a module gives the request or its context.

    Args:
        module: Module, e.g., `flask`.
        name: Name in the module, e.g., `request`.

    Returns:
        Whether the name, or the whole module, is forbidden.
    """
    if module not in _FORBIDDEN:
        return False
    names = _FORBIDDEN[module]
    return names is None or name in names


def _resolve(node: ast.expr, modules: dict[str, str]) -> str | None:
    """Get the dotted name of an expression, with module aliases resolved.

    Args:
        node: Expression, e.g., `f.globals` for `import flask as f`.
        modules: Modules by the names they are bound to.

    Returns:
        Dotted name, e.g., `flask.globals`, or `None` if the expression
        isn't a name or attribute of one.
    """
    if isinstance(node, ast.Name):
        return modules.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, modules)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _without_functions(tree: ast.Module, names: set[str]) -> ast.Module:
    """Remove functions from the top level of a module.

    Args:
        tree: Syntax tree of the module.
        names: Names of the functions to remove.

    Returns:
        The tree, without the functions.
    """
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.FunctionDef) and node.name in names)
    ]
    return tree


def _forbidden_uses(path: Path, exempt: set[str] | None = None) -> list[str]:
    """Find uses of the request objects of Flask and Connexion in a module.

    Args:
        path: Path of the module.
        exempt: Names of top-level functions of the module to skip.

    Returns:
        Descriptions of the imports and attribute accesses found.
    """
    tree = _without_functions(
        ast.parse(path.read_text(encoding="utf-8")), exempt or set()
    )
    # Modules by the names they are bound to, e.g., `f` for `import flask as f`
    modules: dict[str, str] = {}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if _is_forbidden(node.module, alias.name):
                    found.append(f"from {node.module} import {alias.name}")
                modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN and _FORBIDDEN[alias.name] is None:
                    found.append(f"import {alias.name}")
                if alias.asname is not None:
                    modules[alias.asname] = alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            base = _resolve(node.value, modules)
            if base is not None and _is_forbidden(base, node.attr):
                found.append(f"{base}.{node.attr}")
    return found


@pytest.mark.parametrize(
    "path", _MODULES, ids=[str(path.relative_to(_PACKAGE)) for path in _MODULES]
)
def test_no_framework_request(path: Path) -> None:
    """Modules don't read the request objects of the framework."""
    assert _forbidden_uses(path, _EXEMPT.get(path)) == []


def test_exempt_functions_exist() -> None:
    """Exempt functions read the request, so exemptions aren't stale."""
    for path, names in _EXEMPT.items():
        assert _forbidden_uses(path) != []
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        assert names <= functions


def test_exempt_function_skipped(tmp_path: Path) -> None:
    """Uses in exempt top-level functions are skipped, others are found."""
    path = tmp_path / "module.py"
    path.write_text(
        "def exempt():\n    from flask import request\n"
        "def other():\n    from flask import request\n",
        encoding="utf-8",
    )
    assert _forbidden_uses(path, {"exempt"}) == ["from flask import request"]


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
        ("import flask as f\nf.request", ["flask.request"]),
        (
            "from flask import globals as fg\nfg.request_ctx",
            ["flask.globals.request_ctx"],
        ),
        (
            "import flask.globals\nflask.globals.request_ctx",
            ["flask.globals.request_ctx"],
        ),
        (
            "import connexion.context as cc\ncc.x",
            ["import connexion.context", "connexion.context.x"],
        ),
        ("from . import x\nx.request", []),
    ],
)
def test_finds_uses(tmp_path: Path, source: str, expected: list[str]) -> None:
    """Imports and attribute accesses of the request objects are found."""
    path = tmp_path / "module.py"
    path.write_text(source, encoding="utf-8")
    assert _forbidden_uses(path) == expected
