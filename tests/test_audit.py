"""Tests for the audit logger and the operations that use it."""

import json
import logging
from copy import deepcopy
from inspect import unwrap
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import mongomock
import pytest
import yaml
from flask import Flask
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from werkzeug.exceptions import BadRequest, Conflict, NotFound

from foca import audit, context
from foca.config.access import set_app_config
from foca.context import RequestIdMiddleware
from foca.models.config import AccessControlConfig, Config, LogConfig, MongoConfig
from foca.security.access_control import ownership
from foca.security.access_control.access_control_server import (
    deletePermission,
    postPermission,
    putPermission,
)
from foca.security.access_control.enforcement import (
    _replace_resource_role,
    add_resource_role,
    remove_resource,
    remove_resource_role,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.security.access_control.ownership import (
    LastOwner,
    add_owner,
    grant_role,
    revoke_roles,
)
from foca.utils.logging import JsonFormatter
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_ID,
    MOCK_PERMISSION,
    MOCK_RULE,
    MONGO_CONFIG,
)
from tests.security.access_control.callers import (
    ALICE,
    CONQUEST,
    HEADER,
    PROTES,
    TASK,
    as_caller,
    create_app,
)

NO_ACTOR = {"subject": None, "client": None, "on_behalf_of": None}


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Get the audit events logged so far.

    Args:
        caplog: Log capturing fixture.

    Returns:
        Fields of the events.
    """
    return [
        record.audit  # type: ignore[attr-defined]  # added via `extra`
        for record in caplog.records
        if record.name == "foca.audit"
    ]


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Get the only message of the audit logger.

    Args:
        caplog: Log capturing fixture.

    Returns:
        Message.
    """
    messages = [
        record.getMessage() for record in caplog.records if record.name == "foca.audit"
    ]
    assert len(messages) == 1
    return messages[0]


def _parse_text(message: str) -> dict[str, Any]:
    """Parse the text of an event, as a consumer of the log would.

    Args:
        message: Message of the event.

    Returns:
        Names and values of the fields.
    """
    decoder = json.JSONDecoder()
    fields: dict[str, Any] = {}
    position = 0
    while position < len(message):
        name, _, _ = message[position:].partition("=")
        position += len(name) + 1
        fields[name], position = decoder.raw_decode(message, position)
        position += 1
    return fields


@pytest.fixture(autouse=True)
def _capture_info(caplog: pytest.LogCaptureFixture) -> None:
    """Capture the audit logger from level `INFO`."""
    caplog.set_level(logging.INFO, logger="foca.audit")


@pytest.fixture
def collection() -> Collection[dict[str, Any]]:
    """In-memory policy rule collection."""
    return mongomock.MongoClient().db.policy_rules


@pytest.fixture
def permissions_app(collection: Collection[dict[str, Any]]) -> Flask:
    """App for the permissions API, with an in-memory collection."""
    app = Flask(__name__)
    access_control = AccessControlConfig.model_validate(ACCESS_CONTROL_CONFIG)
    config = Config(db=MongoConfig.model_validate(MONGO_CONFIG))
    config.security.access_control = access_control
    config.db.dbs[access_control.db_name].collections[  # type: ignore[union-attr,index]  # set in the config
        access_control.collection_name
    ].client = collection
    set_app_config(app, config)
    app.config["CASBIN_MODEL"] = access_control.model
    app.config["casbin_adapter"] = Adapter(collection=collection)
    return app


@pytest.fixture
def app() -> Flask:
    """App with service clients and in-memory policies."""
    return create_app()


class TestLog:
    """Tests for `log()`."""

    def test_logger_and_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """Events go to the logger `foca.audit`, on level `INFO`."""
        audit.log("thing.create", "/things/1")
        record = caplog.records[-1]
        assert record.name == "foca.audit"
        assert record.levelno == logging.INFO

    def test_outside_of_requests(self, caplog: pytest.LogCaptureFixture) -> None:
        """Outside of requests, there is no actor, and nothing fails."""
        audit.log("thing.create", "/things/1", size=3)
        assert _events(caplog) == [
            {
                "action": "thing.create",
                "target": "/things/1",
                "actor": NO_ACTOR,
                "request_id": None,
                "size": 3,
            }
        ]

    def test_without_claims(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Requests without a validated token have no actor."""
        with app.test_request_context():
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"] == NO_ACTOR

    def test_user(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """The actor is the subject of the token."""
        with as_caller(app, ALICE):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"] == {
            "subject": "alice",
            "client": None,
            "on_behalf_of": None,
        }

    def test_service_client(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """The actor includes the client of the token."""
        with as_caller(app, PROTES):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"] == {
            "subject": "protes",
            "client": "protes",
            "on_behalf_of": None,
        }

    def test_on_behalf_of(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """The actor includes the user that a service client acts for."""
        with as_caller(app, CONQUEST, headers={HEADER: "alice"}):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"] == {
            "subject": "conquest",
            "client": "conquest",
            "on_behalf_of": "alice",
        }

    @pytest.mark.parametrize("claims", [ALICE, PROTES])
    def test_on_behalf_of_not_allowed(
        self,
        app: Flask,
        caplog: pytest.LogCaptureFixture,
        claims: dict[str, Any],
    ) -> None:
        """Users named by callers not allowed to aren't believed or logged as denied."""
        caplog.set_level(logging.INFO)
        with as_caller(app, claims, headers={HEADER: "bob"}):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"]["on_behalf_of"] is None
        assert "bob" not in caplog.text
        assert "rejected" not in caplog.text

    def test_failing_debug_log(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failures of logging why a lookup failed don't fail the logging."""
        with (
            as_caller(app, ALICE),
            mock.patch.object(audit.logger, "debug", side_effect=RuntimeError),
            mock.patch(
                "foca.security.access_control.enforcement.get_current_subject",
                side_effect=ValueError,
            ),
        ):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"]["subject"] is None

    def test_failing_request_id(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failures of getting the request ID don't fail the logging."""
        with (
            as_caller(app, ALICE),
            mock.patch("foca.audit.request_id", side_effect=RuntimeError),
        ):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["request_id"] is None

    def test_on_behalf_of_invalid(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid values of the header don't fail the logging."""
        with as_caller(app, CONQUEST, headers={HEADER: " alice "}):
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"]["on_behalf_of"] is None
        assert _events(caplog)[0]["actor"]["client"] == "conquest"

    def test_failed_lookup_is_traceable(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failures to get a part of the actor are logged on level `DEBUG`."""
        caplog.set_level(logging.DEBUG, logger="foca.audit")
        with as_caller(app, CONQUEST, headers={HEADER: " alice "}):
            audit.log("thing.create", "/things/1")
        record = next(r for r in caplog.records if r.levelno == logging.DEBUG)
        assert record.name == "foca.audit"
        assert record.exc_info

    def test_without_configuration(self, caplog: pytest.LogCaptureFixture) -> None:
        """Failures to get the actor don't fail the logging, or lose other parts."""
        app = Flask(__name__)
        with app.test_request_context():
            audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["actor"] == NO_ACTOR

    def test_request_id(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Events have the identifier of the request."""
        monkeypatch.setattr(audit, "request_id", MagicMock(return_value="req-1"))
        audit.log("thing.create", "/things/1")
        assert _events(caplog)[0]["request_id"] == "req-1"
        assert 'request_id="req-1"' in _log_text(caplog)

    def test_request_id_of_context(self, caplog: pytest.LogCaptureFixture) -> None:
        """The identifier is that of `foca.context`."""
        token = context._request_id.set("req-2")
        try:
            audit.log("thing.create", "/things/1")
        finally:
            context._request_id.reset(token)
        assert _events(caplog)[0]["request_id"] == "req-2"

    @pytest.mark.parametrize(
        ("kwargs", "taken"), [({}, False), ({"trust_incoming": True}, True)]
    )
    def test_request_id_of_client(
        self,
        app: Flask,
        caplog: pytest.LogCaptureFixture,
        kwargs: dict[str, Any],
        taken: bool,
    ) -> None:
        """Clients choose the identifier in events only if it's trusted."""

        @app.route("/")
        def _view() -> str:
            """Log an event."""
            audit.log("thing.create", "/things/1")
            return ""

        app.wsgi_app = RequestIdMiddleware(  # type: ignore[method-assign]
            app.wsgi_app, **kwargs
        )
        app.test_client().get("/", headers={"X-Request-ID": "chosen"})
        assert (_events(caplog)[0]["request_id"] == "chosen") is taken

    def test_reserved_fields(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fields can't forge the action, target, actor or request ID."""
        forged: dict[str, Any] = {
            "action": "forged",
            "target": "forged",
            "actor": "forged",
            "request_id": "forged",
        }
        with as_caller(app, ALICE):
            audit.log("thing.create", "/things/1", **forged, role="owner")
        event = _events(caplog)[0]
        assert event["action"] == "thing.create"
        assert event["target"] == "/things/1"
        assert event["actor"]["subject"] == "alice"
        assert event["request_id"] is None
        assert event["role"] == "owner"
        assert "forged" not in _log_text(caplog)

    def test_text(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Text output has all fields."""
        with as_caller(app, ALICE):
            audit.log("thing.create", "/things/1", role="owner", count=2)
        assert _log_text(caplog) == (
            'action="thing.create" target="/things/1" '
            'actor={"subject": "alice", "client": null, "on_behalf_of": null} '
            'request_id=null role="owner" count=2'
        )

    @pytest.mark.parametrize(
        "value",
        [
            'x\nforged action="other"',
            "x\r\nforged",
            "x\u2028forged",
            "x\x1b[31mforged",
            "x\ud800forged",
            'x" role="admin',
            "x\xa0forged",
            "x\x7fforged",
            "x\x85forged",
            "x\U000e0001forged",
            "Zoë",
        ],
    )
    def test_text_escapes_values(
        self, caplog: pytest.LogCaptureFixture, value: str
    ) -> None:
        """Values can't forge lines or fields in text output."""
        audit.log(value, value, role=value)
        message = _log_text(caplog)
        assert message.isprintable()
        assert message.isascii()
        fields = _parse_text(message)
        assert list(fields) == ["action", "target", "actor", "request_id", "role"]
        assert fields["action"] == fields["target"] == fields["role"] == value

    def test_text_escapes_names(self, caplog: pytest.LogCaptureFixture) -> None:
        """Names of fields can't add fields or lines in text output."""
        audit.log("thing.create", "/things/1", **{'a b="c"\n': 1, "plain_name": 2})
        message = _log_text(caplog)
        assert message.isprintable()
        assert '"a b=\\"c\\"\\n"=1' in message
        assert "plain_name=2" in message

    def test_text_unusual_values(self, caplog: pytest.LogCaptureFixture) -> None:
        """Values that JSON doesn't have don't fail the logging."""

        class Unprintable:
            """Object whose text can't be read."""

            def __str__(self) -> str:
                """Fail.

                Raises:
                    RuntimeError: Always.
                """
                raise RuntimeError

        circular: list[Any] = []
        circular.append(circular)
        audit.log(
            "thing.create",
            "/things/1",
            path=Path("/x"),
            nan=float("nan"),
            circular=circular,
            unprintable=Unprintable(),
        )
        message = _log_text(caplog)
        assert 'path="/x"' in message
        assert 'nan="nan"' in message
        assert 'unprintable="<not printable>"' in message
        assert 'circular="[[...]]"' in message

    def test_failing_handler(self, caplog: pytest.LogCaptureFixture) -> None:
        """Failures of handlers don't fail what was logged, and are logged."""

        class Failing(logging.Handler):
            """Handler that fails."""

            def emit(self, record: logging.LogRecord) -> None:
                """Fail.

                Args:
                    record: Log record.

                Raises:
                    RuntimeError: Always.
                """
                raise RuntimeError

        logger = logging.getLogger("foca.audit")
        handler = Failing()
        logger.addHandler(handler)
        try:
            audit.log("thing.create", "/things/1")
        finally:
            logger.removeHandler(handler)
        failure = next(r for r in caplog.records if r.name == "foca.audit_failures")
        assert "thing.create" in failure.getMessage()

    def test_failure_of_failure_logger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing fails if the failure can't be logged either."""
        failing = MagicMock(side_effect=RuntimeError)
        monkeypatch.setattr(audit.logger, "info", failing)
        monkeypatch.setattr(audit._failure_logger, "exception", failing)
        audit.log("thing.create", "/things/1")
        assert failing.call_count == 2

    def test_json_formatter_unusual_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unusual values don't turn the event into text."""
        audit.log("thing.create", "/things/1", score=float("nan"), meta={(1, 2): 3})
        record = next(r for r in caplog.records if r.name == "foca.audit")
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["audit"]["action"] == "thing.create"
        assert parsed["audit"]["score"] == "nan"
        assert parsed["audit"]["meta"] == "{(1, 2): 3}"

    def test_routing_example(self) -> None:
        """The example of the docs is a valid configuration."""
        docs = Path(__file__).parents[1] / "docs/configuration/log.md"
        section = docs.read_text().split("own handler:")[1]
        example = section.split("```yaml\n")[1].split("```")[0]
        conf = LogConfig(**yaml.safe_load(example)["log"])
        assert conf.loggers is not None
        assert "foca.audit" in conf.loggers

    def test_percent_signs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Percent signs in values aren't read as formats."""
        audit.log("thing.create", "/things/%s%d")
        assert 'target="/things/%s%d"' in _log_text(caplog)

    def test_json_formatter(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """The JSON formatter writes the fields as an object."""
        with as_caller(app, ALICE):
            audit.log("thing.create", "/things/1", role="own\ner")
        record = next(r for r in caplog.records if r.name == "foca.audit")
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["logger"] == "foca.audit"
        assert parsed["audit"] == {
            "action": "thing.create",
            "target": "/things/1",
            "actor": {"subject": "alice", "client": None, "on_behalf_of": None},
            "request_id": None,
            "role": "own\ner",
        }


class TestPermissionsApi:
    """Tests for the events of the permissions API."""

    def test_post(
        self, permissions_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Added rules are logged."""
        with permissions_app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            permission_id = unwrap(postPermission)()
        event = _events(caplog)[0]
        assert event["action"] == "permission.create"
        assert event["target"] == permission_id
        assert event["ptype"] == "p"
        assert event["rule"] == ["alice", "data1", "POST"]

    def test_put(
        self, permissions_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Updated rules are logged."""
        with permissions_app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            unwrap(putPermission)(id=MOCK_ID)
        event = _events(caplog)[0]
        assert event["action"] == "permission.create"
        assert event["target"] == MOCK_ID
        assert event["ptype"] == "p"
        assert event["rule"] == ["alice", "data1", "POST"]

    def test_put_stored(
        self,
        permissions_app: Flask,
        collection: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Updated rules are logged with what they replace."""
        collection.insert_one(
            {"id": MOCK_ID, "ptype": "p", "v0": "bob", "v1": "x", "v2": "GET"}
        )
        with permissions_app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            unwrap(putPermission)(id=MOCK_ID)
        event = _events(caplog)[0]
        assert event["action"] == "permission.update"
        assert event["rule"] == ["alice", "data1", "POST"]
        assert event["previous"] == {"ptype": "p", "rule": ["bob", "x", "GET"]}

    def test_delete(
        self,
        permissions_app: Flask,
        collection: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Deleted rules are logged."""
        collection.insert_one({**deepcopy(MOCK_RULE), "id": MOCK_ID})
        with permissions_app.app_context():
            unwrap(deletePermission)(id=MOCK_ID)
        event = _events(caplog)[0]
        assert event["action"] == "permission.delete"
        assert event["target"] == MOCK_ID
        assert event["ptype"] == MOCK_RULE["ptype"]
        assert event["rule"] == [
            MOCK_RULE["v0"],
            MOCK_RULE["v1"],
            MOCK_RULE["v2"],
            MOCK_RULE["v3"],
        ]

    def test_rejected(
        self,
        permissions_app: Flask,
        collection: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Changes that are refused or find nothing aren't logged."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": "configured", "configured": True}
        )
        with permissions_app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            with pytest.raises(BadRequest):
                unwrap(putPermission)(id="x")
            with pytest.raises(NotFound):
                unwrap(deletePermission)(id=MOCK_ID)
            with pytest.raises(Conflict):
                unwrap(deletePermission)(id="configured")
        with (
            permissions_app.test_request_context(json={"policy_type": "p"}),
            pytest.raises(BadRequest),
        ):
            unwrap(postPermission)()
        assert _events(caplog) == []

    def test_values_keep_positions(
        self,
        permissions_app: Flask,
        collection: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Stored values are logged as they are, also odd ones."""
        collection.insert_one(
            {"id": MOCK_ID, "ptype": "p", "v0": 5, "v1": "bob", "v2": "/x"}
        )
        with permissions_app.app_context():
            unwrap(deletePermission)(id=MOCK_ID)
        assert _events(caplog)[0]["rule"][:3] == [5, "bob", "/x"]

    def test_configured_duplicate_not_logged(
        self,
        permissions_app: Flask,
        collection: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The rule logged is the one changed, not a configured one of that name."""
        collection.insert_one(
            {
                "id": MOCK_ID,
                "ptype": "p",
                "v0": "configured",
                "configured": True,
                "generations": ["0" * 24],
            }
        )
        collection.insert_one({"id": MOCK_ID, "ptype": "p", "v0": "real"})
        permissions_app.config["casbin_adapter"].set_configured_policies([])
        with permissions_app.app_context():
            unwrap(deletePermission)(id=MOCK_ID)
        assert _events(caplog)[0]["rule"][0] == "real"


class TestResourceRoles:
    """Tests for the events of the helpers for resource roles."""

    def test_add(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Granted roles are logged once, with the actor."""
        with as_caller(app, ALICE):
            assert add_resource_role("bob", "owner", TASK) is True
            assert add_resource_role("bob", "owner", TASK) is False
        events = _events(caplog)
        assert len(events) == 1
        assert events[0]["action"] == "resource_role.grant"
        assert events[0]["target"] == TASK
        assert events[0]["actor"]["subject"] == "alice"
        assert events[0]["subject"] == "bob"
        assert events[0]["subject_type"] == "user"
        assert events[0]["role"] == "owner"
        assert events[0]["ptype"] == "g2"

    def test_add_invalid(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Roles that can't be granted aren't logged."""
        with as_caller(app, ALICE), pytest.raises(ValueError):
            add_resource_role("", "owner", TASK)
        assert _events(caplog) == []

    def test_remove(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Revoked roles are logged once."""
        with as_caller(app, ALICE):
            add_resource_role("bob", "owner", TASK, subject_type="group")
            caplog.clear()
            assert (
                remove_resource_role("bob", "owner", TASK, subject_type="group") is True
            )
            assert (
                remove_resource_role("bob", "owner", TASK, subject_type="group")
                is False
            )
        events = _events(caplog)
        assert len(events) == 1
        assert events[0]["action"] == "resource_role.revoke"
        assert events[0]["target"] == TASK
        assert events[0]["subject"] == "bob"
        assert events[0]["subject_type"] == "group"
        assert events[0]["role"] == "owner"
        assert events[0]["ptype"] == "g2"

    def test_remove_configured(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Configured rules aren't revoked, so nothing is logged."""
        with as_caller(app, ALICE):
            adapter = app.config["casbin_adapter"]
            adapter.set_configured_policies([["g2", "user:bob", "owner", TASK]])
            assert remove_resource_role("bob", "owner", TASK) is False
            assert adapter.get_filtered_rules("g2", 2, TASK, configured=True) == [
                ["user:bob", "owner", TASK]
            ]
        assert _events(caplog) == []

    def test_remove_resource(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Roles revoked with their resource are logged, with their rules."""
        with as_caller(app, ALICE):
            add_resource_role("bob", "owner", TASK)
            add_resource_role("devs", "owner", TASK, subject_type="group")
            caplog.clear()
            assert remove_resource(TASK) is True
            assert remove_resource(TASK) is False
        events = _events(caplog)
        assert len(events) == 1
        assert events[0]["action"] == "resource_role.revoke_all"
        assert events[0]["target"] == TASK
        assert events[0]["ptype"] == "g2"
        assert sorted(events[0]["rules"]) == [
            ["group:devs", "owner", TASK],
            ["user:bob", "owner", TASK],
        ]
        assert events[0]["swept"] is False
        assert (events[0]["part"], events[0]["parts"]) == (1, 1)
        assert events[0]["incomplete"] is False
        assert events[0]["uncertain"] is None

    def test_remove_resource_batches(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Many rules are logged in several events."""
        with (
            as_caller(app, ALICE),
            mock.patch(
                "foca.security.access_control.enforcement._MAX_RULES_PER_AUDIT_EVENT", 2
            ),
        ):
            for user in ("a", "b", "c"):
                add_resource_role(user, "owner", TASK)
            caplog.clear()
            assert remove_resource(TASK) is True
        events = _events(caplog)
        assert [(event["part"], event["parts"]) for event in events] == [(1, 2), (2, 2)]
        assert [len(event["rules"]) for event in events] == [2, 1]

    def test_remove_resource_swept(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rules removed without being listed are logged."""
        with (
            as_caller(app, ALICE),
            mock.patch.object(
                app.config["casbin_adapter"],
                "remove_filtered_policy",
                return_value=True,
            ),
        ):
            assert remove_resource(TASK) is False
        events = _events(caplog)
        assert len(events) == 1
        assert events[0]["rules"] == []
        assert events[0]["swept"] is True

    def test_remove_resource_invalid_rule(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid rules, which grant no role, leave a trace when removed."""
        adapter = app.config["casbin_adapter"]
        adapter._collection.insert_one(
            {"ptype": "g2", "v0": "user:bob", "v1": "owner", "v2": TASK, "v3": "x"}
        )
        with as_caller(app, ALICE):
            assert remove_resource(TASK) is False
        events = _events(caplog)
        assert len(events) == 1
        assert events[0]["rules"] == []
        assert events[0]["swept"] is True
        assert adapter._collection.count_documents({"ptype": "g2"}) == 0

    def test_remove_resource_failure(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What was removed is logged, also if removing the rest fails."""
        adapter = app.config["casbin_adapter"]
        with as_caller(app, ALICE):
            add_resource_role("bob", "owner", TASK)
            add_resource_role("devs", "owner", TASK, subject_type="group")
            caplog.clear()
            remove_policy = adapter.remove_policy
            calls = iter([remove_policy, MagicMock(side_effect=PyMongoError)])

            def fail_second(*args: Any) -> bool:
                """Remove the first rule, and fail on the second.

                Args:
                    *args: Arguments of `remove_policy()`.

                Returns:
                    Whether the rule was removed.
                """
                return next(calls)(*args)

            with (
                mock.patch.object(adapter, "remove_policy", fail_second),
                pytest.raises(PyMongoError),
            ):
                remove_resource(TASK)
        events = _events(caplog)
        assert len(events) == 1
        remaining = adapter.get_filtered_rules("g2", 2, TASK, configured=False)
        assert len(remaining) == 1
        assert len(events[0]["rules"]) == 1
        assert events[0]["rules"][0] not in remaining
        assert events[0]["incomplete"] is True
        assert events[0]["uncertain"] == remaining[0]

    def test_remove_resource_failed_write_may_have_removed(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing write is logged, as it may have removed its rule."""
        adapter = app.config["casbin_adapter"]
        with as_caller(app, ALICE):
            add_resource_role("bob", "owner", TASK)
            caplog.clear()
            with (
                mock.patch.object(adapter, "remove_policy", side_effect=PyMongoError),
                pytest.raises(PyMongoError),
            ):
                remove_resource(TASK)
        events = _events(caplog)
        assert events[0]["rules"] == []
        assert events[0]["uncertain"] == ["user:bob", "owner", TASK]
        assert events[0]["incomplete"] is True

    def test_remove_resource_failed_sweep(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing removal of the rest is logged, as it may have removed some."""
        adapter = app.config["casbin_adapter"]
        with (
            as_caller(app, ALICE),
            mock.patch.object(
                adapter, "remove_filtered_policy", side_effect=PyMongoError
            ),
            pytest.raises(PyMongoError),
        ):
            remove_resource(TASK)
        events = _events(caplog)
        assert events[0]["rules"] == []
        assert events[0]["uncertain"] is None
        assert events[0]["incomplete"] is True

    def test_remove_resource_failed_read(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing is logged if nothing can have been removed."""
        adapter = app.config["casbin_adapter"]
        with as_caller(app, ALICE):
            calls = iter([[], PyMongoError()])
            with (
                mock.patch.object(adapter, "get_filtered_rules", side_effect=calls),
                pytest.raises(PyMongoError),
            ):
                remove_resource(TASK)
        assert _events(caplog) == []

    def test_replace_keeps_configured(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Configured roles, which are kept, aren't listed as replaced."""
        with as_caller(app, ALICE):
            adapter = app.config["casbin_adapter"]
            add_resource_role("bob", "x", TASK)
            adapter.set_configured_policies([["g2", "user:bob", "owner", TASK]])
            caplog.clear()
            _replace_resource_role("bob", "viewer", TASK, ["owner", "viewer", "x"])
        assert _events(caplog)[0]["replacing"] == ["x"]

    def test_replace(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Replacing roles is logged, also if the role was there."""
        with as_caller(app, ALICE):
            add_resource_role("bob", "x", TASK)
            add_resource_role("carol", "y", TASK)
            caplog.clear()
            assert _replace_resource_role("bob", "owner", TASK, ["owner", "x"]) is True
            assert _replace_resource_role("bob", "owner", TASK, ["owner", "x"]) is False
        events = _events(caplog)
        assert [event["action"] for event in events] == ["resource_role.grant"] * 2
        assert events[0]["subject"] == "bob"
        assert events[0]["subject_type"] == "user"
        assert events[0]["role"] == "owner"
        assert events[0]["ptype"] == "g2"
        assert events[0]["replacing"] == ["x"]
        assert events[0]["already_granted"] is False
        assert events[1]["already_granted"] is True
        # Not held any more
        assert events[1]["replacing"] == []

    def test_ownership(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Operations on owners are logged via the helpers."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            grant_role(TASK, "bob", "owner")
            revoke_roles(TASK, "bob")
        assert [
            (event["action"], event["subject"], event["role"])
            for event in _events(caplog)
        ] == [
            ("resource_role.grant", "alice", "owner"),
            ("resource_role.grant", "bob", "owner"),
            ("resource_role.revoke", "bob", "owner"),
        ]

    def test_rolled_back_grant(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Roles revoked again, as the resource is gone, are logged as revoked."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            caplog.clear()
            with pytest.raises(NotFound):
                grant_role(TASK, "bob", "owner", exists=lambda: False)
        assert [(event["action"], event["subject"]) for event in _events(caplog)] == [
            ("resource_role.grant", "bob"),
            ("resource_role.revoke", "bob"),
        ]

    def test_restored_owner(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Owners restored after a concurrent removal are logged as granted."""
        with as_caller(app, ALICE):
            add_owner(TASK)
            caplog.clear()
            with (
                mock.patch.object(ownership, "_check_other_owners"),
                pytest.raises(LastOwner),
            ):
                revoke_roles(TASK, "alice")
        assert [(event["action"], event["subject"]) for event in _events(caplog)] == [
            ("resource_role.revoke", "alice"),
            ("resource_role.grant", "alice"),
        ]
