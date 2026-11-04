"""Tests for the context of log records and JSON logs."""

import io
import json
import logging
import sys
from collections.abc import Iterator
from logging.config import dictConfig
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from flask import Flask
from pydantic import ValidationError

from foca import context
from foca.config.config_parser import ConfigParser
from foca.context import RequestIdMiddleware
from foca.models.config import (
    LogConfig,
    LogFilterConfig,
    LogLoggerConfig,
)
from foca.security.access_control import enforcement
from foca.utils import logging as module
from foca.utils.logging import ContextFilter, JsonFormatter, escape_log_value
from foca.utils.urls import _register_api, get_operation_id

CONTEXT_FIELDS = ("request_id", "operation_id", "subject", "client")


def _record(msg: str = "hello", **extra: Any) -> logging.LogRecord:
    """Create a log record.

    Args:
        msg: Message.
        **extra: Further attributes.

    Returns:
        Log record.
    """
    return logging.makeLogRecord({"msg": msg, "name": "my.logger", **extra})


def _format(record: logging.LogRecord) -> dict[str, Any]:
    """Format a log record as JSON and parse it.

    Args:
        record: Log record.

    Returns:
        Parsed object.
    """
    line = JsonFormatter().format(record)
    assert "\n" not in line
    parsed: dict[str, Any] = json.loads(line)
    return parsed


class TestContextFilter:
    """Tests for `ContextFilter`."""

    def test_outside_of_requests(self) -> None:
        """Outside of requests, the fields are empty, and nothing fails."""
        record = _record()
        assert ContextFilter().filter(record) is True
        for field in CONTEXT_FIELDS:
            assert getattr(record, field) == ""

    def test_celery_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Celery tasks use the identifier of the task."""
        task = MagicMock()
        task.request.id = "task-1"
        monkeypatch.setattr(context, "current_task", task)
        record = _record()
        ContextFilter().filter(record)
        assert vars(record)["request_id"] == "task-1"

    def test_in_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In requests, the record has the fields of the request."""
        monkeypatch.setattr(enforcement, "get_current_subject", lambda: "alice")
        monkeypatch.setattr(enforcement, "_get_claims", lambda: {"azp": "client-1"})
        app = Flask(__name__)
        records = []

        @app.route("/", endpoint="/api.my_app_getThings")
        def _view() -> str:
            """Keep a record."""
            record = _record()
            ContextFilter().filter(record)
            records.append(record)
            return ""

        _register_api(app, "/api", [{"operationId": "my_app.getThings"}])
        app.wsgi_app = RequestIdMiddleware(app.wsgi_app)  # type: ignore[method-assign]
        app.test_client().get("/", headers={"X-Request-ID": "abc-1"})
        assert vars(records[0])["request_id"] == "abc-1"
        assert vars(records[0])["operation_id"] == "my_app.getThings"
        assert vars(records[0])["subject"] == "alice"
        assert vars(records[0])["client"] == "client-1"

    def test_values_escaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values of tokens can't forge log lines."""
        monkeypatch.setattr(enforcement, "get_current_subject", lambda: "a\nb")
        monkeypatch.setattr(enforcement, "_get_claims", lambda: {"azp": "c\r\nd"})
        with Flask(__name__).test_request_context():
            record = _record()
            ContextFilter().filter(record)
        assert vars(record)["subject"] == "a\\nb"
        assert vars(record)["client"] == "c\\r\\nd"

    def test_failure_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Failing to get a value doesn't fail logging."""

        def _fail() -> None:
            """Fail."""
            raise RuntimeError

        monkeypatch.setattr(enforcement, "get_current_subject", _fail)
        with Flask(__name__).test_request_context():
            record = _record()
            assert ContextFilter().filter(record) is True
        assert vars(record)["subject"] == ""

    def test_without_foca_config(self) -> None:
        """Apps without FOCA's configuration can log, too."""
        with Flask(__name__).test_request_context():
            record = _record()
            assert ContextFilter().filter(record) is True
        assert vars(record)["subject"] == ""


class TestGetOperationId:
    """Tests for `get_operation_id()`."""

    def test_outside_of_requests(self) -> None:
        """Outside of requests, it's empty."""
        assert get_operation_id() == ""

    def test_no_route(self) -> None:
        """Requests without a route have none."""
        with Flask(__name__).test_request_context("/missing"):
            assert get_operation_id() == ""

    def test_unknown_operation(self) -> None:
        """Other endpoints go by their names."""
        app = Flask(__name__)
        app.add_url_rule("/", "/api.other", lambda: "")
        with app.test_request_context("/"):
            assert get_operation_id() == "other"


class TestJsonFormatter:
    """Tests for `JsonFormatter`."""

    def test_fields(self) -> None:
        """Objects have the fields, also without the filter."""
        record = _record("hello %s", args=("you",), levelname="WARNING", created=0.5)
        data = _format(record)
        assert data == {
            "timestamp": "1970-01-01T00:00:00.500000+00:00",
            "level": "WARNING",
            "logger": "my.logger",
            "message": "hello you",
            "request_id": "",
            "operation_id": "",
            "subject": "",
            "client": "",
        }

    def test_fields_of_filter(self) -> None:
        """What the filter added is used."""
        record = _record()
        for field in CONTEXT_FIELDS:
            setattr(record, field, f"my-{field}")
        data = _format(record)
        for field in CONTEXT_FIELDS:
            assert data[field] == f"my-{field}"

    def test_exception(self) -> None:
        """Exceptions stay in one line."""
        try:
            raise ValueError("bad\nvalue")
        except ValueError:
            logger = logging.getLogger("x")
            record = logger.makeRecord(
                "x", logging.ERROR, "f", 1, "failed", (), _exc_info()
            )
        data = _format(record)
        assert "ValueError: bad\nvalue" in data["exc_info"]

    def test_stack(self) -> None:
        """Stack information is written."""
        assert _format(_record(stack_info="Stack"))["stack_info"] == "Stack"

    def test_extra(self) -> None:
        """Fields given via `extra` are written."""
        data = _format(_record(audit={"action": "x"}, number=1, _private=2))
        assert data["audit"] == {"action": "x"}
        assert data["number"] == 1
        assert "_private" not in data

    def test_extra_cannot_replace_fields(self) -> None:
        """Fields given via `extra` don't replace the others."""
        data = _format(_record(level="fake", timestamp="fake", logger="fake"))
        assert data["level"] != "fake"
        assert data["timestamp"] != "fake"
        assert data["logger"] == "my.logger"

    def test_not_serializable(self) -> None:
        """What JSON can't represent is written as text."""
        value = object()
        assert _format(_record(value=value))["value"] == str(value)

    def test_keys_not_serializable(self) -> None:
        """Values that can't be written at all are written as text."""
        circular: dict[str, Any] = {}
        circular["self"] = circular
        data = _format(_record(tuples={(1, 2): 3}, circular=circular))
        assert data["tuples"] == "{(1, 2): 3}"
        assert data["circular"] == str(circular)

    def test_values_json_lacks(self) -> None:
        """The output is valid JSON, for strict parsers."""
        line = JsonFormatter().format(_record(ratio=float("nan")))
        assert (
            json.loads(line, parse_constant=lambda _: pytest.fail("Not valid JSON"))[
                "ratio"
            ]
            == "nan"
        )

    def test_deep_nesting(self) -> None:
        """Deeply nested values don't get out of the formatter as errors."""
        deep: list[Any] = []
        for _ in range(100000):
            deep = [deep]
        # How deep Python gets depends on its version
        assert "deep" in _format(_record(deep=deep))

    def test_not_printable(self) -> None:
        """Values that can't be printed don't fail the record."""

        class _Bad:
            """Value that can't be printed."""

            def __str__(self) -> str:
                """Fail."""
                raise RuntimeError

        assert _format(_record(bad=_Bad()))["bad"] == "<not printable>"

    def test_only_failing_values_as_text(self) -> None:
        """Other fields keep their types if one can't be written."""
        data = _format(_record(ratio=float("nan"), count=3))
        assert data["ratio"] == "nan"
        assert data["count"] == 3

    def test_iteration_failing(self) -> None:
        """Values failing in other ways don't fail the record."""

        class _Bad(list[int]):
            """List that can't be iterated over."""

            def __iter__(self) -> Iterator[int]:
                """Fail."""
                raise RuntimeError

        assert _format(_record(bad=_Bad([1]), count=3))["count"] == 3

    def test_control_characters(self) -> None:
        """Line breaks of messages can't forge log lines."""
        message = "a\nb" + chr(0x2028) + "c"
        assert _format(_record(message))["message"] == message

    def test_no_double_escaping(self) -> None:
        """What was escaped for text logs reads the same."""
        message = escape_log_value("a\nb")
        assert _format(_record(message))["message"] == message


class TestLogConfig:
    """Tests of the configuration of logging."""

    def test_defaults(self) -> None:
        """By default, the format stays, and the additions are there."""
        conf = LogConfig()
        assert conf.handlers is not None
        assert conf.formatters is not None
        assert conf.handlers["console"].formatter == "standard"
        assert conf.handlers["console"].filters == ["context"]
        assert "{request_id}" not in conf.formatters["standard"].format
        assert (
            conf.formatters["json"].class_formatter
            == "foca.utils.logging.JsonFormatter"
        )
        assert conf.filters["context"].factory == "foca.utils.logging.ContextFilter"
        assert conf.loggers == {}

    def test_defaults_not_shared(self) -> None:
        """Instances don't share their defaults."""
        first = LogConfig()
        assert first.handlers is not None
        first.handlers["console"].filters = []
        first.filters["other"] = LogFilterConfig.model_validate({})
        second = LogConfig()
        assert second.handlers is not None
        assert second.handlers["console"].filters == ["context"]
        assert "other" not in second.filters

    def test_keeps_own_builtin_names(self) -> None:
        """Apps can replace the built-in formatter and filter."""
        conf = LogConfig.model_validate(
            {
                "formatters": {"json": {"format": "{message}"}},
                "filters": {"context": {"name": "foca"}},
                "handlers": {"console": {"filters": []}},
            }
        )
        assert conf.formatters is not None
        assert conf.formatters["json"].format == "{message}"
        assert conf.filters["context"].name == "foca"
        assert conf.handlers is not None
        assert conf.handlers["console"].filters == []

    def test_without_formatters_and_handlers(self) -> None:
        """Sections can be empty."""
        conf = LogConfig(formatters=None, handlers=None, root=None)
        dictConfig(conf.model_dump(by_alias=True, exclude_none=True))

    @pytest.mark.parametrize(
        "section",
        [
            {"handlers": {"console": {"filters": ["missing"]}}},
            {"loggers": {"a": {"filters": ["missing"]}}},
            {"loggers": {"a": {"handlers": ["missing"]}}},
        ],
    )
    def test_unknown_names(self, section: dict[str, Any]) -> None:
        """Filters and handlers must exist."""
        with pytest.raises(ValidationError, match="missing"):
            LogConfig.model_validate(section)

    @pytest.mark.parametrize("level", [11, -1])
    def test_invalid_level(self, level: int) -> None:
        """Levels of loggers are checked."""
        with pytest.raises(ValidationError):
            LogLoggerConfig(level=level)

    def test_filter_with_factory(self) -> None:
        """Filters can have factories with further arguments."""
        conf = LogFilterConfig.model_validate({"()": "a.B", "option": 1})
        assert conf.model_dump(by_alias=True, exclude_none=True) == {
            "()": "a.B",
            "option": 1,
        }

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="logging.config ignores the defaults of formatters before 3.12",
    )
    def test_formatter_defaults(self) -> None:
        """Defaults of formatters reach the formatter."""
        logger = logging.getLogger("foca_test_defaults")
        stream = io.StringIO()
        conf = LogConfig.model_validate(
            {
                "formatters": {
                    "text": {
                        "format": "{message}|{when}",
                        "defaults": {"when": "never"},
                    }
                },
                "handlers": {"console": {"formatter": "text"}},
                "loggers": {
                    "foca_test_defaults": {
                        "level": 30,
                        "handlers": ["console"],
                        "propagate": False,
                    }
                },
            }
        )
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            dictConfig(conf.model_dump(by_alias=True, exclude_none=True))
            logger.handlers[0].setStream(stream)  # type: ignore[attr-defined]
            logger.warning("shown")
            assert stream.getvalue() == "shown|never\n"
        finally:
            logger.handlers.clear()
            logger.propagate = True
            root.handlers[:] = handlers
            root.setLevel(level)

    def test_passed_to_dict_config(self, tmp_path: Path) -> None:
        """Loggers and filters take effect, with text and JSON formats."""
        stream = io.StringIO()
        logger = logging.getLogger("foca_test_logger")
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        conf = LogConfig.model_validate(
            {
                "formatters": {
                    "text": {
                        "format": "{request_id}|{levelname}|{message}",
                        "datefmt": "%Y",
                    }
                },
                "handlers": {"console": {"formatter": "text"}},
                "filters": {"only_a": {"name": "foca_test_logger"}},
                "loggers": {
                    "foca_test_logger": {
                        "level": 30,
                        "handlers": ["console"],
                        "propagate": False,
                        "filters": ["only_a"],
                    }
                },
            }
        )
        try:
            dictConfig(conf.model_dump(by_alias=True, exclude_none=True))
            logger.handlers[0].setStream(stream)  # type: ignore[attr-defined]
            logger.info("ignored")
            logger.warning("shown")
            assert stream.getvalue() == "|WARNING|shown\n"
            assert logger.propagate is False
        finally:
            logger.handlers.clear()
            logger.filters.clear()
            logger.propagate = True
            logger.setLevel(logging.NOTSET)
            root.handlers[:] = handlers
            root.setLevel(level)

    def test_json_formatter_from_config(self, tmp_path: Path) -> None:
        """The built-in formatter works when set in a configuration file."""
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"log": {"handlers": {"console": {"formatter": "json"}}}})
        )
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            ConfigParser(config_file=path)
            stream = io.StringIO()
            root.handlers[0].setStream(stream)  # type: ignore[attr-defined]
            logging.getLogger("foca_test_json").info("hello", extra={"a": 1})
            data = json.loads(stream.getvalue())
            assert data["message"] == "hello"
            assert data["a"] == 1
            assert data["request_id"] == ""
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)


def _exc_info() -> Any:
    """Get the current exception.

    Returns:
        Information on the exception.
    """
    import sys  # noqa: PLC0415

    return sys.exc_info()


def test_module_has_formatter() -> None:
    """The names used in the configuration exist."""
    assert module.JsonFormatter is JsonFormatter
