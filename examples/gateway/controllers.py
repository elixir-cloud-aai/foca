"""Controllers of the gateway example app."""

import functools
import os
from typing import Any

from celery import Celery
from celery.exceptions import TimeoutError as ResultTimeout
from flask import current_app
from service import run_pipeline
from tasks import TASK_NAME

from foca.plugins.adapter import current_principal, inbound_from_request, request_id

# Seconds to wait for the result of the worker
WORKER_TIMEOUT = 30


@functools.cache
def _celery_client() -> Celery:
    """Get the client that sends tasks to the worker, once per process.

    Uses the same environment variables as the worker's Celery app (cf.
    :py:func:`foca.factories.celery_app.create_celery_app`). A bare client, as
    the app needs nothing else of a Celery app; created on first use, so after
    Gunicorn forked.

    Returns:
        Celery client.
    """
    jobs = current_app.config.foca.jobs
    return Celery(
        broker=os.environ.get(
            "CELERY_BROKER_URL", f"pyamqp://{jobs.host}:{jobs.port}//"
        ),
        backend=os.environ.get("CELERY_RESULT_BACKEND", jobs.backend),
        set_as_current=False,
    )


def addMessage(body: dict[str, Any]) -> dict[str, Any]:
    """Run a message through the plugin pipeline, in the app's process.

    Args:
        body: Message.

    Returns:
        Payload after the plugins, and the run's provenance record.
    """
    return run_pipeline(
        inbound_from_request(body), current_principal(), request_id=request_id()
    )


def addWorkerMessage(
    body: dict[str, Any],
) -> dict[str, Any] | tuple[dict[str, Any], int]:
    """Run a message through the plugin pipeline, in the Celery worker.

    Args:
        body: Message.

    Returns:
        Payload after the plugins, and the run's provenance record; or
        problem details and status 503 if the worker didn't answer in time,
        e.g., as it is still starting.
    """
    result = _celery_client().send_task(TASK_NAME, args=[body])
    try:
        data: dict[str, Any] = result.get(timeout=WORKER_TIMEOUT)
    except ResultTimeout:
        return {"title": "Service Unavailable", "status": 503}, 503
    return data
