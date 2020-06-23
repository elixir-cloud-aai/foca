"""Unit tests for Celery app factory module."""

from celery import Celery

from foca.factories.celery_app import create_celery_app
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (Config, JobsConfig)

CONFIG = Config()
CONFIG.jobs = JobsConfig()


def test_create_celery_app():
    """Test Connexion app creation."""
    cnx_app = create_connexion_app(CONFIG)
    cel_app = create_celery_app(cnx_app.app)
    assert isinstance(cel_app, Celery)
    assert cel_app.conf['FOCA'] == CONFIG
