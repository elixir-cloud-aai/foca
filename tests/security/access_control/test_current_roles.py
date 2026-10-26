"""Tests of `get_current_roles()`, unmocked."""

import itertools

from flask import Flask

from foca.security.access_control import get_current_roles
from foca.security.access_control.constants import DEFAULT_MODEL_FILE
from foca.security.access_control.enforcement import locked_enforcer
from tests.security.access_control.callers import (
    ADMINS,
    ALICE,
    CAROL,
    DAVE,
    NO_SUBJECT,
    PROTES,
    as_caller,
    create_app,
)


def _add_rules(app: Flask, *rules: tuple[str, ...], ptype: str = "g") -> None:
    """Add role rules to the policies of an app.

    Args:
        app: App.
        rules: Subjects and roles (and domains, if any).
        ptype: Policy type of the rules.
    """
    with app.app_context(), locked_enforcer() as enforcer:
        for rule in rules:
            enforcer.add_named_grouping_policy(ptype, *rule)


def test_client_and_group_roles() -> None:
    """Service clients get their mapped role, users their group roles."""
    app = create_app(client_mapping={"protes": "admin"})
    with as_caller(app, PROTES):
        assert get_current_roles() == ["admin"]
    with as_caller(app, CAROL):
        assert get_current_roles() == ["admin", f"group:{ADMINS}"]
    with as_caller(app, None):
        assert get_current_roles() == []
    # unidentified callers have no roles, as for `is_admin()`
    _add_rules(app, (f"group:{ADMINS}", "operator"))
    with as_caller(app, {**NO_SUBJECT, "groups": [ADMINS]}):
        assert get_current_roles() == []


def test_global_roles() -> None:
    """Global role rules grant roles, transitively, without duplicates."""
    app = create_app()
    _add_rules(
        app,
        ("user:alice", "editor"),
        ("group:devs", "editor"),
        ("editor", "reviewer"),
        ("reviewer", "user:alice"),  # back to the identity: no role
    )
    with as_caller(app, ALICE):
        assert get_current_roles() == ["group:devs", "editor", "reviewer"]
    with as_caller(app, DAVE):
        assert get_current_roles() == []


def test_hierarchy_limit() -> None:
    """Roles beyond Casbin's maximum hierarchy level don't apply."""
    app = create_app()
    chain = ["user:dave", *(f"r{i}" for i in range(14))]
    _add_rules(app, *itertools.pairwise(chain))
    with as_caller(app, DAVE):
        roles = get_current_roles()
    assert roles[0] == "r0"
    assert "r13" not in roles


def test_other_role_definitions() -> None:
    """Only global roles (`g`) count, not other role definitions."""
    app = create_app(model_file=DEFAULT_MODEL_FILE)
    _add_rules(app, ("group:devs", "editor"))
    _add_rules(app, ("editor", "secret_data"), ptype="g2")
    with as_caller(app, ALICE):
        assert get_current_roles() == ["group:devs", "editor"]
    app = create_app()
    _add_rules(app, ("user:alice", "admin", "/tasks/1"), ptype="g2")
    with as_caller(app, ALICE):
        assert get_current_roles() == ["group:devs"]


def test_opt_in_roles() -> None:
    """Opt-in roles, and roles they lead to, apply only if asked for."""
    app = create_app(opt_in_roles=["admin"])
    _add_rules(app, ("admin", "auditor"))
    with as_caller(app, CAROL):
        assert get_current_roles() == [f"group:{ADMINS}"]
    with as_caller(app, CAROL, headers={"X-Request-Roles": "admin"}):
        assert get_current_roles() == ["admin", f"group:{ADMINS}", "auditor"]


def test_without_access_control() -> None:
    """Without access control, no rules apply."""
    app = create_app()
    _add_rules(app, ("group:devs", "editor"))
    app.config.foca.security.access_control.enabled = False  # type: ignore[attr-defined]  # dynamic attribute
    with as_caller(app, ALICE):
        assert get_current_roles() == ["group:devs"]
