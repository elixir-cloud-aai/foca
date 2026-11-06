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
from connexion.resolver import Resolver
from flask import Flask
from pydantic import ValidationError

from foca import context
from foca.config.config_parser import ConfigParser
from foca.context import RequestIdMiddleware
from foca.factories.connexion_app import create_connexion_app
from foca.models.config import (
    LogConfig,
    LogFilterConfig,
    LogLoggerConfig,
    RequestIdConfig,
)
from foca.security.access_control import enforcement
from foca.utils import logging as module
from foca.utils.logging import ContextFilter, JsonFormatter, escape_log_value
from foca.utils.urls import get_operation_id

CONTEXT_FIELDS = ("request_id", "operation_id", "subject", "client")

# Records kept by `_keep_record()`
_RECORDS: list[logging.LogRecord] = []


def _keep_record() -> str:
    """Serve an operation, keeping a record with the context.

    Returns:
        Empty body.
    """
    record = _record()
    ContextFilter().filter(record)
    _RECORDS.append(record)
    return ""


class _KeepRecordResolver(Resolver):
    """Resolver of all operations to `_keep_record()`."""

    def __init__(self) -> None:
        """Create the resolver."""
        super().__init__(function_resolver=lambda _: _keep_record)


def _spec() -> dict[str, Any]:
    """Build a specification with the operation `my_app.getThings`.

    Returns:
        OpenAPI 3 specification, with base path `/api`.
    """
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1"},
        "servers": [{"url": "/api"}],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "my_app.getThings",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


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
        records: list[logging.LogRecord] = []
        monkeypatch.setattr(sys.modules[__name__], "_RECORDS", records)
        cnx_app = create_connexion_app()
        cnx_app.add_api(_spec(), resolver=_KeepRecordResolver())
        app = cnx_app.app
        app.wsgi_app = RequestIdMiddleware(  # type: ignore[method-assign]
            app.wsgi_app, trust_incoming=True
        )
        app.test_client().get("/api/things", headers={"X-Request-ID": "abc-1"})
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

    def test_not_an_operation(self) -> None:
        """Routes that aren't operations, e.g., added via Flask, have none."""
        app = Flask(__name__)
        app.add_url_rule("/", "/api.other", lambda: "")
        with app.test_request_context("/"):
            assert get_operation_id() == ""

    def test_operation(self) -> None:
        """Operations go by their identifiers in the specification."""
        cnx_app = create_connexion_app()
        cnx_app.add_api(_spec(), resolver=_KeepRecordResolver())
        with cnx_app.app.test_request_context("/api/things"):
            assert get_operation_id() == "my_app.getThings"


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
                "filters": {"only_a": {"name": "foca_test_logger.a"}},
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
            child = logger.getChild("a")
            # Below the level of the logger
            child.info("ignored")
            # Refused by the filter of the logger, which only applies to itself
            logger.warning("filtered")
            child.warning("shown")
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


class TestSourceryFindings:
    """Tests of edge cases of the configuration and of the JSON format."""

    @pytest.mark.parametrize("header", ["Content-Length", "set-cookie", "Host"])
    def test_reserved_header(self, header: str) -> None:
        """Headers with another purpose can't be used."""
        with pytest.raises(ValidationError, match="must not be a standard header"):
            RequestIdConfig(header=header)

    def test_root_handler_unknown(self) -> None:
        """Handlers of the root logger must exist."""
        with pytest.raises(ValidationError, match="root: missing"):
            LogConfig.model_validate({"root": {"handlers": ["missing"]}})

    def test_no_root(self) -> None:
        """The root logger may have no configuration."""
        assert LogConfig.model_validate({"root": None}).root is None

    def test_task_id_escaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Task identifiers can't add log lines."""
        monkeypatch.setattr(module, "request_id", lambda: "a\nb")
        record = _record()
        ContextFilter().filter(record)
        assert record.request_id == "a\\nb"  # type: ignore[attr-defined]

    def test_operation_id_escaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Operation identifiers can't add log lines."""
        monkeypatch.setattr(
            "foca.utils.urls.get_operation_id", lambda: "a\nb", raising=True
        )
        with Flask(__name__).test_request_context():
            assert module._get_context()["operation_id"] == "a\\nb"

    def test_key_not_a_string(self) -> None:
        """Keys of `extra` that aren't strings don't lose the record."""
        record = _record()
        vars(record)[1] = "value"  # type: ignore[index]
        assert _format(record)["1"] == "value"

    def test_mixed_values(self) -> None:
        """Records with values of several kinds aren't lost."""
        data = _format(_record(other=object(), ratio=float("nan")))
        assert data["ratio"] == "nan"
        assert data["other"].startswith("<object")


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
