"""Mock data for testing."""
from pathlib import Path


MOCK_ID = "mock_id"
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
    "port": 27017,
    "dbs": {
        "access_control_db": DB_CONFIG,
    },
}

DIR = Path(__file__).parent / "access_control/foca_casbin_adapter/test_files/"
MODEL_CONF_FILE = str(DIR / "rbac_model.conf")
ACCESS_CONTROL_CONFIG = {
    "db_name": "access_control_db",
    "collection_name": "policy_rules",
    "owner_headers": ["X-User", "X-Group"],
    "user_headers": ["X-User"],
    "model": MODEL_CONF_FILE
}

MOCK_RULE = {
    "rule_section": "p",
    "policy_type": "p1",
    "rule": {
        "v0": "alice",
        "v1": "data1",
        "v2": "POST",
        "v3": "read"
    }
}

MOCK_RULE_INVALID = {"rule": []}

MOCK_PERMISSION = {
    "ptype": "p",
    "v0": "alice",
    "v1": "/",
    "v2": "GET"
}
