"""Controllers of the gateway example app."""

import os
from typing import Any

from celery import Celery
from flask import current_app
from service import run_pipeline
from tasks import TASK_NAME

from foca.plugins.adapter import current_principal, inbound_from_request, request_id

# Seconds to wait for the result of the worker
WORKER_TIMEOUT = 30


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


def addWorkerMessage(body: dict[str, Any]) -> dict[str, Any]:
    """Run a message through the plugin pipeline, in the Celery worker.

    Args:
        body: Message.

    Returns:
        Payload after the plugins, and the run's provenance record.
    """
    jobs = current_app.config.foca.jobs
    broker = os.environ.get("CELERY_BROKER_URL", f"pyamqp://{jobs.host}:{jobs.port}//")
    with Celery(broker=broker, backend=jobs.backend) as client:
        result = client.send_task(TASK_NAME, args=[body])
        data: dict[str, Any] = result.get(timeout=WORKER_TIMEOUT)
    return data
