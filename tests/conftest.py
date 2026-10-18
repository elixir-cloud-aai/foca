"""Shared test fixtures."""

import mongomock
import pytest

from foca.database import register_mongodb

# Environment variables configuring MongoDB connections
MONGO_ENV_VARS = [
    "MONGO_URI",
    "MONGO_HOST",
    "MONGO_PORT",
    "MONGO_DBNAME",
    "MONGO_USERNAME",
    "MONGO_PASSWORD",
]


@pytest.fixture
def mongo_clients(monkeypatch) -> list[tuple[str, mongomock.MongoClient]]:
    """Replace MongoDB clients created by FOCA with in-memory clients.

    MongoDB-related environment variables are unset.

    Returns:
        Connection string and in-memory client of each client created, in
        order.
    """
    for var in MONGO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    created: list[tuple[str, mongomock.MongoClient]] = []

    def mongo_client(uri: str) -> mongomock.MongoClient:
        client = mongomock.MongoClient()
        created.append((uri, client))
        return client

    monkeypatch.setattr(register_mongodb, "MongoClient", mongo_client)
    return created
