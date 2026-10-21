"""Unit tests for endpoint controllers."""

import logging
from collections.abc import Iterator
from copy import deepcopy
from inspect import unwrap
from typing import Any

import mongomock
import pytest
from flask import Flask
from flask.testing import FlaskClient
from pymongo.collection import Collection
from werkzeug.exceptions import BadRequest, Conflict, NotFound

from foca.config.access import get_app_config, set_app_config
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
from foca.utils import logging as logging_module
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MOCK_ID,
    MOCK_PERMISSION,
    MOCK_PERMISSIONS_INVALID,
    MOCK_RULE,
    MOCK_RULE_USER_INPUT_OUTPUT,
    MONGO_CONFIG,
)


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
    set_app_config(app, config)
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


class TestGetAllPermissions:
    """Tests for listing permissions."""

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
        with app.app_context(), pytest.raises(BadRequest):
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
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            res = unwrap(postPermission)()
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
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            ids = {unwrap(postPermission)() for _ in range(10)}
        assert len(ids) == 10
        assert all(len(id) == 8 and set(id) <= {"a", "b"} for id in ids)

    @pytest.mark.parametrize("payload", ["", *MOCK_PERMISSIONS_INVALID])
    def test_invalid(
        self, app: Flask, collection: Collection[dict[str, Any]], payload: Any
    ) -> None:
        """Invalid payloads are rejected."""
        with (
            app.test_request_context(json=deepcopy(payload)),
            pytest.raises(BadRequest),
        ):
            unwrap(postPermission)()
        assert collection.count_documents({}) == 0


class TestPutPermission:
    """Tests for updating permissions."""

    def test_configured(
        self, app: Flask, collection: Collection[dict[str, Any]]
    ) -> None:
        """Configured rules cannot be replaced."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "configured": True}
        )
        with (
            app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
            pytest.raises(Conflict),
        ):
            unwrap(putPermission)(id=MOCK_ID)
        document = collection.find_one({"id": MOCK_ID})
        assert document is not None
        assert document["configured"] is True

    def test_put(self, app: Flask, collection: Collection[dict[str, Any]]) -> None:
        """Only rule fields are stored."""
        collection.insert_one(
            {"id": MOCK_ID, "ptype": "g", "v0": "a", "v1": "b", "foo": "bar"}
        )
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            assert unwrap(putPermission)(id=MOCK_ID) == MOCK_ID
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
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            post_id = unwrap(postPermission)()
            unwrap(putPermission)(id=MOCK_ID)
        posted, put = (
            collection.find_one({"id": id}, projection={"_id": False, "id": False})
            for id in [post_id, MOCK_ID]
        )
        assert posted == put

    @pytest.mark.parametrize("id", ["mock_id", "mock", "mockIdentifier", "mock-1"])
    def test_invalid_id(
        self, app: Flask, collection: Collection[dict[str, Any]], id: str
    ) -> None:
        """Invalid identifiers are rejected."""
        with (
            app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
            pytest.raises(BadRequest),
        ):
            unwrap(putPermission)(id=id)
        assert collection.count_documents({}) == 0

    @pytest.mark.parametrize("payload", ["", *MOCK_PERMISSIONS_INVALID])
    def test_invalid(
        self, app: Flask, collection: Collection[dict[str, Any]], payload: Any
    ) -> None:
        """Invalid payloads are rejected."""
        with (
            app.test_request_context(json=deepcopy(payload)),
            pytest.raises(BadRequest),
        ):
            unwrap(putPermission)(id=MOCK_ID)
        assert collection.count_documents({}) == 0


class TestLogging:
    """Tests for logging identifiers taken from requests."""

    ID = "mock\nId"

    @pytest.fixture(autouse=True)
    def _logger(self, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
        """Capture records of the module, also if its logger was disabled."""
        disabled = module.logger.disabled
        module.logger.disabled = False
        with caplog.at_level(logging.INFO, logger=module.logger.name):
            yield
        module.logger.disabled = disabled

    @staticmethod
    def _assert_escaped(caplog: pytest.LogCaptureFixture) -> None:
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == module.logger.name
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
        """Invalid identifiers are logged escaped."""
        with (
            app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
            pytest.raises(BadRequest),
        ):
            unwrap(putPermission)(id=self.ID)
        self._assert_escaped(caplog)


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
    def client(self, app: Flask) -> FlaskClient:
        """Client of an app routing requests to the controllers."""
        for path, method, controller in [
            ("/permissions", "POST", postPermission),
            ("/permissions", "GET", getAllPermissions),
            ("/permissions/<id>", "PUT", putPermission),
            ("/permissions/<id>", "GET", getPermission),
            ("/permissions/<id>", "DELETE", deletePermission),
        ]:
            app.add_url_rule(
                path,
                endpoint=controller.__name__,
                view_func=controller,
                methods=[method],
            )
        return app.test_client()

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
        client: FlaskClient,
        stored: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
        method: str,
        path: str,
        body: str,
        log_traffic: bool | None,
    ) -> None:
        """Traffic is logged unless disabled via `access_control.log_traffic`.

        The setting is applied as configured at request time.
        """
        if log_traffic is not None:
            config = get_app_config(app)
            config.security.access_control.log_traffic = log_traffic
        response = getattr(client, method)(path, json=deepcopy(MOCK_PERMISSION))
        assert response.status_code == 200
        request = f'"{method.upper()} {path} HTTP/1.1" from 127.0.0.1'
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
