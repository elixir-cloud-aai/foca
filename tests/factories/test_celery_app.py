"""Unit tests for Celery app factory module."""

import pytest
from celery import Celery

from foca.factories import celery_app as module
from foca.factories.celery_app import create_celery_app
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, JobsConfig

CONFIG = Config()
CONFIG.jobs = JobsConfig()


@pytest.fixture
def logged(monkeypatch) -> list[str]:
    """Messages logged via `logger.info()` of the Celery factory module."""
    for var in ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"]:
        monkeypatch.delenv(var, raising=False)
    messages: list[str] = []
    monkeypatch.setattr(module.logger, "info", messages.append)
    return messages


def test_create_celery_app(logged):
    """Test Connexion app creation."""
    cnx_app = create_connexion_app(CONFIG)
    cel_app = create_celery_app(cnx_app.app)
    assert isinstance(cel_app, Celery)
    assert cel_app.conf.foca == CONFIG
    assert cel_app.conf.broker_url == "pyamqp://rabbitmq:5672//"
    assert cel_app.conf.result_backend == "rpc://"


def test_broker_env_vars(logged, monkeypatch):
    """Broker and backend URLs from environment variables are used, but not
    logged with credentials."""
    broker = "amqps://user:secret@rabbitmq.example.org:5671/vhost?ssl=1"
    backend = "redis://:secret@redis:6379/0"
    monkeypatch.setenv("CELERY_BROKER_URL", broker)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", backend)
    cnx_app = create_connexion_app(CONFIG)
    cel_app = create_celery_app(cnx_app.app)
    assert cel_app.conf.broker_url == broker
    assert cel_app.conf.result_backend == backend
    assert "secret" not in " ".join(logged)
    assert (
        "Celery app created with broker "
        "'amqps://rabbitmq.example.org:5671/vhost' and result backend "
        "'redis://redis:6379/0'."
    ) in logged


def test_create_celery_app_without_jobs(logged):
    """Background tasks need to be configured."""
    cnx_app = create_connexion_app(Config())
    with pytest.raises(ValueError):
        create_celery_app(cnx_app.app)
