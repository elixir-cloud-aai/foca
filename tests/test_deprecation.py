"""Tests for `foca.deprecation`."""

import logging
import threading
import warnings
from collections.abc import Iterator

import pytest
from flask import Flask

from foca import deprecation
from foca.config.access import get_app_config
from foca.deprecation import warn_deprecated


@pytest.fixture(autouse=True)
def _forget_logged() -> Iterator[None]:
    """Forget which deprecations were logged, before and after each test."""
    deprecation._logged.clear()
    yield
    deprecation._logged.clear()


def _caller() -> None:
    """Call a deprecated API."""
    warn_deprecated("'old()' is deprecated; use 'new()'.")


class TestWarnDeprecated:
    """Tests for `warn_deprecated()`."""

    def test_warns_each_time(self) -> None:
        """The warning is emitted on each call."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _caller()
            _caller()
        assert [str(w.message) for w in caught] == [
            "'old()' is deprecated; use 'new()'."
        ] * 2
        assert all(w.category is DeprecationWarning for w in caught)

    def test_stack_level(self) -> None:
        """The warning points at the caller of the deprecated API."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _caller()  # this line
        assert caught[0].filename == __file__

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
        records = [r for r in caplog.records if r.name == deprecation.LOGGER_NAME]
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.WARNING, "'old()' is deprecated; use 'new()'."),
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
        records = [r for r in caplog.records if r.name == deprecation.LOGGER_NAME]
        assert len(records) == 1

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
        assert [r.getMessage() for r in caplog.records] == [
            "'old()' is deprecated; use 'new()'."
        ]

    def test_logging_failure_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing log handler doesn't fail the deprecated call."""

        def _fail(*args: object, **kwargs: object) -> None:
            """Fail."""
            raise RuntimeError

        monkeypatch.setattr(deprecation._logger, "warning", _fail)
        with pytest.warns(DeprecationWarning):
            _caller()

    def test_foca_deprecations_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """FOCA's own deprecations are logged."""
        with (
            caplog.at_level(logging.WARNING, logger=deprecation.LOGGER_NAME),
            pytest.warns(DeprecationWarning),
            pytest.raises(RuntimeError),
        ):
            get_app_config(Flask(__name__))
        assert [r.getMessage() for r in caplog.records] == [
            "'get_app_config()' is deprecated; use 'foca.get_config()'."
        ]
