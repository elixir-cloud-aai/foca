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
    NotAccepted,
    OutcomeUnknown,
    PipelineRun,
    PluginHost,
)
from foca.plugins.errors import (
    ForwardTimeout,
    GatewayError,
    InvalidSelection,
    LoopDetected,
    NoBackendAvailable,
    PipelineFailed,
    PipelineRejected,
)
from foca.plugins.model import (
    Backend,
    BackendResponse,
    FrozenMapping,
    Headers,
    InboundRequest,
    Location,
    OutboundRequest,
    Principal,
)
from foca.plugins.provenance import ProvenanceStore

__all__ = [
    "API_VERSION",
    "PHASES",
    "Attempt",
    "Backend",
    "BackendResponse",
    "Context",
    "ForwardTimeout",
    "FrozenMapping",
    "GatewayError",
    "Headers",
    "InboundRequest",
    "InvalidSelection",
    "InvalidVia",
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
    "ProvenanceStore",
    "Rejected",
]
