"""Factory for creating and configuring Celery application instances."""

import logging
import weakref
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from celery import Celery
from celery.schedules import ParseException, crontab
from celery.signals import worker_init, worker_process_init
from flask import Flask

from foca.config.access import _get_app_config
from foca.config.environment import get_env
from foca.database.register_mongodb import reopen_mongodb
from foca.deprecation import warn_deprecated
from foca.models.config import MongoConfig, PeriodicJobConfig

# Get logger instance
logger = logging.getLogger(__name__)

_REOPEN_UID = "foca-reopen-mongodb"
"""Dispatch UID of the handler that reopens MongoDB clients around forks."""

# By `id()`, as configurations are unhashable and compared by value
_databases: "weakref.WeakValueDictionary[int, MongoConfig]" = (
    weakref.WeakValueDictionary()
)
"""Database configurations of the Celery apps, by `id()`, while alive."""


def _reopen_clients(**kwargs: Any) -> None:
    """Replace the MongoDB clients of the Celery apps around worker forks.

    Connected once, to Celery's worker signals, and kept alive deliberately;
    it holds the configurations weakly, so that they and their clients are
    released with their apps (e.g., in tests that create many apps).

    An error with one configuration is logged, and the others are still
    reopened, as with a handler per configuration (cf. `Signal.send`).

    Args:
        **kwargs: Arguments of the signal; ignored.
    """
    for db in list(_databases.values()):
        try:
            reopen_mongodb(db)
        except Exception:
            logger.exception(
                "MongoDB client of %s:%s could not be reopened.", db.host, db.port
            )


def _reopen_around_forks(db: MongoConfig) -> None:
    """Have workers replace the MongoDB client of a configuration.

    Args:
        db: Database configuration; held weakly (cf. :py:func:`_reopen_clients`).
    """
    _databases[id(db)] = db
    # `worker_init`: before the worker forks its processes, so that it forks
    # without the clients' threads; `worker_process_init`: in each of them,
    # as the worker may have used the clients since (it replaces processes
    # that end, while it runs); connected once (cf. `dispatch_uid`)
    worker_init.connect(_reopen_clients, weak=False, dispatch_uid=_REOPEN_UID)
    worker_process_init.connect(_reopen_clients, weak=False, dispatch_uid=_REOPEN_UID)


def _redact_url(url: str) -> str:
    """Remove credentials and query from a URL, for logging.

    Args:
        url: URL, e.g., of a message broker.

    Returns:
        URL without user info and query.
    """
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _get_beat_entry(name: str, job: PeriodicJobConfig) -> dict[str, Any]:
    """Get the entry of a periodic job for Celery's `beat_schedule`.

    Args:
        name: Name of the job.
        job: Periodic job.

    Returns:
        Entry of the job.

    Raises:
        ValueError: Its `cron` isn't valid for `celery.schedules.crontab`, or
            never occurs (e.g., 31 February), which would stop Celery beat.
    """
    schedule: Any = job.every
    if job.cron is not None:
        try:
            schedule = crontab(**job.cron.model_dump())
            schedule.remaining_estimate(datetime.now(UTC))
        except (ParseException, ValueError, RuntimeError) as exc:
            raise ValueError(
                f"Invalid 'cron' of periodic job '{name}' ('jobs.periodic'): {exc}"
            ) from exc
    return {
        "task": job.task,
        "schedule": schedule,
        "args": job.args,
        "kwargs": job.kwargs,
        "options": job.options,
    }


def create_celery_app(app: Flask) -> Celery:
    """Create and configure Celery application instance for a Flask app.

    Deprecated, removed in the ASGI release: use
    :py:meth:`foca.Foca.create_celery_app`.

    Args:
        app: Flask application instance.

    Returns:
        Celery application instance.
    """
    warn_deprecated(
        "'foca.factories.celery_app.create_celery_app()' is deprecated and "
        "removed in the ASGI release; use 'Foca.create_celery_app()'.",
        stacklevel=2,
    )
    return _create_celery_app(app)


def _create_celery_app(app: Flask) -> Celery:
    """Create and configure Celery application instance.

    For FOCA's own use.

    The message broker URL is taken from environment variable
    `CELERY_BROKER_URL`, if set (e.g., with credentials or for
    `amqps://`), otherwise built from `jobs.host` and `jobs.port`. The
    result backend is taken from environment variable
    `CELERY_RESULT_BACKEND`, if set, otherwise from `jobs.backend`. Both
    variables can come from files (cf. :py:func:`foca.config.environment.get_env`).

    If the app has a database, a worker replaces its MongoDB client before it
    forks its processes, and each process after its fork, as clients must not
    be used across forks (cf.
    :py:func:`foca.database.register_mongodb.reopen_mongodb`); the worker's
    signals don't keep the database configuration alive.

    Jobs of `jobs.periodic` are set as the schedule of Celery beat.

    Args:
        app: Flask application instance.

    Returns:
        Celery application instance.
    """
    config = _get_app_config(app)
    conf = config.jobs
    if conf is None:
        raise ValueError("No background task support configured")
    broker = get_env("CELERY_BROKER_URL", f"pyamqp://{conf.host}:{conf.port}//")
    backend = get_env("CELERY_RESULT_BACKEND", conf.backend.get_secret_value())

    # Instantiate Celery app
    celery = Celery(
        app=__name__,
        broker=broker,
        backend=backend,
        include=conf.include,
    )
    logger.info(
        "Celery app created with broker '%s' and result backend '%s'.",
        _redact_url(broker),
        _redact_url(backend),
    )

    # Update Celery app configuration with Flask app configuration
    setattr(celery.conf, "foca", config)
    celery.conf.beat_schedule = {
        name: _get_beat_entry(name, job) for name, job in conf.periodic.items()
    }
    logger.debug("Celery app configured.")

    # Celery's task base class is created dynamically
    # Celery creates the task base class at runtime
    class ContextTask(celery.Task):  # type: ignore[name-defined]
        # https://github.com/python/mypy/issues/4284)
        """Create task subclass that executes tasks in app context."""

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            """Wrap task execution in application context."""
            with app.app_context():  # pragma: no cover
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    logger.debug("App context added to 'celery.Task' class.")

    if config.db is not None:
        _reopen_around_forks(config.db)
        logger.debug("MongoDB client set to be reopened around worker forks.")

    return celery
