"""Unit tests for endpoint controllers."""

import logging
from copy import deepcopy

import mongomock
import pytest
from flask import Flask
from werkzeug.exceptions import BadRequest, Conflict, NotFound

from foca.config.access import set_app_config
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
def collection():
    """In-memory policy rule collection."""
    return mongomock.MongoClient().db.policy_rules


@pytest.fixture
def app(collection) -> Flask:
    """App whose policy rule collection and Casbin adapter share
    `collection`."""
    app = Flask(__name__)
    access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
    config = Config(db=MongoConfig(**MONGO_CONFIG))
    config.security.access_control = access_control
    config.db.dbs[access_control.db_name].collections[  # type: ignore
        access_control.collection_name
    ].client = collection
    set_app_config(app, config)
    app.config["CASBIN_MODEL"] = access_control.model
    app.config["casbin_adapter"] = Adapter(collection=collection)
    return app


@pytest.fixture
def stored(collection) -> dict:
    """Stored permission; returns the expected permission."""
    collection.insert_one({**deepcopy(MOCK_RULE), "id": MOCK_ID})
    return {**deepcopy(MOCK_RULE_USER_INPUT_OUTPUT), "id": MOCK_ID}


class TestGetPermission:
    """Tests for getting a permission."""

    def test_get(self, app, stored):
        with app.app_context():
            assert getPermission.__wrapped__(id=MOCK_ID) == stored

    def test_extra_fields(self, app, collection):
        """Only rule fields of stored permissions are returned."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "foo": "bar", "v5": None}
        )
        expected = {**deepcopy(MOCK_RULE_USER_INPUT_OUTPUT), "id": MOCK_ID}
        with app.app_context():
            assert getPermission.__wrapped__(id=MOCK_ID) == expected
            assert getAllPermissions.__wrapped__()["results"] == [expected]

    def test_not_found(self, app, stored):
        with app.app_context(), pytest.raises(NotFound):
            getPermission.__wrapped__(id=MOCK_ID + MOCK_ID)


class TestDeletePermission:
    """Tests for deleting a permission."""

    def test_delete(self, app, collection, stored):
        with app.app_context():
            assert deletePermission.__wrapped__(id=MOCK_ID) == MOCK_ID
        assert collection.count_documents({}) == 0

    def test_not_found(self, app, collection):
        collection.insert_one(deepcopy(MOCK_RULE))
        with app.app_context(), pytest.raises(NotFound):
            deletePermission.__wrapped__(id=MOCK_ID)
        assert collection.count_documents({}) == 1

    def test_configured(self, app, collection):
        """Configured rules cannot be deleted."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "configured": True}
        )
        with app.app_context(), pytest.raises(Conflict):
            deletePermission.__wrapped__(id=MOCK_ID)
        assert collection.count_documents({}) == 1


class TestGetAllPermissions:
    """Tests for listing permissions."""

    def test_list(self, app, stored):
        with app.app_context():
            assert getAllPermissions.__wrapped__() == {
                "results": [stored],
                "pagination": {
                    "page": 0,
                    "page_size": 100,
                    "total": 1,
                    "total_pages": 1,
                },
            }

    def test_page(self, app, stored):
        with app.app_context():
            res = getAllPermissions.__wrapped__(page_size=1, page=0)
            assert res["results"] == [stored]

    @pytest.mark.parametrize("page_size,page", [(0, 0), (1001, 0), (1, -1), (1, 1)])
    def test_out_of_range(self, app, stored, page_size, page):
        with app.app_context(), pytest.raises(BadRequest):
            getAllPermissions.__wrapped__(page_size=page_size, page=page)

    def test_incomplete(self, app, collection):
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
            assert getAllPermissions.__wrapped__()["results"] == [
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
            assert getPermission.__wrapped__(id=MOCK_ID)["id"] == MOCK_ID
            with pytest.raises(NotFound):
                getPermission.__wrapped__(id="noType")


class TestPostPermission:
    """Tests for creating permissions."""

    def test_post(self, app, collection):
        """Identifiers are assigned by the implementation."""
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            res = postPermission.__wrapped__()
        stored = collection.find_one({"id": res}, projection={"_id": False})
        assert stored == {
            "id": res,
            "ptype": "p",
            "v0": "alice",
            "v1": "data1",
            "v2": "POST",
        }

    def test_unique_ids(self, app):
        """Generated identifiers are unique and follow the configured
        characters and length."""
        app.config["casbin_adapter"].id_charset = "ab"
        app.config["casbin_adapter"].id_length = 8
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            ids = {postPermission.__wrapped__() for _ in range(10)}
        assert len(ids) == 10
        assert all(len(id) == 8 and set(id) <= {"a", "b"} for id in ids)

    @pytest.mark.parametrize("payload", ["", *MOCK_PERMISSIONS_INVALID])
    def test_invalid(self, app, collection, payload):
        with (
            app.test_request_context(json=deepcopy(payload)),
            pytest.raises(BadRequest),
        ):
            postPermission.__wrapped__()
        assert collection.count_documents({}) == 0


class TestPutPermission:
    """Tests for updating permissions."""

    def test_configured(self, app, collection):
        """Configured rules cannot be replaced."""
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": MOCK_ID, "configured": True}
        )
        with (
            app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
            pytest.raises(Conflict),
        ):
            putPermission.__wrapped__(id=MOCK_ID)
        assert collection.find_one({"id": MOCK_ID})["configured"] is True

    def test_put(self, app, collection):
        """Only rule fields are stored."""
        collection.insert_one(
            {"id": MOCK_ID, "ptype": "g", "v0": "a", "v1": "b", "foo": "bar"}
        )
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            assert putPermission.__wrapped__(id=MOCK_ID) == MOCK_ID
        assert collection.find_one({"id": MOCK_ID}, projection={"_id": False}) == {
            "id": MOCK_ID,
            "ptype": "p",
            "v0": "alice",
            "v1": "data1",
            "v2": "POST",
        }

    def test_same_shape_as_post(self, app, collection):
        """Created and updated permissions are stored alike."""
        with app.test_request_context(json=deepcopy(MOCK_PERMISSION)):
            post_id = postPermission.__wrapped__()
            putPermission.__wrapped__(id=MOCK_ID)
        posted, put = (
            collection.find_one({"id": id}, projection={"_id": False, "id": False})
            for id in [post_id, MOCK_ID]
        )
        assert posted == put

    @pytest.mark.parametrize("id", ["mock_id", "mock", "mockIdentifier", "mock-1"])
    def test_invalid_id(self, app, collection, id):
        with (
            app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
            pytest.raises(BadRequest),
        ):
            putPermission.__wrapped__(id=id)
        assert collection.count_documents({}) == 0

    @pytest.mark.parametrize("payload", ["", *MOCK_PERMISSIONS_INVALID])
    def test_invalid(self, app, collection, payload):
        with (
            app.test_request_context(json=deepcopy(payload)),
            pytest.raises(BadRequest),
        ):
            putPermission.__wrapped__(id=MOCK_ID)
        assert collection.count_documents({}) == 0


class TestLogging:
    """Tests for logging identifiers taken from requests."""

    ID = "mock\nId"

    @pytest.fixture(autouse=True)
    def _logger(self, caplog):
        """Capture records of the module, also if its logger was disabled."""
        disabled = module.logger.disabled
        module.logger.disabled = False
        with caplog.at_level(logging.INFO, logger=module.logger.name):
            yield
        module.logger.disabled = disabled

    @staticmethod
    def _assert_escaped(caplog) -> None:
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == module.logger.name
        ]
        assert messages
        assert all("\n" not in message for message in messages)
        assert any("mock\\nId" in message for message in messages)

    @pytest.mark.parametrize("configured", [True, False])
    def test_delete(self, app, collection, caplog, configured):
        collection.insert_one(
            {**deepcopy(MOCK_RULE), "id": self.ID, "configured": configured}
        )
        with app.app_context():
            if configured:
                with pytest.raises(Conflict):
                    deletePermission.__wrapped__(id=self.ID)
            else:
                deletePermission.__wrapped__(id=self.ID)
        self._assert_escaped(caplog)

    def test_put_invalid_id(self, app, caplog):
        with (
            app.test_request_context(json=deepcopy(MOCK_PERMISSION)),
            pytest.raises(BadRequest),
        ):
            putPermission.__wrapped__(id=self.ID)
        self._assert_escaped(caplog)


class TestTrafficLogging:
    """Tests for logging requests and responses of the permissions API."""

    @pytest.fixture(autouse=True)
    def _logger(self, caplog):
        """Capture records of `log_traffic`, also if its logger was
        disabled."""
        logger = logging_module.logger
        disabled = logger.disabled
        logger.disabled = False
        with caplog.at_level(logging.INFO, logger=logger.name):
            yield
        logger.disabled = disabled

    @pytest.fixture
    def client(self, app):
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
    def test_logged(self, app, client, stored, caplog, method, path, body, log_traffic):
        """Traffic is logged unless disabled via
        `access_control.log_traffic`, as configured at request time."""
        if log_traffic is not None:
            app.config.foca.security.access_control.log_traffic = log_traffic
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
