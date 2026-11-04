"""Gateway settings and request handling of the gateway example app."""

from typing import Any

from foca.plugins import Backend, BackendResponse, InboundRequest, Principal
from foca.plugins.runtime import GatewayApp, get_host

# Every `Foca(gateway=...)` of a process must pass the same settings, so app
# and worker import them from here
GATEWAY = GatewayApp(api="tes", name="gateway-example", version="1.0.0")

# The example forwards to no real backend: it reports the attempt as accepted,
# so that the example needs no other service
BACKEND = Backend(id="example", url="https://backend.example.org")


def run_pipeline(
    inbound: InboundRequest,
    principal: Principal,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run a request through all phases of the plugin pipeline.

    Args:
        inbound: Inbound request.
        principal: Caller.
        request_id: Identifier of the request.

    Returns:
        Payload after the plugins, and the run's provenance record.
    """
    run = get_host().start(inbound, principal, request_id=request_id)
    run.request()
    for attempt in run.attempts(run.route([BACKEND])):
        attempt.succeeded(BackendResponse(status=200), remote_id="example")
        break
    return {"payload": run.inbound.payload, "provenance": run.provenance()}
