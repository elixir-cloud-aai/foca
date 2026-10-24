"""Gateway plugins: installable request processing for gateway apps.

Framework-neutral: plugins get plain data (cf. :py:mod:`foca.plugins.model`)
and an explicit context, so the same plugin code runs on every web
framework that FOCA supports. Only :py:mod:`foca.plugins.adapter` uses the
web framework.
"""

from foca.plugins.base import (
    API_VERSION,
    PHASES,
    NotApplicable,
    Plugin,
    PluginError,
    Rejected,
)
from foca.plugins.context import Context
from foca.plugins.engine import (
    Attempt,
    ForwardTimeout,
    GatewayError,
    LoopDetected,
    NoBackendAvailable,
    NotAccepted,
    OutcomeUnknown,
    PipelineFailed,
    PipelineRejected,
    PipelineRun,
    PluginHost,
)
from foca.plugins.model import (
    Backend,
    BackendResponse,
    Headers,
    InboundRequest,
    Location,
    OutboundRequest,
    Principal,
)

__all__ = [
    "API_VERSION",
    "PHASES",
    "Attempt",
    "Backend",
    "BackendResponse",
    "Context",
    "ForwardTimeout",
    "GatewayError",
    "Headers",
    "InboundRequest",
    "Location",
    "LoopDetected",
    "NoBackendAvailable",
    "NotAccepted",
    "NotApplicable",
    "OutboundRequest",
    "OutcomeUnknown",
    "PipelineFailed",
    "PipelineRejected",
    "PipelineRun",
    "Plugin",
    "PluginError",
    "PluginHost",
    "Principal",
    "Rejected",
]
