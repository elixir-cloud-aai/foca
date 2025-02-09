"""Factory for creating and configuring Celery application instances."""

from inspect import stack
import logging

from flask import Flask
from celery import Celery

# Get logger instance
logger = logging.getLogger(__name__)


def create_celery_app(app: Flask) -> Celery:
    """Create and configure Celery application instance.

    Args:
        app: Flask application instance.

    Returns:
        Celery application instance.
    """
    conf = app.config.foca.jobs  # type: ignore[attr-defined]

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
    setattr(celery.conf, 'foca', app.config.foca)  # type: ignore[attr-defined]
    logger.debug('Celery app configured.')

    class ContextTask(celery.Task):  # type: ignore
        # https://github.com/python/mypy/issues/4284)
        """Class constructor for Celery tasks.

        Create subclass of task that wraps task execution in application
        context.
        """

        def __call__(self, *args, **kwargs):
            """Execute the task within the application context.

            This method wraps the task execution in the application's context,
            ensuring that the task has access to the application's resources
            and configurations.

            Args:
                args: Variable length argument list.
                kwargs: Arbitrary keyword arguments.

            Returns:
                The result of the task execution.
            """
            with app.app_context():  # pragma: no cover
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    logger.debug("App context added to 'celery.Task' class.")

    return celery
