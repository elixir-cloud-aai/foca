"""Mock data for testing."""
from pathlib import Path


INDEX_CONFIG = {
    "keys": [("id", 1)]
}
COLLECTION_CONFIG = {
    "indexes": [INDEX_CONFIG],
}
DB_CONFIG = {
    "collections": {
        "policy_rules": COLLECTION_CONFIG,
    },
}
MONGO_CONFIG = {
    "host": "mongodb",
    "port": 12345,
    "dbs": {
        "access_control_db": DB_CONFIG,
    },
}
RELATIVE_PATH = "security/access_control/foca_casbin_adapter/test_files/"
DIR = Path(__file__).parent / RELATIVE_PATH
MODEL_CONF_FILE = str(DIR / "rbac_model.conf")
ACCESS_CONTROL_CONFIG = {
    "db_name": "access_control_db",
    "collection_name": "policy_rules",
    "owner_headers": ["X-User", "X-Group"],
    "user_headers": ["X-User"],
    "model": MODEL_CONF_FILE
}

MOCK_ID = "mock_id"
MOCK_RULE = {
    "ptype": "p1",
    "v0": "alice",
    "v1": "data1",
    "v2": "POST",
    "v3": "read"
}
MOCK_RULE_USER_INPUT_OUTPUT = {
    "policy_type": "p1",
    "rule": {
        "v0": "alice",
        "v1": "data1",
        "v2": "POST",
        "v3": "read"
    }
}
MOCK_RULE_INVALID = {"rule": []}
MOCK_PERMISSION = ["alice", "/", "GET"]
MOCK_REQUEST = {
    "REQUEST_METHOD": "GET",
    "PATH_INFO": "/",
    "SERVER_PROTOCOL": "HTTP/1.1",
    "REMOTE_ADDR": "192.168.1.1"
}
