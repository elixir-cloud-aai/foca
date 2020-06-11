"""Unit tests for Celery app factory module."""

from celery import Celery
from flask import Flask
import pytest

from foca.factories.celery_app import create_celery_app

MOCK_HOST = "mock_host"
MOCK_PORT = "mock_port"
DICT_CONF_VALS = {
    'broker_host': "broker_host",
    'broker_port': 5762,
    'result_backend': "result_backend",
    'include': "include",
    'message_maxsize': 10000
}


def test_create_celery_app(monkeypatch):
    """Test for correct creatiion of celery app"""
    def mock_os_env(*args, **kwargs):
        if 'RABBIT_HOST' in args:
            return MOCK_HOST
        elif 'RABBIT_PORT' in args:
            return MOCK_PORT

    monkeypatch.setattr(
        'foca.factories.celery_app.os.environ.get',
        mock_os_env
    )

    def mock_get_conf(*args, **kwargs):
        return DICT_CONF_VALS[list(args)[2]]

    monkeypatch.setattr(
        'foca.factories.celery_app.get_conf',
        mock_get_conf
    )

    monkeypatch.setattr(
        'foca.factories.celery_app.get_conf_type',
        lambda *args, **kwargs: DICT_CONF_VALS['include']
    )

    demo_app = Flask(__name__)
    demo_app.config['BROKER_URL'] = "broker_url = 'amqp://myuser:mypassword@localhost:5672/myvhost"
    demo_app.config['celery'] = 'celery'
    celery = create_celery_app(demo_app)

    #@celery.task()
    #def add_together(a, b):
    #    return a + b

    #result = add_together.delay(23, 42)

    assert type(celery) == type(Celery(app=__name__))


class eager_conf:
    """Configuration for testing Celery tasks for Celery >= 4.0."""

    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True
    CELERY_RESULT_BACKEND = "cache"
    CELERY_CACHE_BACKEND = "memory"

@pytest.fixture(scope='session')
def celery_config():
    return {
        'broker_url': 'amqp://',
        'result_backend': 'redis://'
    }

def test_appctx_task(monkeypatch, celery_worker):
    """Test execution of Celery task with application context."""
    app = Flask("myapp")
    app.config.from_object(eager_conf)

    def mock_get_conf(*args, **kwargs):
        return DICT_CONF_VALS[list(args)[2]]

    monkeypatch.setattr(
        'foca.factories.celery_app.get_conf',
        mock_get_conf
    )

    monkeypatch.setattr(
        'foca.factories.celery_app.get_conf_type',
        lambda *args, **kwargs: DICT_CONF_VALS['include']
    )

    monkeypatch.setattr(
        'foca.factories.celery_app.get_conf_type',
        lambda *args, **kwargs: DICT_CONF_VALS['include']
    )

    # Set the current Celery application
    c = Celery('mycurrent')
    c.set_current()

    celery = create_celery_app(app)

    @celery.task
    def appctx():
        return current_app.name

    r = appctx.delay()
    assert r.result == "myapp"
