"""Errors of gateway requests, with problem details for clients."""

from typing import Any

from foca.plugins.base import Rejected

PROBLEM_TYPE_BASE = "https://foca.readthedocs.io/en/latest/plugins/#"
"""Base of the problem types of gateway errors."""


class GatewayError(Exception):
    """Error of a gateway request, with problem details for the client.

    Attributes:
        code: HTTP status of the response to the client.
        title: Title of the problem.
        detail: Description of the problem, for the client.
        problem_type: URI of the problem type.
        run: Run of the request (:py:class:`foca.plugins.engine.PipelineRun`),
            e.g., for provenance; `None` before it started.
    """

    code = 500
    title = "Internal Server Error"
    slug = "gateway-error"

    def __init__(self, detail: str, run: Any = None) -> None:
        """Create the error.

        Args:
            detail: Description of the problem, for the client.
            run: Run of the request, if started.
        """
        super().__init__(detail)
        self.detail = detail
        self.run = run
        self.problem_type = f"{PROBLEM_TYPE_BASE}{self.slug}"


class PipelineRejected(GatewayError):
    """A plugin rejected the request (cf. :py:class:`foca.plugins.Rejected`)."""

    slug = "rejected"

    def __init__(self, rejection: Rejected, step: str, run: Any = None) -> None:
        """Create the error.

        Args:
            rejection: Rejection raised by the plugin.
            step: Identifier of the step.
            run: Run of the request.
        """
        super().__init__(rejection.detail, run=run)
        self.code = rejection.status
        self.title = rejection.title or _phrase(rejection.status)
        self.step = step


class PipelineFailed(GatewayError):
    """All alternatives of a required step failed."""

    title = "Internal Server Error"
    slug = "plugin-step-failed"

    def __init__(self, step: str, run: Any = None) -> None:
        """Create the error.

        Args:
            step: Identifier of the step.
            run: Run of the request.
        """
        super().__init__(f"Plugin step failed: {step}", run=run)
        self.step = step


class NoBackendAvailable(GatewayError):
    """No backend accepted the request."""

    code = 503
    title = "Service Unavailable"
    slug = "no-backend-available"


class ForwardTimeout(GatewayError):
    """The forwarding attempts of the request took too long."""

    code = 503
    title = "Service Unavailable"
    slug = "forward-timeout"


class LoopDetected(GatewayError):
    """The request already passed this gateway (`Via` header)."""

    code = 508
    title = "Loop Detected"
    slug = "loop-detected"


class InvalidVia(GatewayError):
    """The `Via` header of the request is too long."""

    code = 400
    title = "Bad Request"
    slug = "invalid-via"


def _phrase(status: int) -> str:
    """Get the reason phrase of a rejection status.

    Args:
        status: HTTP status, one of :py:data:`foca.plugins.base.REJECTION_STATUSES`.

    Returns:
        Reason phrase as per RFC 9110.
    """
    return {
        400: "Bad Request",
        403: "Forbidden",
        409: "Conflict",
        413: "Content Too Large",
        422: "Unprocessable Content",
    }[status]


class InvalidSelection(GatewayError):
    """The client's choice of pipeline or backends is invalid or not allowed."""

    code = 400
    title = "Bad Request"

    def __init__(self, detail: str, slug: str = "invalid-pipeline") -> None:
        """Create the error.

        Args:
            detail: Description of the problem, for the client.
            slug: Last part of the problem type: `invalid-pipeline`,
                `invalid-backends` or `reserved-tag`.
        """
        self.slug = slug
        super().__init__(detail)
