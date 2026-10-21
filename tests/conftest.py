"""Shared test fixtures."""

from typing import Any

import mongomock
import pytest
from pymongo import MongoClient

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
def mongo_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, MongoClient[dict[str, Any]]]]:
    """Replace MongoDB clients created by FOCA with in-memory clients.

    MongoDB-related environment variables are unset.

    Returns:
        Connection string and in-memory client of each client created, in
        order.
    """
    for var in MONGO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    created: list[tuple[str, MongoClient[dict[str, Any]]]] = []

    def mongo_client(uri: str) -> MongoClient[dict[str, Any]]:
        client: MongoClient[dict[str, Any]] = mongomock.MongoClient()
        created.append((uri, client))
        return client

    monkeypatch.setattr(register_mongodb, "MongoClient", mongo_client)
    return created
