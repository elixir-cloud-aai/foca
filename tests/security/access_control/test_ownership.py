"""Tests for resource ownership helpers."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any
from unittest import mock

import mongomock
import pytest
from connexion.exceptions import BadRequestProblem, Forbidden, Unauthorized
from flask import Flask
from flask.globals import request_ctx
from pydantic import ValidationError
from pymongo import MongoClient
from werkzeug.exceptions import Conflict, NotFound

from foca.models.config import AccessControlConfig, Config
from foca.security.access_control import (
    LastOwner,
    ResourceRole,
    SubjectResourceRole,
    add_owner,
    add_resource_role,
    grant_role,
    is_admin,
    list_resource_roles,
    list_roles,
    list_subject_roles,
    ownership,
    remove_resource_role,
    resource_filter,
    revoke_roles,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.enforcement import get_enforcer
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter

ISSUER = "https://login.example.org/oidc/"
HEADER = "X-On-Behalf-Of"
ADMINS = "urn:example:group:admins"
TASK = "/tasks/1"
# Claims of validated tokens
ALICE = {"iss": ISSUER, "sub": "alice", "groups": ["devs"]}
BOB = {"iss": ISSUER, "sub": "bob", "groups": ["ops/team"]}
CAROL = {"iss": ISSUER, "sub": "carol", "groups": [ADMINS]}
DAVE = {"iss": ISSUER, "sub": "dave"}
CONQUEST = {"iss": ISSUER, "sub": "conquest", "client_id": "conquest"}
PROTES = {"iss": ISSUER, "sub": "protes", "client_id": "protes"}
NO_SUBJECT = {"iss": ISSUER}
# Model with resource-scoped roles only
MODEL_WITHOUT_GLOBAL_ROLES = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[role_definition]
g2 = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g2(r.sub, p.sub, r.obj) && keyMatch2(r.obj, p.obj) && r.act == p.act
"""


def _model_path(model_file: str = RESOURCE_ROLE_MODEL_FILE) -> str:
    if Path(model_file).is_absolute():
        return model_file
    with resource_path(ACCESS_CONTROL_BASE_PATH, model_file) as model:
        return str(model)


def _create_app(
    model_file: str = RESOURCE_ROLE_MODEL_FILE, **access_control: Any
) -> Flask:
    """Create app with service clients, groups and in-memory policies."""
    app = Flask(__name__)
    app.config.foca = Config.model_validate(  # type: ignore[attr-defined]  # dynamic attribute
        {
            "security": {
                "auth": {
                    "issuers": [ISSUER],
                    "audience": ["foca"],
                    "service_clients": [
                        {"client_id": "conquest", "on_behalf_of": True},
                        {"client_id": "protes"},
                    ],
                    "on_behalf_of_header": HEADER,
                },
                "access_control": {
                    "enabled": True,
                    "model": _model_path(model_file),
                    "claim_groups": "groups",
                    "group_mapping": {ADMINS: "admin"},
                    "keep_group_subjects": True,
                    **access_control,
                },
            }
        }
    )
    app.config["CASBIN_MODEL"] = _model_path(model_file)
    app.config["casbin_adapter"] = Adapter(
        collection=mongomock.MongoClient()["access_control_db"]["policy_rules"]
    )
    return app


@pytest.fixture
def app() -> Flask:
    """App with service clients, groups and in-memory policies."""
    return _create_app()


@pytest.fixture(
    params=["mongomock", pytest.param("mongodb", marks=pytest.mark.mongodb)]
)
def store_app(request: pytest.FixtureRequest, app: Flask) -> Iterator[Flask]:
    """`app`, with policies in memory or in a MongoDB server.

    The MongoDB server runs on `localhost:12345` (tests marked `mongodb`).
    """
    if request.param == "mongomock":
        yield app
        return
    db_name = "foca_test_ownership"
    client: MongoClient[dict[str, Any]] = MongoClient(
        "mongodb://localhost:12345", serverSelectionTimeoutMS=5000
    )
    client.drop_database(db_name)
    app.config["casbin_adapter"] = Adapter(collection=client[db_name]["policy_rules"])
    yield app
    client.drop_database(db_name)
    client.close()


@contextmanager
def as_caller(
    app: Flask,
    claims: dict[str, Any] | None,
    headers: dict[str, str] | None = None,
) -> Iterator[None]:
    """Request context with token info as set by Connexion."""
    with app.test_request_context("/tasks", headers=headers):
        request_ctx.connexion_context = (  # type: ignore[attr-defined]  # set by Connexion
            {} if claims is None else {"token_info": {"claims": claims}}
        )
        yield


def _audit_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == ownership.__name__ and record.levelno == logging.INFO
    ]


class TestConfig:
    """Tests for the `resource_roles` option."""

    def test_default(self) -> None:
        """Owners and maintainers by default."""
        assert AccessControlConfig().resource_roles == ["owner", "maintainer"]

    def test_custom(self) -> None:
        """Other roles may be added."""
        conf = AccessControlConfig(resource_roles=["viewer", "owner"])
        assert conf.resource_roles == ["viewer", "owner"]

    @pytest.mark.parametrize(
        "roles,message",
        [
            (["maintainer"], "must contain 'owner'"),
            (["owner", "owner"], "must not contain duplicates"),
            (["owner", "admin"], "must not contain 'admin'"),
            (["owner", "user:x"], "must not start with 'user:'"),
            (["owner", "group:x"], "must not start with 'group:'"),
            (["owner", "client:x"], "must not start with 'client:'"),
            (["owner", "a,b"], "must not contain commas"),
            (["owner", ""], "must be non-empty"),
            (["owner", "#x"], "must not start with '#'"),
        ],
    )
    def test_invalid(self, roles: list[str], message: str) -> None:
        """Roles must include owners and be valid in rules."""
        with pytest.raises(ValidationError, match=message):
            AccessControlConfig(resource_roles=roles)

    def test_custom_subject_prefix(self) -> None:
        """Roles must not start with the configured subject prefix."""
        with pytest.raises(ValidationError, match="must not start with 'u-'"):
            AccessControlConfig(subject_prefix="u-", resource_roles=["owner", "u-x"])


class TestIsAdmin:
    """Tests for `is_admin()`."""

    @pytest.mark.parametrize(
        "claims,expected",
        [(CAROL, True), (ALICE, False), (DAVE, False), (PROTES, False), (None, False)],
    )
    def test_group_mapping(
        self, app: Flask, claims: dict[str, Any] | None, expected: bool
    ) -> None:
        """Admins are mapped from group claims."""
        with as_caller(app, claims):
            assert is_admin() is expected

    def test_g_rule(self, app: Flask) -> None:
        """Users may be made admins via `g` rules."""
        with app.app_context():
            get_enforcer().add_grouping_policy("user:dave", "admin")
        with as_caller(app, DAVE):
            assert is_admin()

    def test_client_mapping(self) -> None:
        """Service clients may be mapped to admins."""
        app = _create_app(client_mapping={"protes": "admin"})
        with as_caller(app, PROTES):
            assert is_admin()

    def test_transitive(self, app: Flask) -> None:
        """Roles inheriting the admin role are admins."""
        with app.app_context():
            get_enforcer().add_grouping_policy("superusers", "admin")
            get_enforcer().add_grouping_policy("group:devs", "superusers")
        with as_caller(app, ALICE):
            assert is_admin()

    def test_model_without_global_roles(self, tmp_path: Path) -> None:
        """Without global roles (`g`), only mapped admins are admins."""
        model = tmp_path / "model.conf"
        model.write_text(MODEL_WITHOUT_GLOBAL_ROLES)
        app = _create_app(model_file=str(model))
        with as_caller(app, ALICE):
            assert not is_admin()
        with as_caller(app, CAROL):
            assert is_admin()


class TestAddOwner:
    """Tests for `add_owner()`."""

    def test_creator(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """The creating user becomes owner."""
        caplog.set_level(logging.INFO)
        with as_caller(app, ALICE):
            assert add_owner(TASK) == ResourceRole("user", "alice", "owner")
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]
        assert _audit_logs(caplog) == [
            "Role 'owner' on '/tasks/1' granted to user 'alice' by user 'alice'"
        ]

    def test_on_behalf_of_user(self, app: Flask) -> None:
        """Resources created on behalf of a user belong to that user."""
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            assert add_owner(TASK) == ResourceRole("user", "alice", "owner")
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]

    def test_service_client(self, app: Flask) -> None:
        """Resources created by a service client for itself belong to it."""
        with as_caller(app, PROTES):
            assert add_owner(TASK) == ResourceRole("client", "protes", "owner")

    def test_on_behalf_of_user_from_field(self, app: Flask) -> None:
        """Users named in request fields are checked like the header."""
        with as_caller(app, CONQUEST):
            owner = add_owner(TASK, on_behalf_of="alice")
            assert owner == ResourceRole("user", "alice", "owner")
        with as_caller(app, PROTES), pytest.raises(Forbidden):
            add_owner("/tasks/2", on_behalf_of="alice")

    def test_subject_and_on_behalf_of(self, app: Flask) -> None:
        """Owners are either given or named by the caller."""
        with as_caller(app, CONQUEST), pytest.raises(ValueError, match="both"):
            add_owner(TASK, subject="bob", on_behalf_of="alice")

    def test_on_behalf_of_user_overrides_header(self, app: Flask) -> None:
        """Users named in request fields take precedence over the header."""
        with as_caller(app, CONQUEST, headers={HEADER: "bob"}):
            assert add_owner(TASK, on_behalf_of="alice").subject == "alice"

    def test_not_allowed_on_behalf_of(self, app: Flask) -> None:
        """Other callers cannot name users to act for."""
        with as_caller(app, PROTES, headers={HEADER: "alice"}):
            with pytest.raises(Forbidden):
                add_owner(TASK)
            assert list_roles(TASK) == []

    def test_given_subject(self, app: Flask) -> None:
        """Owners may be given, e.g., groups."""
        with as_caller(app, ALICE):
            owner = add_owner(TASK, subject="devs", subject_type="group")
            assert owner == ResourceRole("group", "devs", "owner")

    def test_without_caller(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Owners may be given outside of requests by subjects."""
        caplog.set_level(logging.INFO)
        with as_caller(app, None):
            add_owner(TASK, subject="alice")
        assert _audit_logs(caplog) == [
            "Role 'owner' on '/tasks/1' granted to user 'alice' by unidentified caller"
        ]

    def test_no_subject(self, app: Flask) -> None:
        """Requests without subject cannot own resources."""
        with as_caller(app, NO_SUBJECT), pytest.raises(Unauthorized):
            add_owner(TASK)

    def test_invalid_subject(self, app: Flask) -> None:
        """Subjects that cannot be stored in rules are rejected."""
        with as_caller(app, {"sub": "a,b"}), pytest.raises(ValueError):
            add_owner(TASK)


class TestListRoles:
    """Tests for `list_roles()`."""

    def test_order_and_filter(self, app: Flask) -> None:
        """Users first, then groups and clients; optionally by role."""
        with as_caller(app, CAROL):
            add_resource_role("devs", "maintainer", TASK, subject_type="group")
            add_resource_role("protes", "owner", TASK, subject_type="client")
            add_resource_role("bob", "maintainer", TASK)
            add_resource_role("alice", "owner", TASK)
            add_resource_role("alice", "owner", "/tasks/2")
            assert list_roles(TASK) == [
                ResourceRole("user", "bob", "maintainer"),
                ResourceRole("user", "alice", "owner"),
                ResourceRole("group", "devs", "maintainer"),
                ResourceRole("client", "protes", "owner"),
            ]
            assert list_roles(TASK, role="owner") == [
                ResourceRole("user", "alice", "owner"),
                ResourceRole("client", "protes", "owner"),
            ]

    def test_duplicates(self, app: Flask) -> None:
        """Rules stored more than once are listed once."""
        adapter = app.config["casbin_adapter"]
        for _ in range(2):
            adapter.save_policy_line("g2", ["user:alice", "owner", TASK])
            adapter.save_policy_line("g2", ["group:devs", "owner", TASK])
        with as_caller(app, ALICE):
            assert list_roles(TASK) == [
                ResourceRole("user", "alice", "owner"),
                ResourceRole("group", "devs", "owner"),
            ]
            assert [
                (role.subject, role.role)
                for role in list_roles(TASK)
                if role.subject_type == "user"
            ] == list_resource_roles(TASK, subject_type="user")

    def test_other_rules_ignored(self, app: Flask) -> None:
        """Rules of subjects of other types are not listed."""
        with as_caller(app, CAROL):
            get_enforcer().add_named_grouping_policy("g2", "admin", "owner", TASK)
            assert list_roles(TASK) == []


class TestGrantRole:
    """Tests for `grant_role()`."""

    def test_grant_and_replace(
        self, store_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Roles are granted, replacing other roles of the subject."""
        caplog.set_level(logging.INFO)
        with as_caller(store_app, ALICE):
            add_owner(TASK)
            assert grant_role(TASK, "bob", "owner") == []
            assert grant_role(TASK, "bob", "maintainer") == ["owner"]
            assert grant_role(TASK, "bob", "maintainer") == ["maintainer"]
            assert list_roles(TASK) == [
                ResourceRole("user", "alice", "owner"),
                ResourceRole("user", "bob", "maintainer"),
            ]
        assert _audit_logs(caplog)[1:] == [
            "Role 'owner' on '/tasks/1' granted to user 'bob' by user 'alice' "
            "(previous roles: none)",
            "Role 'maintainer' on '/tasks/1' granted to user 'bob' by user "
            "'alice' (previous roles: owner)",
            "Role 'maintainer' on '/tasks/1' granted to user 'bob' by user "
            "'alice' (previous roles: maintainer)",
        ]

    def test_other_roles_kept(self, app: Flask) -> None:
        """Roles not managed by the helpers are kept."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            add_resource_role("bob", "auditor", TASK)
            grant_role(TASK, "bob", "maintainer")
            assert ("bob", "auditor") in list_resource_roles(TASK, subject_type="user")

    def test_groups_and_clients(self, app: Flask) -> None:
        """Roles are granted to groups and service clients."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            grant_role(TASK, "ops/team", "owner", subject_type="group")
            grant_role(TASK, "protes", "maintainer", subject_type="client")
            assert list_roles(TASK) == [
                ResourceRole("user", "alice", "owner"),
                ResourceRole("group", "ops/team", "owner"),
                ResourceRole("client", "protes", "maintainer"),
            ]

    def test_group_roles_not_applying(self) -> None:
        """Roles are not granted to groups if they would not apply."""
        app = _create_app(keep_group_subjects=False)
        with as_caller(app, ALICE):
            add_owner(TASK)
            with pytest.raises(ValueError, match="keep_group_subjects"):
                grant_role(TASK, "devs", "owner", subject_type="group")
            with pytest.raises(ValueError, match="keep_group_subjects"):
                add_owner("/tasks/2", subject="devs", subject_type="group")
            # so that the last owner that applies is kept
            with pytest.raises(LastOwner):
                revoke_roles(TASK, "alice")
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]

    def test_owners_not_applying(self) -> None:
        """Owners whose role does not apply do not count."""
        app = _create_app(keep_group_subjects=False)
        with as_caller(app, ALICE):
            add_owner(TASK)
            add_resource_role("devs", "owner", TASK, subject_type="group")
            add_resource_role("krini", "owner", TASK, subject_type="client")
            with pytest.raises(LastOwner):
                revoke_roles(TASK, "alice")
            with pytest.raises(LastOwner):
                grant_role(TASK, "alice", "maintainer")
            # owners whose role does not apply may be removed
            assert revoke_roles(TASK, "devs", subject_type="group")

    def test_unknown_client(self, app: Flask) -> None:
        """Roles are only granted to service clients."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            with pytest.raises(ValueError, match="not a service client"):
                grant_role(TASK, "krini", "owner", subject_type="client")

    def test_unknown_role(self, app: Flask) -> None:
        """Only configured roles are granted."""
        with as_caller(app, ALICE), pytest.raises(ValueError, match="admin"):
            grant_role(TASK, "bob", "admin")

    def test_custom_roles(self) -> None:
        """Configured roles are granted and replaced."""
        app = _create_app(resource_roles=["owner", "viewer"])
        with as_caller(app, ALICE):
            add_owner(TASK)
            grant_role(TASK, "bob", "viewer")
            with pytest.raises(ValueError, match="maintainer"):
                grant_role(TASK, "bob", "maintainer")
            assert grant_role(TASK, "bob", "owner") == ["viewer"]

    def test_last_owner(self, store_app: Flask) -> None:
        """The last owner cannot be downgraded."""
        with as_caller(store_app, ALICE):
            add_owner(TASK)
            with pytest.raises(LastOwner) as exc_info:
                grant_role(TASK, "alice", "maintainer")
            assert isinstance(exc_info.value, Conflict)
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]
            # with another owner, also a group
            grant_role(TASK, "devs", "owner", subject_type="group")
            grant_role(TASK, "alice", "maintainer")
            with pytest.raises(LastOwner):
                grant_role(TASK, "devs", "maintainer", subject_type="group")
            # granting owner to the last owner is fine
            assert grant_role(TASK, "devs", "owner", subject_type="group") == ["owner"]

    def test_concurrent_downgrade(self, store_app: Flask) -> None:
        """Owners removed concurrently are restored."""
        # Races are simulated by skipping the check for other owners, as a
        # concurrent request removing them after the check would
        with as_caller(store_app, ALICE):
            add_owner(TASK)
            grant_role(TASK, "bob", "owner")

            # a concurrent request downgrades bob after alice's check
            with (
                mock.patch.object(ownership, "_check_other_owners"),
                pytest.raises(LastOwner),
            ):
                remove_resource_role("bob", "owner", TASK)
                grant_role(TASK, "alice", "maintainer")
            assert list_roles(TASK, role="owner") == [
                ResourceRole("user", "alice", "owner")
            ]

    def test_resource_gone(self, app: Flask) -> None:
        """Roles granted on resources deleted meanwhile are revoked."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            with pytest.raises(NotFound):
                grant_role(TASK, "bob", "maintainer", exists=lambda: False)
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]
            grant_role(TASK, "bob", "maintainer", exists=lambda: True)
            assert ResourceRole("user", "bob", "maintainer") in list_roles(TASK)

    def test_existence_check_fails(self, app: Flask) -> None:
        """Roles are revoked again if checking the resource fails."""

        def fail() -> bool:
            raise RuntimeError("database not available")

        with as_caller(app, ALICE):
            add_owner(TASK)
            with pytest.raises(RuntimeError, match="database"):
                grant_role(TASK, "bob", "maintainer", exists=fail)
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]

    def test_no_owner(self, app: Flask) -> None:
        """Resources without owners only get owners."""
        with as_caller(app, CAROL):
            add_resource_role("bob", "maintainer", TASK)
            with pytest.raises(ValueError, match="no owner"):
                grant_role(TASK, "dave", "maintainer")
            assert grant_role(TASK, "bob", "owner") == ["maintainer"]

    def test_restored_owner_resource_gone(self, app: Flask) -> None:
        """Owners are not restored on resources deleted meanwhile."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            with (
                mock.patch.object(ownership, "_check_other_owners"),
                pytest.raises(NotFound),
            ):
                grant_role(TASK, "alice", "maintainer", exists=lambda: False)
            assert list_roles(TASK) == []

    def test_invalid_subject(self, app: Flask) -> None:
        """Subjects that cannot be stored in rules are rejected."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            with pytest.raises(ValueError):
                grant_role(TASK, "a,b", "maintainer")
            with pytest.raises(ValueError, match="empty"):
                grant_role(TASK, "", "maintainer")


class TestRevokeRoles:
    """Tests for `revoke_roles()`."""

    def test_revoke(self, store_app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """All managed roles of a subject are revoked."""
        caplog.set_level(logging.INFO)
        with as_caller(store_app, ALICE):
            add_owner(TASK)
            grant_role(TASK, "devs", "maintainer", subject_type="group")
            add_resource_role("devs", "auditor", TASK, subject_type="group")
            assert revoke_roles(TASK, "devs", subject_type="group")
            assert not revoke_roles(TASK, "devs", subject_type="group")
            # roles not managed by the helpers are kept
            assert list_roles(TASK) == [
                ResourceRole("user", "alice", "owner"),
                ResourceRole("group", "devs", "auditor"),
            ]
        assert _audit_logs(caplog)[-1] == (
            "Roles on '/tasks/1' of group 'devs' revoked by user 'alice' "
            "(previous roles: maintainer)"
        )

    def test_no_roles(self, app: Flask) -> None:
        """Subjects without roles, or invalid ones, have nothing to revoke."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            assert not revoke_roles(TASK, "bob")
            assert not revoke_roles(TASK, "a,b")

    def test_last_owner(self, store_app: Flask) -> None:
        """The last owner cannot be removed."""
        with as_caller(store_app, ALICE):
            add_owner(TASK)
            with pytest.raises(LastOwner):
                revoke_roles(TASK, "alice")
            grant_role(TASK, "protes", "owner", subject_type="client")
            assert revoke_roles(TASK, "alice")
            with pytest.raises(LastOwner):
                revoke_roles(TASK, "protes", subject_type="client")

    def test_restored_owner_resource_gone(self, app: Flask) -> None:
        """Owners are not restored on resources deleted meanwhile."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            with (
                mock.patch.object(ownership, "_check_other_owners"),
                pytest.raises(NotFound),
            ):
                revoke_roles(TASK, "alice", exists=lambda: False)
            assert list_roles(TASK) == []

    def test_concurrent_removal(self, app: Flask) -> None:
        """Owners removed concurrently are restored."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            grant_role(TASK, "bob", "owner")
            with (
                mock.patch.object(ownership, "_check_other_owners"),
                pytest.raises(LastOwner),
            ):
                remove_resource_role("bob", "owner", TASK)
                revoke_roles(TASK, "alice")
            assert list_roles(TASK) == [ResourceRole("user", "alice", "owner")]


class TestListSubjectRoles:
    """Tests for `list_subject_roles()`."""

    def test_user_and_groups(self, app: Flask) -> None:
        """Roles of the user and their groups under a prefix are listed."""
        with as_caller(app, ALICE):
            add_resource_role("alice", "owner", "/tasks/2")
            add_resource_role("alice", "maintainer", "/tasks/1")
            add_resource_role("devs", "owner", "/tasks/1", subject_type="group")
            add_resource_role("alice", "owner", "/tasks/1/logs")
            add_resource_role("alice", "owner", "/services/1")
            add_resource_role("bob", "owner", "/tasks/3")
            assert list_subject_roles("/tasks/") == [
                SubjectResourceRole("1", "maintainer", "user", "alice"),
                SubjectResourceRole("1", "owner", "group", "devs"),
                SubjectResourceRole("2", "owner", "user", "alice"),
            ]
            assert list_subject_roles("/tasks") == list_subject_roles("/tasks/")

    def test_client(self, app: Flask) -> None:
        """Roles of service clients are listed."""
        with as_caller(app, PROTES):
            add_owner(TASK)
            assert list_subject_roles("/tasks") == [
                SubjectResourceRole("1", "owner", "client", "protes")
            ]

    def test_on_behalf_of_user(self, app: Flask) -> None:
        """Only direct roles of users that service clients act for are listed."""
        with as_caller(app, ALICE):
            add_resource_role("alice", "owner", "/tasks/2")
            add_resource_role("devs", "owner", TASK, subject_type="group")
            add_resource_role("conquest", "owner", "/tasks/3", subject_type="client")
        expected = [SubjectResourceRole("2", "owner", "user", "alice")]
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            assert list_subject_roles("/tasks") == expected
        with as_caller(app, CONQUEST, headers={HEADER: "bob"}):
            assert list_subject_roles("/tasks", on_behalf_of="alice") == expected
        with as_caller(app, CONQUEST):
            assert list_subject_roles("/tasks") == [
                SubjectResourceRole("3", "owner", "client", "conquest")
            ]

    def test_not_allowed_on_behalf_of(self, app: Flask) -> None:
        """Other callers cannot name users to list roles for."""
        with as_caller(app, ALICE, headers={HEADER: "bob"}), pytest.raises(Forbidden):
            list_subject_roles("/tasks")
        with (
            as_caller(app, CONQUEST, headers={HEADER: ""}),
            pytest.raises(BadRequestProblem),
        ):
            list_subject_roles("/tasks")

    def test_group_subjects_not_kept(self) -> None:
        """Roles of groups only apply with group subjects."""
        app = _create_app(keep_group_subjects=False)
        with as_caller(app, ALICE):
            add_resource_role("devs", "owner", TASK, subject_type="group")
            assert list_subject_roles("/tasks") == []


class TestResourceFilter:
    """Tests for `resource_filter()`."""

    def test_filter(self, store_app: Flask) -> None:
        """Users see the resources they or their groups have roles on."""
        with as_caller(store_app, ALICE):
            add_resource_role("alice", "owner", "/tasks/2")
            add_resource_role("devs", "maintainer", TASK, subject_type="group")
            add_resource_role("devs", "owner", "/tasks/2", subject_type="group")
            add_resource_role("bob", "owner", "/tasks/3")
            assert resource_filter("/tasks") == {"id": {"$in": ["1", "2"]}}
            assert resource_filter("/tasks", field="task_id") == {
                "task_id": {"$in": ["1", "2"]}
            }
        with as_caller(store_app, DAVE):
            assert resource_filter("/tasks") == {"id": {"$in": []}}

    def test_filter_by_mongodb(self, app: Flask) -> None:
        """The filter selects the documents of visible resources."""
        collection: Any = mongomock.MongoClient()["db"]["tasks"]
        collection.insert_many([{"id": str(i)} for i in range(1, 4)])
        with as_caller(app, ALICE):
            add_owner(TASK)
            ids = [doc["id"] for doc in collection.find(resource_filter("/tasks"))]
        assert ids == ["1"]

    def test_admin(self, app: Flask) -> None:
        """Admins see everything."""
        with as_caller(app, CAROL):
            assert resource_filter("/tasks") == {}

    def test_service_client(self, app: Flask) -> None:
        """Service clients see the resources they have roles on.

        Not those of users they created resources for, if not acting for them.
        """
        with as_caller(app, CONQUEST):
            add_owner(TASK, on_behalf_of="alice")
            add_resource_role("conquest", "owner", "/tasks/2", subject_type="client")
            assert resource_filter("/tasks") == {"id": {"$in": ["2"]}}

    def test_on_behalf_of_user(self, app: Flask) -> None:
        """Service clients acting for users see the resources of the users."""
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            add_owner(TASK)
            add_resource_role("conquest", "owner", "/tasks/2", subject_type="client")
            assert resource_filter("/tasks") == {"id": {"$in": ["1"]}}
        with as_caller(app, CONQUEST):
            assert resource_filter("/tasks", on_behalf_of="alice") == {
                "id": {"$in": ["1"]}
            }

    def test_admin_client_on_behalf_of_user(self) -> None:
        """Admin clients see everything, also when acting for users."""
        app = _create_app(client_mapping={"conquest": "admin"})
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            assert resource_filter("/tasks") == {}
        # Named users are checked also for admins
        with as_caller(app, CONQUEST), pytest.raises(BadRequestProblem):
            resource_filter("/tasks", on_behalf_of=" alice")

    def test_on_behalf_of_admin(self, app: Flask) -> None:
        """Users' admin rights do not apply to clients acting for them."""
        # Admin via a `g` rule, as service tokens carry no groups of users
        with app.app_context():
            get_enforcer().add_grouping_policy("user:dave", "admin")
        with as_caller(app, DAVE):
            assert resource_filter("/tasks") == {}
        with as_caller(app, CONQUEST, headers={HEADER: "dave"}):
            add_resource_role("dave", "owner", TASK)
            assert resource_filter("/tasks") == {"id": {"$in": ["1"]}}

    def test_invalid_on_behalf_of(self, app: Flask) -> None:
        """Invalid users named by service clients are rejected."""
        with as_caller(app, CONQUEST), pytest.raises(BadRequestProblem):
            resource_filter("/tasks", on_behalf_of=" alice")

    def test_not_allowed_on_behalf_of(self, app: Flask) -> None:
        """Other callers cannot name users to list resources for."""
        with (
            as_caller(app, PROTES, headers={HEADER: "alice"}),
            pytest.raises(Forbidden),
        ):
            resource_filter("/tasks")
        with as_caller(app, CAROL), pytest.raises(Forbidden):
            resource_filter("/tasks", on_behalf_of="alice")


def test_default_model() -> None:
    """Helpers need a model with resource-scoped roles."""
    app = _create_app(model_file=DEFAULT_MODEL_FILE)
    with as_caller(app, ALICE), pytest.raises(ValueError, match="resource-scoped"):
        add_owner(TASK)
