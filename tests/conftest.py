"""Shared test fixtures."""

import logging
import logging.config
from collections.abc import Iterator
from typing import Any

import mongomock
import pytest
from pymongo import MongoClient

from foca.config import config_parser
from foca.database import register_mongodb
from tests.mock_data import MongoClients

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
) -> MongoClients:
    """Replace MongoDB clients created by FOCA with in-memory clients.

    MongoDB-related environment variables are unset.

    Returns:
        Connection string and in-memory client of each client created, in
        order.
    """
    for var in MONGO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    created: MongoClients = []

    def mongo_client(uri: str) -> MongoClient[dict[str, Any]]:
        client: MongoClient[dict[str, Any]] = mongomock.MongoClient()
        created.append((uri, client))
        return client

    monkeypatch.setattr(register_mongodb, "MongoClient", mongo_client)
    return created


@pytest.fixture
def log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Iterator[pytest.LogCaptureFixture]:
    """Capture logs, also after `Foca()` configured logging.

    FOCA's logging configuration replaces the handlers of the root logger,
    dropping pytest's capturing handler, so it is attached again.
    """

    def dict_config(config: dict[str, Any]) -> None:
        logging.config.dictConfig(config)
        logging.getLogger().addHandler(caplog.handler)

    monkeypatch.setattr(config_parser, "dictConfig", dict_config)
    caplog.set_level(logging.INFO)
    yield caplog
    logging.getLogger().removeHandler(caplog.handler)
