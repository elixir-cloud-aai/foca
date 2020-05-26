from foca.factories.celery_app import create_celery_app
from celery import Celery
from flask import Flask


MOCK_HOST = "mock_host"
MOCK_PORT = "mock_port"
DICT_CONF_VALS = {
    'broker_host': "broker_host",
    'broker_port': "broker_port",
    'result_backend': "result_backend",
    'include': "include",
    'message_maxsize': "message_maxsize"
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
    demo_app.config['BROKER_URL'] = 'sqla+sqlite:///celerydb.sqlite'
    demo_app.config['CELERY_RESULT_BACKEND'] = 'db+sqlite:///results.sqlite'
    demo_app.config['celery'] = 'celery'
    res_app = create_celery_app(demo_app)
    celery_app_ret_type = type(Celery(app=__name__))

    assert type(res_app) == celery_app_ret_type
