"""Tests for `foca.deprecation`."""

import inspect
import logging
import os
import threading
import warnings
from collections.abc import Callable

import pytest
from flask import Flask

from foca import deprecation
from foca.config.access import get_app_config
from foca.deprecation import warn_deprecated
from foca.errors import Unauthorized
from foca.security.access_control.access_control_server import _MISSING, _get_body

MESSAGE = "'old()' is deprecated; use 'new()'."


def _caller(stacklevel: int = 2) -> None:
    """Call a deprecated API.

    Args:
        stacklevel: Stack level of the warning.
    """
    warn_deprecated(MESSAGE, stacklevel=stacklevel)


def _indirect_caller() -> None:
    """Call a deprecated API via a helper, as `_warn_www_authenticate()`."""
    _caller(stacklevel=3)


def _records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """Get the records of the logger of deprecations.

    Args:
        caplog: Captured log records.

    Returns:
        Levels and messages of the records.
    """
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == deprecation.LOGGER_NAME
    ]


class TestWarnDeprecated:
    """Tests for `warn_deprecated()`."""

    def test_warns_each_time(self) -> None:
        """The warning is emitted on each call."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _caller()
            _caller()
        assert [str(w.message) for w in caught] == [MESSAGE] * 2
        assert all(w.category is DeprecationWarning for w in caught)

    @pytest.mark.parametrize("call", [_caller, _indirect_caller])
    def test_stack_level(self, call: Callable[[], None]) -> None:
        """The warning points at the caller of the deprecated API."""
        frame = inspect.currentframe()
        assert frame is not None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            line = frame.f_lineno + 1
            call()
        assert (caught[0].filename, caught[0].lineno) == (__file__, line)

    def test_logs_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """Each deprecation is logged once per process, at WARNING."""
        with (
            caplog.at_level(logging.WARNING, logger=deprecation.LOGGER_NAME),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore")
            _caller()
            _caller()
            warn_deprecated("'other()' is deprecated.")
        assert _records(caplog) == [
            (logging.WARNING, MESSAGE),
            (logging.WARNING, "'other()' is deprecated."),
        ]

    def test_logs_once_across_threads(self, caplog: pytest.LogCaptureFixture) -> None:
        """Concurrent calls log a deprecation once."""
        barrier = threading.Barrier(8)

        def _call() -> None:
            """Call the deprecated API with the other threads."""
            barrier.wait()
            _caller()

        with (
            caplog.at_level(logging.WARNING, logger=deprecation.LOGGER_NAME),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore")
            threads = [threading.Thread(target=_call) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert len(_records(caplog)) == 1

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork()")
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_logs_again_after_fork(self) -> None:
        """Forked processes, e.g., Gunicorn's workers, log deprecations again."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _caller()
        assert MESSAGE in deprecation._logged
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover (child; reports via the pipe)
            try:
                records: list[logging.LogRecord] = []
                handler = logging.Handler()
                handler.emit = records.append  # type: ignore[method-assign,assignment]
                logging.getLogger(deprecation.LOGGER_NAME).addHandler(handler)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    _caller()
                    _caller()
                os.write(write, str(len(records)).encode())
            finally:
                os._exit(0)
        os.close(write)
        logged_in_child = os.read(read, 8)
        os.close(read)
        os.waitpid(pid, 0)
        assert logged_in_child == b"1"
        assert MESSAGE in deprecation._logged

    def test_logs_again_after_failure(self) -> None:
        """A deprecation whose logging failed is logged on a later call."""

        def _fail(record: logging.LogRecord) -> bool:
            """Fail on filtering.

            Args:
                record: Log record.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError

        logger = logging.getLogger(deprecation.LOGGER_NAME)
        logger.addFilter(_fail)
        try:
            with pytest.warns(DeprecationWarning):
                _caller()
        finally:
            logger.removeFilter(_fail)
        assert MESSAGE not in deprecation._logged

    def test_logged_when_warnings_are_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The deprecation is logged before the warning is raised as an error."""
        with (
            caplog.at_level(logging.WARNING, logger=deprecation.LOGGER_NAME),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("error")
            with pytest.raises(DeprecationWarning):
                _caller()
        assert _records(caplog) == [(logging.WARNING, MESSAGE)]

    def test_logging_failure_ignored(self) -> None:
        """A failing log filter doesn't fail the deprecated call."""

        def _fail(record: logging.LogRecord) -> bool:
            """Fail on filtering.

            Args:
                record: Log record.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError

        logger = logging.getLogger(deprecation.LOGGER_NAME)
        logger.addFilter(_fail)
        try:
            with pytest.warns(DeprecationWarning):
                _caller()
        finally:
            logger.removeFilter(_fail)


def _get_app_config() -> None:
    """Call `get_app_config()` for an app without configuration."""
    with pytest.raises(RuntimeError):
        get_app_config(Flask(__name__))


def _www_authenticate() -> None:
    """Pass challenges as `www_authenticate`."""
    Unauthorized(www_authenticate="Bearer")


def _permission_body() -> None:
    """Pass the body of the permissions API as `permission`."""
    _get_body(_MISSING, {"rule": []})


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (_get_app_config, "'get_app_config()' is deprecated"),
        (_www_authenticate, "'www_authenticate' of 'Unauthorized' is deprecated"),
        (_permission_body, "The request body of the permissions API as 'permission'"),
    ],
)
def test_foca_deprecations_logged(
    caplog: pytest.LogCaptureFixture, call: Callable[[], None], message: str
) -> None:
    """FOCA's own deprecations are logged."""
    with (
        caplog.at_level(logging.WARNING, logger=deprecation.LOGGER_NAME),
        pytest.warns(DeprecationWarning),
    ):
        call()
    logged = _records(caplog)
    assert len(logged) == 1
    assert logged[0][1].startswith(message)
