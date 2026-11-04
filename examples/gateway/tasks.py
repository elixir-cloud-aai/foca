"""Celery tasks of the gateway example app."""

from typing import Any

from celery import shared_task
from service import run_pipeline

from foca.plugins import InboundRequest, Principal

TASK_NAME = "gateway_example.process_message"


@shared_task(name=TASK_NAME)
def process_message(body: dict[str, Any]) -> dict[str, Any]:
    """Run a message through the plugin pipeline, in the worker.

    Args:
        body: Message, as sent to the app.

    Returns:
        Payload after the plugins, and the run's provenance record.
    """
    inbound = InboundRequest(method="POST", path="/messages/worker", payload=body)
    return run_pipeline(inbound, Principal())
