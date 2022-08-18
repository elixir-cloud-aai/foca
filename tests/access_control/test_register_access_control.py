"""Tests for registering access control"""

from copy import deepcopy
from flask import Flask
import mongomock

# from foca.access_control.register_access_control import check_permissions
from foca.access_control.foca_casbin_adapter.adapter import Adapter
from foca.models.config import AccessControlConfig, Config, MongoConfig
from tests.mock_data import (
    ACCESS_CONTROL_CONFIG,
    MONGO_CONFIG,
    MOCK_PERMISSION
)

app = Flask(__name__)
TEST_MONGO_CONFIG = deepcopy(MONGO_CONFIG)
access_control = AccessControlConfig(**ACCESS_CONTROL_CONFIG)
app.config["FOCA"] = Config(
    db=MongoConfig(**MONGO_CONFIG),
    access_control=access_control
)
app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
    .client = mongomock.MongoClient().db.collection
app.config["casbin_adapter"] = Adapter(
    uri="mongodb://localhost:12345/",
    dbname=ACCESS_CONTROL_CONFIG["db_name"],
    collection=ACCESS_CONTROL_CONFIG["collection_name"]
)
app.config["CASBIN_MODEL"] = access_control.model
app.config["FOCA"].db.dbs["access_control_db"].collections["policy_rules"]\
    .client.insert_one(MOCK_PERMISSION)
REQ = {
    "REQUEST_METHOD": "GET",
    "PATH_INFO": "/",
    "SERVER_PROTOCOL": "HTTP/1.1",
    "REMOTE_ADDR": "192.168.1.1",
    "headers": {"X-User": "alice"}
}


def test_check_permission():
    """Test to check only valid user requests are permitted via enforcer."""

    # @check_permissions
    def mock_func():
        return {"foo": "bar"}

    with app.test_request_context(environ_base=REQ):
        response = mock_func()
        assert response == {"foo": "bar"}
