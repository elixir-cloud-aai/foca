"""Deprecations of FOCA, as warnings and, once per process, in the logs.

Python hides `DeprecationWarning` outside of `__main__` by default, so apps
served by, e.g., Gunicorn never show them. FOCA therefore also logs each
deprecation once per process, at `WARNING`, on the logger
:py:data:`LOGGER_NAME`.
"""

import contextlib
import logging
import threading
import warnings

LOGGER_NAME = "foca.deprecation"
"""Name of the logger of deprecations, e.g., to silence them in the logs."""

_logger = logging.getLogger(LOGGER_NAME)

# Deprecations logged in this process, by message
_logged: set[str] = set()
_lock = threading.Lock()


def warn_deprecated(message: str, stacklevel: int = 2) -> None:
    """Warn that an API is deprecated, and log it once per process.

    Emits a `DeprecationWarning` on each call. The first call with a message
    in a process also logs the message at `WARNING`, before the warning is
    emitted, so that it is logged also where warnings are errors. Logging
    never fails the call.

    Args:
        message: Message, naming the deprecated API and its replacement.
        stacklevel: Stack level of the warning, as for `warnings.warn()`,
            counted from the caller of this function: by default, the warning
            points at the caller of the deprecated API that calls it.
    """
    with _lock:
        first = message not in _logged
        _logged.add(message)
    if first:
        # Logging must not fail the call
        with contextlib.suppress(Exception):
            _logger.warning(message)
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel + 1)
