"""Factory for creating and configuring Celery application instances."""

import logging
import os
from urllib.parse import urlsplit, urlunsplit

from celery import Celery
from flask import Flask

from foca.config.access import get_app_config

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


def create_celery_app(app: Flask) -> Celery:
    """Create and configure Celery application instance.

    The message broker URL is taken from environment variable
    `CELERY_BROKER_URL`, if set (e.g., with credentials or for
    `amqps://`), otherwise built from `jobs.host` and `jobs.port`. The
    result backend is taken from environment variable
    `CELERY_RESULT_BACKEND`, if set, otherwise from `jobs.backend`.

    Args:
        app: Flask application instance.

    Returns:
        Celery application instance.
    """
    config = get_app_config(app)
    conf = config.jobs
    if conf is None:
        raise ValueError("No background task support configured")
    broker = os.environ.get("CELERY_BROKER_URL", f"pyamqp://{conf.host}:{conf.port}//")
    backend = os.environ.get("CELERY_RESULT_BACKEND", conf.backend)

    # Instantiate Celery app
    celery = Celery(
        app=__name__,
        broker=broker,
        backend=backend,
        include=conf.include,
    )
    logger.info(
        f"Celery app created with broker '{_redact_url(broker)}' and result "
        f"backend '{_redact_url(backend)}'."
    )

    # Update Celery app configuration with Flask app configuration
    setattr(celery.conf, "foca", config)
    logger.debug("Celery app configured.")

    # Celery's task base class is created dynamically
    # Celery creates the task base class at runtime
    class ContextTask(celery.Task):  # type: ignore[name-defined]
        # https://github.com/python/mypy/issues/4284)
        """Create task subclass that executes tasks in app context."""

        def __call__(self, *args, **kwargs):
            """Wrap task execution in application context."""
            with app.app_context():  # pragma: no cover
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    logger.debug("App context added to 'celery.Task' class.")

    return celery
