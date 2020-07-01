"""Factory for creating Celery app instances based on Flask apps."""

from inspect import stack
import logging

from flask import Flask
from celery import Celery

# Get logger instance
logger = logging.getLogger(__name__)


def create_celery_app(app: Flask) -> Celery:
    """Create and configure Celery app.

    Args:
        app: Flask application.

    Returns:
        Celery application.
    """
    conf = app.config['FOCA'].jobs

    # Instantiate Celery app
    celery = Celery(
        app=__name__,
        broker=f"pyamqp://{conf.host}:{conf.port}//",
        backend=conf.backend,
        include=conf.include,
    )
    calling_module = ':'.join([stack()[1].filename, stack()[1].function])
    logger.debug(f"Celery app created from '{calling_module}'.")

    # Update Celery app configuration with Flask app configuration
    celery.conf['FOCA'] = app.config['FOCA']
    logger.debug('Celery app configured.')

    class ContextTask(celery.Task):  # type: ignore
        # https://github.com/python/mypy/issues/4284)
        """Create subclass of task that wraps task execution in application
        context.
        """
        def __call__(self, *args, **kwargs):
            """Wrap task execution in application context."""
            with app.app_context():  # pragma: no cover
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    logger.debug("App context added to 'celery.Task' class.")

    return celery
