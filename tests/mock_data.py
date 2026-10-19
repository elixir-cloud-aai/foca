"""Mock data for testing."""

from pathlib import Path

INDEX_CONFIG = {"keys": [("id", 1)]}
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
    "model": MODEL_CONF_FILE,
}

MOCK_ID = "mock_id"
MOCK_RULE = {
    "ptype": "p1",
    "v0": "alice",
    "v1": "data1",
    "v2": "POST",
    "v3": "read",
}
MOCK_RULE_USER_INPUT_OUTPUT = {
    "policy_type": "p1",
    "rule": {"v0": "alice", "v1": "data1", "v2": "POST", "v3": "read"},
}
MOCK_RULE_INVALID = {"rule": []}
# Permission valid for model `MODEL_CONF_FILE`
MOCK_PERMISSION = {
    "policy_type": "p",
    "rule": {"v0": "alice", "v1": "data1", "v2": "POST"},
}
# Permissions invalid for model `MODEL_CONF_FILE`
MOCK_PERMISSIONS_INVALID = [
    MOCK_RULE_INVALID,
    {"policy_type": "p"},
    # policy type not defined in model
    {"policy_type": "p2", "rule": {"v0": "a", "v1": "b", "v2": "GET"}},
    {"policy_type": "x", "rule": {"v0": "a", "v1": "b", "v2": "GET"}},
    {"rule": {"v0": "a", "v1": "b", "v2": "GET"}},
    # wrong number of values
    {"policy_type": "p", "rule": {"v0": "a", "v1": "b"}},
    {"policy_type": "g", "rule": {"v0": "a", "v1": "b", "v2": "c"}},
    {"policy_type": "p", "rule": {}},
    # unknown and non-consecutive keys
    {"policy_type": "p", "rule": {"v0": "a", "v1": "b", "v3": "GET"}},
    {"policy_type": "p", "rule": {"v0": "a", "v1": "b", "act": "GET"}},
    # invalid values
    {"policy_type": "p", "rule": {"v0": "a,b", "v1": "b", "v2": "GET"}},
    {"policy_type": "p", "rule": {"v0": "a\nb", "v1": "b", "v2": "GET"}},
    {"policy_type": "p", "rule": {"v0": "a\rb", "v1": "b", "v2": "GET"}},
    {"policy_type": "p", "rule": {"v0": "#a", "v1": "b", "v2": "GET"}},
    {"policy_type": "p", "rule": {"v0": "", "v1": "b", "v2": "GET"}},
    {"policy_type": "p", "rule": {"v0": 1, "v1": "b", "v2": "GET"}},
    {"policy_type": "p", "rule": {"v0": "a" * 1025, "v1": "b", "v2": "GET"}},
    # invalid action pattern
    {"policy_type": "p", "rule": {"v0": "a", "v1": "b", "v2": "(GET"}},
]
