"""Unit tests for Celery app factory module."""


import json

from connexion import App
from connexion.apps.flask_app import FlaskApp
from flask import Response

from foca.models.config import (Config, JobsConfig)
from foca.factories.connexion_app import (
    __add_config_to_connexion_app,
    create_connexion_app,
    )


from celery import Celery
from flask import Flask

from foca.factories.celery_app import create_celery_app
from foca.factories.connexion_app import create_connexion_app

CONFIG = Config()
CONFIG.jobs = JobsConfig()


def test_create_celery_app():
    """Test Connexion app creation."""
    cnx_app = create_connexion_app(CONFIG)
    print(CONFIG)
    cel_app = create_celery_app(cnx_app.app)
    assert isinstance(cel_app, Celery)
    assert cel_app.conf['FOCA'] == CONFIG

#    """Test for correct creatiion of celery app"""
#    def mock_os_env(*args, **kwargs):
#        if 'RABBIT_HOST' in args:
#            return MOCK_HOST
#        elif 'RABBIT_PORT' in args:
#            return MOCK_PORT
#
#    monkeypatch.setattr(
#        'foca.factories.celery_app.os.environ.get',
#        mock_os_env
#    )
#
#    def mock_get_conf(*args, **kwargs):
#        return DICT_CONF_VALS[list(args)[2]]
#
#    monkeypatch.setattr(
#        'foca.factories.celery_app.get_conf',
#        mock_get_conf
#    )
#
#    monkeypatch.setattr(
#        'foca.factories.celery_app.get_conf_type',
#        lambda *args, **kwargs: DICT_CONF_VALS['include']
#    )
#
#    demo_app = Flask(__name__)
#    demo_app.config['BROKER_URL'] = "broker_url = 'amqp://myuser:mypassword@localhost:5672/myvhost"
#    demo_app.config['celery'] = 'celery'
#    celery = create_celery_app(demo_app)
#
#    #@celery.task()
#    #def add_together(a, b):
#    #    return a + b
#
#    #result = add_together.delay(23, 42)
#
#    assert type(celery) == type(Celery(app=__name__))
#
#
#class eager_conf:
#    """Configuration for testing Celery tasks for Celery >= 4.0."""
#
#    CELERY_TASK_ALWAYS_EAGER = True
#    CELERY_TASK_EAGER_PROPAGATES = True
#    CELERY_RESULT_BACKEND = "cache"
#    CELERY_CACHE_BACKEND = "memory"
#
#@pytest.fixture(scope='session')
#def celery_config():
#    return {
#        'broker_url': 'amqp://',
#        'backend': 'redis://'
#    }
#
#
#def test_appctx_task(monkeypatch, celery_worker):
#    """Test execution of Celery task with application context."""
#    app = Flask("myapp")
#    app.config.from_object(eager_conf)
#
#    def mock_get_conf(*args, **kwargs):
#        return DICT_CONF_VALS[list(args)[2]]
#
#    monkeypatch.setattr(
#        'foca.factories.celery_app.get_conf',
#        mock_get_conf
#    )
#
#    monkeypatch.setattr(
#        'foca.factories.celery_app.get_conf_type',
#        lambda *args, **kwargs: DICT_CONF_VALS['include']
#    )
#
#    monkeypatch.setattr(
#        'foca.factories.celery_app.get_conf_type',
#        lambda *args, **kwargs: DICT_CONF_VALS['include']
#    )
#
#    # Set the current Celery application
#    c = Celery('mycurrent')
#    c.set_current()
#
#    celery = create_celery_app(app)
#
#    @celery.task
#    def appctx():
#        return current_app.name
#
#    r = appctx.delay()
#    assert r.result == "myapp"
#