"""Mock data for testing."""


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

ACCESS_CONTROL_CONFIG = {
    "db_name": "access_control_db",
    "collection_name": "policy_rules"
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
