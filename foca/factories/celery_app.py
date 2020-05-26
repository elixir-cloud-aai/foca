"""Factory for creating Celery app instances based on Flask apps."""

import os

from inspect import stack
import logging

from flask import Flask
from celery import Celery

from foca.config.config_parser import (get_conf, get_conf_type)


# Get logger instance
logger = logging.getLogger(__name__)


def create_celery_app(app: Flask) -> Celery:
    """Creates Celery application and configures it from Flask app."""
    broker = 'pyamqp://{host}:{port}//'.format(
        host=os.environ.get(
            'RABBIT_HOST',
            get_conf(app.config, 'celery', 'broker_host')
            ),
        port=os.environ.get(
            'RABBIT_PORT',
            get_conf(app.config, 'celery', 'broker_port')
            ),
    )
    backend = get_conf(app.config, 'celery', 'result_backend')
    include = get_conf_type(app.config, 'celery', 'include', types=(list))
    maxsize = get_conf(app.config, 'celery', 'message_maxsize')

    # Instantiate Celery app
    celery = Celery(
        app=__name__,
        broker=broker,
        backend=backend,
        include=include,
    )
    logger.info("Celery app created from '{calling_module}'.".format(
        calling_module=':'.join([stack()[1].filename, stack()[1].function])
    ))

    # Set Celery options
    celery.Task.resultrepr_maxsize = maxsize
    celery.amqp.argsrepr_maxsize = maxsize
    celery.amqp.kwargsrepr_maxsize = maxsize

    # Update Celery app configuration with Flask app configuration
    celery.conf.update(app.config)
    logger.info('Celery app configured.')

    class ContextTask(celery.Task):  # type: ignore
        # https://github.com/python/mypy/issues/4284)
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    logger.debug("App context added to 'celery.Task' class.")

    return celery
