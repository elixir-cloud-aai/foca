"""Deprecations of FOCA, as warnings and, once per process, in the logs.

Python hides `DeprecationWarning` outside of `__main__` by default, so apps
served by, e.g., Gunicorn never show them. FOCA therefore also logs each
deprecation once per process, at `WARNING`, on the logger
:py:data:`LOGGER_NAME`. Processes forked from one that logged a deprecation
(e.g., Gunicorn's workers) log it again.

The public API is :py:func:`warn_deprecated` and :py:data:`LOGGER_NAME`.
"""

import contextlib
import logging
import os
import threading
import warnings

LOGGER_NAME = "foca.deprecation"
"""Name of the logger of deprecations, e.g., to silence them in the logs."""

_logger = logging.getLogger(LOGGER_NAME)

# Deprecations logged in this process, by message
_logged: set[str] = set()
_lock = threading.Lock()


def _forget_logged() -> None:
    """Forget which deprecations were logged, e.g., in a forked process.

    The lock is replaced rather than taken, as another thread of the parent
    may have held it while forking.
    """
    global _lock  # noqa: PLW0603 (state of the process)
    _lock = threading.Lock()
    _logged.clear()


# Each process logs its deprecations, e.g., each Gunicorn worker, also those
# that its parent logged before forking (e.g., with `preload_app`)
if hasattr(os, "register_at_fork"):  # pragma: no branch (POSIX)
    os.register_at_fork(after_in_child=_forget_logged)


def warn_deprecated(message: str, stacklevel: int = 2) -> None:
    """Warn that an API is deprecated, and log it once per process.

    Emits a `DeprecationWarning` on each call. The first call with a message
    in a process also logs the message at `WARNING`, before the warning is
    emitted, so that it is logged also where warnings are errors. Logging
    never fails the call.

    Messages are logged as they are and remembered per process, so pass
    constant messages, never values of requests or other data.

    Args:
        message: Message, naming the deprecated API and its replacement.
        stacklevel: Stack level of the warning: 1 is the function calling
            `warn_deprecated()` (i.e., the deprecated API), 2, the default,
            its caller, and so on, as for `warnings.warn()` called by the
            deprecated API itself.
    """
    with _lock:
        first = message not in _logged
        _logged.add(message)
    if first:
        # A faulty filter or handler of the app must not fail the call
        with contextlib.suppress(Exception):
            _logger.warning(message)
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel + 1)
