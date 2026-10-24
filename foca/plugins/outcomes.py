"""Outcomes of forwarding attempts and internal errors of the engine."""

import re

MAX_ERROR_LENGTH = 500
"""Maximum length of error messages in records."""


_REASON = re.compile(r"[a-z0-9_]{1,64}")


# User info of URLs in error messages, which may hold credentials
_USER_INFO = re.compile(r"(//)[^/@\s]*@")


class NotAccepted(Exception):
    """The backend certainly did not accept the request; for apps' clients.

    Attributes:
        reason: Reason code, e.g., `connect_error`, `tls_error`,
            `not_public`, `no_credentials` or `http_<status>` (4xx, 3xx).
    """

    def __init__(self, reason: str, message: str = "") -> None:
        """Create the outcome.

        Args:
            reason: Reason code (`[a-z0-9_]{1,64}`).
            message: Description, for logs.

        Raises:
            ValueError: The reason code is invalid.
        """
        if not _REASON.fullmatch(reason):
            raise ValueError("Reason codes match [a-z0-9_]{1,64}")
        super().__init__(message or reason)
        self.reason = reason


class OutcomeUnknown(NotAccepted):
    """The backend may have accepted the request; for apps' clients.

    Raised, e.g., on read timeouts, lost connections after sending, and
    `5xx` statuses (reasons `read_timeout`, `connection_lost`,
    `http_<status>`); no other backend is then tried.
    """


def sanitize_error(exc: BaseException) -> str:
    """Describe an exception for records, without credentials.

    Args:
        exc: Exception.

    Returns:
        Type and message, with user info removed from URLs, at most
        `MAX_ERROR_LENGTH` characters.
    """
    message = _USER_INFO.sub(r"\1", f"{type(exc).__name__}: {exc}")
    return message[:MAX_ERROR_LENGTH]


class _DeadlineExceededError(Exception):
    """A hook did not return in time, or no worker was free."""


class _AppError(Exception):
    """The app's sanitation failed; its exception reaches the caller."""

    def __init__(self, error: Exception) -> None:
        """Wrap the app's exception.

        Args:
            error: Exception raised by the app.
        """
        super().__init__(str(error))
        self.error = error


class _NoEnabledPluginError(ValueError):
    """A step that may not be skipped has no enabled plugin."""


class _InvalidOutputError(Exception):
    """A hook produced invalid output."""


class _StepFailedError(Exception):
    """All alternatives of a step failed."""

    def __init__(self, step: str) -> None:
        """Create the error.

        Args:
            step: Identifier of the step.
        """
        super().__init__(step)
        self.step = step
