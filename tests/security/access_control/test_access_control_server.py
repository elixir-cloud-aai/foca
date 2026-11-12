"""Unit tests for endpoint controllers."""

import logging
from collections.abc import Callable, Iterator
from copy import deepcopy
from functools import wraps
from inspect import unwrap
from typing import Any

import mongomock
import pytest
from flask import Flask, request
from pymongo.collection import Collection
from werkzeug.exceptions import Conflict, NotFound

from foca.config.access import _get_app_config, _set_app_config
from foca.errors import InvalidFieldsProblem
from foca.models.config import AccessControlConfig, Config, MongoConfig
from foca.security.access_control import access_control_server as module
from foca.security.access_control.access_control_server import (
    deletePermission,
    getAllPermissions,
    getPermission,
    postPermission,
    putPermission,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from foca.testing import Client, request_context
from foca.utils import logging as logging_module
from foca.utils.logging import _UNKNOWN_ROUTE
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_ID,
    MOCK_PERMISSION,
    MOCK_PERMISSIONS_INVALID,
    MOCK_RULE,
    MOCK_RULE_USER_INPUT_OUTPUT,
    MONGO_CONFIG,
)


def _passing_body(controller: Callable[..., Any]) -> Callable[..., Any]:
    """Make a view passing the request body to a controller, as Connexion does.

    Args:
        controller: Controller taking the request body as `body`.

    Returns:
        View passing the JSON body of the request and the path parameters.
    """

    @wraps(controller)
    def _view(**kwargs: Any) -> Any:
        """Call the controller with the body of the request."""
        return controller(body=request.get_json(silent=True), **kwargs)

    return _view


@pytest.fixture
def collection() -> Collection[dict[str, Any]]:
    """In-memory policy rule collection."""
    return mongomock.MongoClient().db.policy_rules


@pytest.fixture
def app(collection: Collection[dict[str, Any]]) -> Flask:
    """App whose policy rule collection and Casbin adapter share `collection`."""
    app = Flask(__name__)
    access_control = AccessControlConfig.model_validate(ACCESS_CONTROL_CONFIG)
    config = Config(db=MongoConfig.model_validate(MONGO_CONFIG))
    config.security.access_control = access_control
    config.db.dbs[access_control.db_name].collections[  # type: ignore[union-attr,index]  # set in the config
        access_control.collection_name
    ].client = collection
    _set_app_config(app, config)
    app.config["CASBIN_MODEL"] = access_control.model
    app.config["casbin_adapter"] = Adapter(collection=collection)
    return app


@pytest.fixture
def stored(collection: Collection[dict[str, Any]]) -> dict[str, Any]:
    """Stored permission; returns the expected permission."""
    collection.insert_one({**deepcopy(MOCK_RULE), "id": MOCK_ID})
    return {**deepcopy(MOCK_RULE_USER_INPUT_OUTPUT), "id": MOCK_ID}


class TestGetPermission:
    """Tests for getting a permission."""

    def test_get(self, app: Flask, stored: dict[str, Any]) -> None:
        """Stored permissions are returned."""
        with app.app_context():
            assert unwrap(getPermission)(id=MOCK_ID) == stored

    def test_extra_fields(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Only rule fields of stored permissions are returned."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "foo": "bar", "v5": None}
        )
        expected = {**deepcopy(MOCK_RULE_USER_INPUT_OUTPUT), "id": MOCK_ID}
        with app.app_context():
            assert unwrap(getPermission)(id=MOCK_ID) == expected
            assert unwrap(getAllPermissions)()["results"] == [expected]

    def test_not_found(self, app: Flask, stored: dict[str, Any]) -> None:
        """Unknown identifiers are not found."""
        with app.app_context(), pytest.raises(NotFound):
            unwrap(getPermission)(id=MOCK_ID + MOCK_ID)


class TestDeletePermission:
    """Tests for deleting a permission."""

    def test_delete(
        self, app: Flask, collection: Collection[dict[str, Any]], stored: dict[str, Any]
    ) -> None:
        """Stored permissions are deleted."""
        with app.app_context():
            assert unwrap(deletePermission)(id=MOCK_ID) == MOCK_ID
        assert collection.count_documents({}) == 0

    def test_not_found(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Rules without the identifier are not deleted."""
        collection.insert_one(deepcopy(MOCK_RULE))
        with app.app_context(), pytest.raises(NotFound):
            unwrap(deletePermission)(id=MOCK_ID)
        assert collection.count_documents({}) == 1

    def test_configured(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Configured rules cannot be deleted."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "configured": True}
        )
        with app.app_context(), pytest.raises(Conflict):
            unwrap(deletePermission)(id=MOCK_ID)
        assert collection.count_documents({}) == 1

    def test_configured_meanwhile(
        self,
        app: Flask,
        collection: Collection[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rules configured while deleting them cannot be deleted.

        E.g., if their generation became active after the first check.
        """
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "configured": True}
        )
        # Not configured before deleting, configured once deleting failed
        checks = iter([False, True])
        monkeypatch.setattr(
            app.config["casbin_adapter"], "is_configured", lambda id: next(checks)
        )
        with app.app_context(), pytest.raises(Conflict):
            unwrap(deletePermission)(id=MOCK_ID)
        assert collection.count_documents({}) == 1


class TestGetAllPermissions:
    """Tests for listing permissions."""

    def test_inactive_configured(
        self, app: Flask, collection: Collection[dict[str, Any]], stored: dict[str, Any]
    ) -> None:
        """Configured rules not in effect are neither listed nor returned.

        E.g., rules of a generation of configured rules that is not active
        yet; nor is the document naming the active generation.
        """
        app.config["casbin_adapter"].set_configured_policies([])
        collection.insert_one(
            {
                **deepcopy(MOCK_RULE),
                "id": "pendng",
                "configured": True,
                "generations": ["0" * 24],
            }
        )
        with app.app_context():
            assert unwrap(getAllPermissions)()["results"] == [stored]
            with pytest.raises(NotFound):
                unwrap(getPermission)(id="pendng")

    def test_list(self, app: Flask, stored: dict[str, Any]) -> None:
        """Stored permissions are listed, with pagination."""
        with app.app_context():
            assert unwrap(getAllPermissions)() == {
                "results": [stored],
                "pagination": {
                    "page": 0,
                    "page_size": 100,
                    "total": 1,
                    "total_pages": 1,
                },
            }

    def test_page(self, app: Flask, stored: dict[str, Any]) -> None:
        """Pages of the requested size are returned."""
        with app.app_context():
            res = unwrap(getAllPermissions)(page_size=1, page=0)
            assert res["results"] == [stored]

    @pytest.mark.parametrize("page_size,page", [(0, 0), (1001, 0), (1, -1), (1, 1)])
    def test_out_of_range(
        self, app: Flask, stored: dict[str, Any], page_size: int, page: int
    ) -> None:
        """Page sizes and pages out of range are rejected."""
        with app.app_context(), pytest.raises(InvalidFieldsProblem):
            unwrap(getAllPermissions)(page_size=page_size, page=page)

    def test_incomplete(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Stored rules lacking fields are returned as far as possible."""
        collection.insert_many(
            [
                {"ptype": "p", "v0": "a", "v1": "b", "v2": "GET"},
                {"id": "noType", "v0": "a"},
                {"id": 1, "ptype": "g", "v0": "a", "v1": 2},
                {"id": MOCK_ID, "ptype": "g", "v0": "a", "v1": "b"},
            ]
        )
        with app.app_context():
            assert unwrap(getAllPermissions)()["results"] == [
                {
                    "policy_type": "p",
                    "rule": {"v0": "a", "v1": "b", "v2": "GET"},
                },
                {"policy_type": "g", "rule": {"v0": "a"}},
                {
                    "policy_type": "g",
                    "rule": {"v0": "a", "v1": "b"},
                    "id": MOCK_ID,
                },
            ]
            assert unwrap(getPermission)(id=MOCK_ID)["id"] == MOCK_ID
            with pytest.raises(NotFound):
                unwrap(getPermission)(id="noType")


class TestPostPermission:
    """Tests for creating permissions."""

    def test_post(self, app: Flask, collection: Collection[dict[str, Any]]) -> None:
        """Identifiers are assigned by the implementation."""
        with request_context(app=app):
            res = unwrap(postPermission)(body=deepcopy(MOCK_PERMISSION))
        stored = collection.find_one({"id": res}, projection={"_id": False})
        assert stored == {
            "id": res,
            "ptype": "p",
            "v0": "alice",
            "v1": "data1",
            "v2": "POST",
        }

    def test_unique_ids(self, app: Flask) -> None:
        """Generated identifiers are unique.

        They follow the configured characters and length.
        """
        app.config["casbin_adapter"].id_charset = "ab"
        app.config["casbin_adapter"].id_length = 8
        with request_context(app=app):
            ids = {
                unwrap(postPermission)(body=deepcopy(MOCK_PERMISSION))
                for _ in range(10)
            }
        assert len(ids) == 10
        assert all(len(id) == 8 and set(id) <= {"a", "b"} for id in ids)

    @pytest.mark.parametrize("payload", ["", *MOCK_PERMISSIONS_INVALID])
    def test_invalid(
        self, app: Flask, collection: Collection[dict[str, Any]], payload: Any
    ) -> None:
        """Invalid payloads are rejected."""
        with (
            request_context(app=app),
            pytest.raises(InvalidFieldsProblem),
        ):
            unwrap(postPermission)(body=deepcopy(payload))
        assert collection.count_documents({}) == 0


class TestBodyNames:
    """Tests for the names of the request body of the controllers."""

    def test_permission_deprecated(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """The body is taken as `permission`, with a deprecation warning."""
        with (
            request_context(app=app),
            pytest.warns(DeprecationWarning, match="x-body-name: permission"),
        ):
            post_id = unwrap(postPermission)(permission=deepcopy(MOCK_PERMISSION))
            unwrap(putPermission)(id=MOCK_ID, permission=deepcopy(MOCK_PERMISSION))
        assert collection.count_documents({"id": {"$in": [post_id, MOCK_ID]}}) == 2

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, "got neither"),
            ({"body": MOCK_PERMISSION, "permission": MOCK_PERMISSION}, "got both"),
        ],
    )
    def test_neither_or_both(
        self,
        app: Flask,
        collection: Collection[dict[str, Any]],
        kwargs: dict[str, Any],
        expected: str,
    ) -> None:
        """Calls with neither or both names are rejected."""
        with request_context(app=app):
            with pytest.raises(TypeError, match=expected):
                unwrap(postPermission)(**deepcopy(kwargs))
            with pytest.raises(TypeError, match=expected):
                unwrap(putPermission)(id=MOCK_ID, **deepcopy(kwargs))
        assert collection.count_documents({}) == 0


class TestPutPermission:
    """Tests for updating permissions."""

    def test_inactive_configured(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Configured rules not in effect are neither replaced nor deleted.

        E.g., rules of a generation that is not active yet. As for getting
        them, they are not found for deletion.
        """
        adapter = app.config["casbin_adapter"]
        adapter.set_configured_policies([])
        adapter.save_policy_line("p", ["a", "/b", "GET"])
        collection.insert_one(
            {
                **deepcopy(MOCK_RULE),
                "id": MOCK_ID,
                "configured": True,
                "generations": ["0" * 24],
            }
        )
        with request_context(app=app):
            with pytest.raises(Conflict):
                unwrap(putPermission)(id=MOCK_ID, body=deepcopy(MOCK_PERMISSION))
            with pytest.raises(NotFound):
                unwrap(deletePermission)(id=MOCK_ID)
        document = collection.find_one({"id": MOCK_ID})
        assert document is not None
        assert document["configured"] is True

    def test_configured(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Configured rules cannot be replaced."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "configured": True}
        )
        with (
            request_context(app=app),
            pytest.raises(Conflict),
        ):
            unwrap(putPermission)(id=MOCK_ID, body=deepcopy(MOCK_PERMISSION))
        document = collection.find_one({"id": MOCK_ID})
        assert document is not None
        assert document["configured"] is True

    def test_put(self, app: Flask, collection: Collection[dict[str, Any]]) -> None:
        """Only rule fields are stored."""
        collection.insert_one(
            {"id": MOCK_ID, "ptype": "g", "v0": "a", "v1": "b", "foo": "bar"}
        )
        with request_context(app=app):
            assert (
                unwrap(putPermission)(id=MOCK_ID, body=deepcopy(MOCK_PERMISSION))
                == MOCK_ID
            )
        assert collection.find_one({"id": MOCK_ID}, projection={"_id": False}) == {
            "id": MOCK_ID,
            "ptype": "p",
            "v0": "alice",
            "v1": "data1",
            "v2": "POST",
        }

    def test_same_shape_as_post(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Created and updated permissions are stored alike."""
        with request_context(app=app):
            post_id = unwrap(postPermission)(body=deepcopy(MOCK_PERMISSION))
            unwrap(putPermission)(id=MOCK_ID, body=deepcopy(MOCK_PERMISSION))
        posted, put = (
            collection.find_one({"id": id}, projection={"_id": False, "id": False})
            for id in [post_id, MOCK_ID]
        )
        assert posted == put

    @pytest.mark.parametrize("id", ["mock_id", "mock", "mockIdentifier", "mock-1"])
    def test_invalid_id(
        self,
        app: Flask,
        collection: Collection[dict[str, Any]],
        id: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid identifiers are rejected, and not logged at INFO or higher."""
        with (
            caplog.at_level("INFO"),
            request_context(app=app),
            pytest.raises(InvalidFieldsProblem),
        ):
            unwrap(putPermission)(id=id, body=deepcopy(MOCK_PERMISSION))
        assert collection.count_documents({}) == 0
        assert "Invalid policy rule identifier" in caplog.text
        assert id not in caplog.text

    @pytest.mark.parametrize("payload", ["", *MOCK_PERMISSIONS_INVALID])
    def test_invalid(
        self, app: Flask, collection: Collection[dict[str, Any]], payload: Any
    ) -> None:
        """Invalid payloads are rejected."""
        with (
            request_context(app=app),
            pytest.raises(InvalidFieldsProblem),
        ):
            unwrap(putPermission)(id=MOCK_ID, body=deepcopy(payload))
        assert collection.count_documents({}) == 0

    def test_invalid_values_not_logged(
        self, app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Values of invalid rules are logged at DEBUG only.

        Neither in messages at INFO or above nor in the exception's cause.
        """
        payload = {"policy_type": "s3cr3t", "rule": {"v0": "a", "v1": "b"}}
        with (
            caplog.at_level("DEBUG"),
            request_context(app=app),
            pytest.raises(InvalidFieldsProblem) as exc_info,
        ):
            unwrap(putPermission)(id=MOCK_ID, body=payload)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__
        infos = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert "Invalid policy rule rejected" in infos
        assert not any("s3cr3t" in message for message in infos)
        assert "s3cr3t" in caplog.text


class TestLogging:
    """Tests for logging identifiers taken from requests."""

    ID = "mock\nId"

    @pytest.fixture(autouse=True)
    def _logger(self, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
        """Capture records of the module, also if its logger was disabled."""
        disabled = module.logger.disabled
        module.logger.disabled = False
        with (
            caplog.at_level(logging.INFO, logger=module.logger.name),
            caplog.at_level(logging.INFO, logger="foca.audit"),
        ):
            yield
        module.logger.disabled = disabled

    @staticmethod
    def _assert_escaped(caplog: pytest.LogCaptureFixture) -> None:
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name in (module.logger.name, "foca.audit")
        ]
        assert messages
        assert all("\n" not in message for message in messages)
        assert any("mock\\nId" in message for message in messages)

    @pytest.mark.parametrize("configured", [True, False])
    def test_delete(
        self,
        app: Flask,
        collection: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
        configured: bool,
    ) -> None:
        """Identifiers of deleted permissions are logged escaped."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": self.ID, "configured": configured}
        )
        with app.app_context():
            if configured:
                with pytest.raises(Conflict):
                    unwrap(deletePermission)(id=self.ID)
            else:
                unwrap(deletePermission)(id=self.ID)
        self._assert_escaped(caplog)

    def test_put_invalid_id(self, app: Flask, caplog: pytest.LogCaptureFixture) -> None:
        """Invalid identifiers are not logged, as they are the client's values."""
        with (
            request_context(app=app),
            pytest.raises(InvalidFieldsProblem),
        ):
            unwrap(putPermission)(id=self.ID, body=deepcopy(MOCK_PERMISSION))
        messages = [record.getMessage() for record in caplog.records]
        assert "Invalid policy rule identifier rejected" in messages
        assert not any("mock" in message for message in messages)


class TestTrafficLogging:
    """Tests for logging requests and responses of the permissions API."""

    @pytest.fixture(autouse=True)
    def _logger(self, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
        """Capture records of `log_traffic`, also if its logger was disabled."""
        logger = logging_module.logger
        disabled = logger.disabled
        logger.disabled = False
        with caplog.at_level(logging.INFO, logger=logger.name):
            yield
        logger.disabled = disabled

    @pytest.fixture
    def client(self, app: Flask) -> Client:
        """Client of an app routing requests to the controllers."""
        for path, method, controller in [
            ("/permissions", "POST", _passing_body(postPermission)),
            ("/permissions", "GET", getAllPermissions),
            ("/permissions/<id>", "PUT", _passing_body(putPermission)),
            ("/permissions/<id>", "GET", getPermission),
            ("/permissions/<id>", "DELETE", deletePermission),
        ]:
            app.add_url_rule(
                path,
                endpoint=controller.__name__,
                view_func=controller,
                methods=[method],
            )
        return Client(app)

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("post", "/permissions", "str"),
            ("get", "/permissions", "dict"),
            ("put", f"/permissions/{MOCK_ID}", "str"),
            ("get", f"/permissions/{MOCK_ID}", "dict"),
            ("delete", f"/permissions/{MOCK_ID}", "str"),
        ],
    )
    @pytest.mark.parametrize("log_traffic", [None, True, False])
    def test_logged(
        self,
        app: Flask,
        client: Client,
        stored: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
        method: str,
        path: str,
        body: str,
        log_traffic: bool | None,
    ) -> None:
        """Traffic is logged unless disabled via `access_control.log_traffic`.

        The setting is applied as configured at request time. The controllers
        are routed by Flask, not as operations, so the route is unknown.
        """
        if log_traffic is not None:
            config = _get_app_config(app)
            config.security.access_control.log_traffic = log_traffic
        response = client.request(method, path, json=deepcopy(MOCK_PERMISSION))
        assert response.status_code == 200
        request = f'"{method.upper()} {_UNKNOWN_ROUTE} HTTP/1.1" from 127.0.0.1'
        expected = [
            f"Incoming request: {request}",
            f"Response to request {request}: {body}, status 200",
        ]
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == logging_module.logger.name
        ]
        assert messages == ([] if log_traffic is False else expected)


# Invalid payloads, with the invalid field (JSON Pointer) and its code
INVALID_FIELDS: list[tuple[Any, str, str]] = [
    ("", "#", "invalid-type"),
    ({"rule": []}, "#/rule", "invalid-type"),
    ({"policy_type": "p"}, "#/rule", "required"),
    ({"rule": {"v0": "a", "v1": "b", "v2": "GET"}}, "#/policy_type", "required"),
    (
        {"policy_type": 1, "rule": {"v0": "a", "v1": "b", "v2": "GET"}},
        "#/policy_type",
        "invalid-type",
    ),
    (
        {"policy_type": "s3cr3t", "rule": {"v0": "a", "v1": "b", "v2": "GET"}},
        "#/policy_type",
        "pattern-mismatch",
    ),
    (
        {"policy_type": "p2", "rule": {"v0": "a", "v1": "b", "v2": "GET"}},
        "#/policy_type",
        "unknown-policy-type",
    ),
    ({"policy_type": "p", "rule": {}}, "#/rule", "wrong-value-count"),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": "b"}},
        "#/rule",
        "wrong-value-count",
    ),
    (
        {"policy_type": "g", "rule": {"v0": "a", "v1": "b", "v2": "c"}},
        "#/rule",
        "wrong-value-count",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": "b", "act": "GET"}},
        "#/rule/act",
        "unknown-property",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": "b", "v3": "GET"}},
        "#/rule",
        "non-consecutive-keys",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": 1, "v2": "GET"}},
        "#/rule/v1",
        "invalid-type",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "", "v1": "b", "v2": "GET"}},
        "#/rule/v0",
        "too-short",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": "s" * 1025, "v2": "GET"}},
        "#/rule/v1",
        "too-long",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a,s3cr3t", "v1": "b", "v2": "GET"}},
        "#/rule/v0",
        "invalid-character",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": "b\ns3cr3t", "v2": "GET"}},
        "#/rule/v1",
        "invalid-character",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "#s3cr3t", "v1": "b", "v2": "GET"}},
        "#/rule/v0",
        "invalid-character",
    ),
    (
        {"policy_type": "p", "rule": {"v0": "a", "v1": "b", "v2": "(s3cr3t"}},
        "#/rule/v2",
        "invalid-pattern",
    ),
]


class TestInvalidFields:
    """Tests for the invalid fields reported by the permissions API."""

    @staticmethod
    def _assert_field(exc: InvalidFieldsProblem, pointer: str, code: str) -> None:
        """Assert that the problem names one field, without the client's values.

        Args:
            exc: Raised problem.
            pointer: Expected JSON Pointer of the invalid field.
            code: Expected code.
        """
        assert [(e.pointer, e.parameter, e.code) for e in exc.field_errors] == [
            (pointer, None, code)
        ]
        assert "s3cr3t" not in exc.field_errors[0].detail
        assert "s3cr3t" not in str(exc.detail)
        assert exc.__cause__ is None

    @pytest.mark.parametrize("payload,pointer,code", INVALID_FIELDS)
    def test_post(self, app: Flask, payload: Any, pointer: str, code: str) -> None:
        """Invalid fields of new permissions are reported."""
        with request_context(app=app), pytest.raises(InvalidFieldsProblem) as exc:
            unwrap(postPermission)(body=deepcopy(payload))
        self._assert_field(exc.value, pointer, code)

    @pytest.mark.parametrize("payload,pointer,code", INVALID_FIELDS)
    def test_put(self, app: Flask, payload: Any, pointer: str, code: str) -> None:
        """Invalid fields of updated permissions are reported."""
        with request_context(app=app), pytest.raises(InvalidFieldsProblem) as exc:
            unwrap(putPermission)(id=MOCK_ID, body=deepcopy(payload))
        self._assert_field(exc.value, pointer, code)

    def test_opt_in_role(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        """Role rules granting opt-in roles are reported at the role."""
        monkeypatch.setattr(module, "grants_opt_in_role", lambda *_: True)
        body = {"policy_type": "g", "rule": {"v0": "user:a", "v1": "s3cr3t"}}
        with request_context(app=app), pytest.raises(InvalidFieldsProblem) as exc:
            unwrap(postPermission)(body=body)
        self._assert_field(exc.value, "#/rule/v1", "opt-in-role")

    @pytest.mark.parametrize(
        "id,code", [("mock", "invalid-length"), ("mock-1", "invalid-character")]
    )
    def test_put_id(self, app: Flask, id: str, code: str) -> None:
        """Invalid identifiers are reported as the path parameter `id`."""
        with request_context(app=app), pytest.raises(InvalidFieldsProblem) as exc:
            unwrap(putPermission)(id=id, body=deepcopy(MOCK_PERMISSION))
        errors = exc.value.field_errors
        assert [(e.pointer, e.parameter, e.code) for e in errors] == [
            (None, "id", code)
        ]
        assert id not in errors[0].detail

    @pytest.mark.parametrize(
        "page_size,page,parameter,code",
        [
            (0, 0, "page_size", "too-small"),
            (1001, 0, "page_size", "too-large"),
            (1, -1, "page", "too-small"),
            (1, 1, "page", "too-large"),
        ],
    )
    def test_pages(
        self,
        app: Flask,
        stored: dict[str, Any],
        page_size: int,
        page: int,
        parameter: str,
        code: str,
    ) -> None:
        """Page sizes and pages out of range are reported as their parameters."""
        with app.app_context(), pytest.raises(InvalidFieldsProblem) as exc:
            unwrap(getAllPermissions)(page_size=page_size, page=page)
        assert [(e.pointer, e.parameter, e.code) for e in exc.value.field_errors] == [
            (None, parameter, code)
        ]
