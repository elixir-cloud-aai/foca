"""Unit tests for Celery app factory module."""

from unittest.mock import MagicMock

import pytest
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_init, worker_process_init
from pydantic import ValidationError

from foca.factories import celery_app as module
from foca.factories.celery_app import _create_celery_app, create_celery_app
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import Config, JobsConfig, MongoConfig, PeriodicJobConfig

CONFIG = Config()
CONFIG.jobs = JobsConfig()


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Messages logged via `logger.info()` of the Celery factory module."""
    for var in ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"]:
        monkeypatch.delenv(var, raising=False)
    messages: list[str] = []

    def info(msg: str, *args: object) -> None:
        messages.append(msg % args)

    monkeypatch.setattr(module.logger, "info", info)
    return messages


def test_create_celery_app(logged: list[str]) -> None:
    """Test Connexion app creation."""
    cnx_app = create_connexion_app(CONFIG)
    cel_app = _create_celery_app(cnx_app.app)
    assert isinstance(cel_app, Celery)
    assert cel_app.conf.foca == CONFIG
    assert cel_app.conf.broker_url == "pyamqp://rabbitmq:5672//"
    assert cel_app.conf.result_backend == "rpc://"


def test_broker_env_vars(logged: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Broker and backend URLs from environment variables are used.

    They are not logged with credentials.
    """
    broker = "amqps://user:secret@rabbitmq.example.org:5671/vhost?ssl=1"
    backend = "redis://:secret@redis:6379/0"
    monkeypatch.setenv("CELERY_BROKER_URL", broker)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", backend)
    cnx_app = create_connexion_app(CONFIG)
    cel_app = _create_celery_app(cnx_app.app)
    assert cel_app.conf.broker_url == broker
    assert cel_app.conf.result_backend == backend
    assert "secret" not in " ".join(logged)
    assert (
        "Celery app created with broker "
        "'amqps://rabbitmq.example.org:5671/vhost' and result backend "
        "'redis://redis:6379/0'."
    ) in logged


def test_create_celery_app_deprecated(logged: list[str]) -> None:
    """Celery apps are created from Flask apps, deprecated."""
    cnx_app = create_connexion_app(CONFIG)
    with pytest.deprecated_call(match="Foca.create_celery_app"):
        cel_app = create_celery_app(cnx_app.app)
    assert isinstance(cel_app, Celery)


def test_create_celery_app_without_jobs(logged: list[str]) -> None:
    """Background tasks need to be configured."""
    cnx_app = create_connexion_app(Config())
    with pytest.raises(ValueError):
        _create_celery_app(cnx_app.app)


def test_worker_signals_reopen_mongodb(
    logged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers replace the client before forks, and processes after them."""
    reopen = MagicMock()
    monkeypatch.setattr(module, "reopen_mongodb", reopen)
    config = Config(jobs=JobsConfig(), db=MongoConfig())
    _create_celery_app(create_connexion_app(config).app)
    for signal in (worker_init, worker_process_init):
        reopen.reset_mock()
        signal.send(sender=None)
        reopen.assert_any_call(config.db)


def test_no_database_no_signals(
    logged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers of apps without a database have no client to replace."""
    connected = MagicMock()
    monkeypatch.setattr(worker_init, "connect", connected)
    monkeypatch.setattr(worker_process_init, "connect", connected)
    _create_celery_app(create_connexion_app(CONFIG).app)
    connected.assert_not_called()


def test_worker_signals_reopen_once_per_config(
    logged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating the Celery app of a configuration again adds no handler."""
    reopen = MagicMock()
    monkeypatch.setattr(module, "reopen_mongodb", reopen)
    config = Config(jobs=JobsConfig(), db=MongoConfig())
    app = create_connexion_app(config).app
    _create_celery_app(app)
    _create_celery_app(app)
    worker_process_init.send(sender=None)
    calls = [call for call in reopen.call_args_list if call.args[0] is config.db]
    assert len(calls) == 1


def test_periodic_jobs(logged: list[str]) -> None:
    """Periodic jobs are set as the schedule of Celery beat."""
    config = Config()
    config.jobs = JobsConfig.model_validate(
        {
            "periodic": {
                "often": {"task": "a.b", "every": 60, "args": [1]},
                "nightly": {
                    "task": "a.c",
                    "cron": {"minute": "0", "hour": "3"},
                    "kwargs": {"d": 1},
                    "options": {"expires": 5},
                },
            }
        }
    )
    schedule = _create_celery_app(create_connexion_app(config).app).conf.beat_schedule
    assert schedule["often"] == {
        "task": "a.b",
        "schedule": 60.0,
        "args": [1],
        "kwargs": {},
        "options": {},
    }
    assert schedule["nightly"]["schedule"] == crontab(minute="0", hour="3")
    assert schedule["nightly"]["kwargs"] == {"d": 1}
    assert schedule["nightly"]["options"] == {"expires": 5}


def test_no_periodic_jobs(logged: list[str]) -> None:
    """Without periodic jobs, the schedule is empty."""
    assert _create_celery_app(create_connexion_app(CONFIG).app).conf.beat_schedule == {}


@pytest.mark.parametrize(
    "cron",
    [
        {"hour": "25"},
        {"minute": "0,"},
        {"minute": "-1"},
        {"day_of_month": "31", "month_of_year": "2"},
    ],
)
def test_periodic_job_invalid_cron(logged: list[str], cron: dict[str, str]) -> None:
    """Fields that aren't valid or never occur stop the worker, naming the job."""
    config = Config()
    config.jobs = JobsConfig.model_validate(
        {"periodic": {"my-job": {"task": "a.b", "cron": cron}}}
    )
    with pytest.raises(ValueError, match="'my-job'"):
        _create_celery_app(create_connexion_app(config).app)


def test_periodic_job_numbers_in_cron() -> None:
    """Numbers, as YAML has them, are fine in fields."""
    job = PeriodicJobConfig.model_validate(
        {"task": "a.b", "cron": {"minute": 0, "hour": 3}}
    )
    assert job.cron is not None
    assert job.cron.hour == "3"


@pytest.mark.parametrize(
    "job",
    [
        {"task": "a.b"},
        {"task": "a.b", "every": 1, "cron": {"hour": "3"}},
        {"task": "a.b", "every": 0},
        {"task": "a.b", "every": True},
        {"task": "a.b", "every": float("inf")},
        {"task": "a.b", "every": 1e30},
        {"task": "", "every": 1},
        {"task": "a.b", "cron": {"hours": "3"}},
    ],
)
def test_periodic_job_invalid(job: dict[str, object]) -> None:
    """Jobs need a task and exactly one valid schedule."""
    with pytest.raises(ValidationError):
        PeriodicJobConfig.model_validate(job)
