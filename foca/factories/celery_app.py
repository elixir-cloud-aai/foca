"""Factory for creating and configuring Celery application instances."""

import logging
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
from foca.models.config import PeriodicJobConfig

# Get logger instance
logger = logging.getLogger(__name__)


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
    :py:func:`foca.database.register_mongodb.reopen_mongodb`).

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

    db = config.db
    if db is not None:

        def reopen_client(**kwargs: Any) -> None:
            """Replace the app's MongoDB client around forks of a worker.

            Args:
                **kwargs: Arguments of the signal; ignored.
            """
            reopen_mongodb(db)

        # `worker_init`: before the worker forks its processes, so that it
        # forks without the client's threads; `worker_process_init`: in each
        # of them, as the worker may have used the client since (it replaces
        # processes that end, while it runs)
        uid = f"foca-reopen-mongodb-{id(db)}"
        worker_init.connect(reopen_client, weak=False, dispatch_uid=uid)
        worker_process_init.connect(reopen_client, weak=False, dispatch_uid=uid)
        logger.debug("MongoDB client set to be reopened around worker forks.")

    return celery
