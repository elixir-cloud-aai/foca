"""
Tests for exceptions.py
"""

import json
from copy import deepcopy

import pytest
from connexion import App
from connexion.exceptions import BadRequestProblem
from flask import Flask, Response
from werkzeug.exceptions import NotFound, ServiceUnavailable

from foca.errors.exceptions import (
    _exc_to_str,
    _exclude_key_nested_dict,
    _log_exception,
    _problem_handler_json,
    _subset_nested_dict,
    register_exception_handler,
)
from foca.models.config import Config

EXCEPTION_INSTANCE = Exception()
INVALID_LOG_FORMAT = "unknown_log_format"
TEST_DICT = {
    "title": "MyException",
    "details": {
        "code": 400,
        "description": "Some exception",
    },
    "status": 400,
}
TEST_KEYS = ["details", "code"]
EXPECTED_SUBSET_RESULT = {
    "details": {
        "code": 400,
    },
}
EXPECTED_EXCLUDE_RESULT = {
    "title": "MyException",
    "details": {
        "description": "Some exception",
    },
    "status": 400,
}
PUBLIC_MEMBERS = [["title"]]
PRIVATE_MEMBERS = [["status"]]


class UnknownException(Exception):
    pass


def test_register_exception_handler():
    """Test exception handler registration with Connexion app."""
    app = App(__name__)
    ret = register_exception_handler(app)
    assert isinstance(ret, App)


def test__exc_to_str():
    """Test exception reformatter function."""
    res = _exc_to_str(exc=EXCEPTION_INSTANCE)
    assert isinstance(res, str)


@pytest.mark.parametrize("format", ["oneline", "minimal", "regular"])
def test__log_exception(caplog, format):
    """Test exception reformatter function."""
    _log_exception(
        exc=EXCEPTION_INSTANCE,
        format=format,
    )
    assert "Exception" in caplog.text


def test__log_exception_invalid_format(caplog):
    """Test exception reformatter function with invalid format argument."""
    _log_exception(
        exc=EXCEPTION_INSTANCE,
        format=INVALID_LOG_FORMAT,
    )
    assert "logging is misconfigured" in caplog.text


def test__subset_nested_dict():
    """Test nested dictionary subsetting function."""
    res = _subset_nested_dict(obj=TEST_DICT, key_sequence=deepcopy(TEST_KEYS))
    assert res == EXPECTED_SUBSET_RESULT


def test__exclude_key_nested_dict():
    """Test function to exclude a key from a nested dictionary."""
    res = _exclude_key_nested_dict(
        obj=TEST_DICT, key_sequence=deepcopy(TEST_KEYS)
    )
    assert res == EXPECTED_EXCLUDE_RESULT


def test__problem_handler_json():
    """Test problem handler with instance of custom, unlisted error."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    EXPECTED_RESPONSE = app.config.foca.exceptions.mapping[Exception]
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == "500 INTERNAL SERVER ERROR"
        assert res.mimetype == "application/problem+json"
        response = json.loads(res.data.decode("utf-8"))
        assert response == EXPECTED_RESPONSE


class ItemNotFound(NotFound):
    pass


class ItemGone(ItemNotFound):
    pass


@pytest.mark.parametrize(
    "exception,status",
    [
        (ServiceUnavailable(), 503),
        (NotFound(), 404),
        # subclasses of mapped exceptions are handled like their closest
        # mapped base class
        (ItemNotFound(), 404),
        (ItemGone(), 404),
        (UnknownException(), 500),
    ],
)
def test__problem_handler_json_status(caplog, exception, status):
    """Test problem handler; status codes and logging."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    with app.app_context():
        res = _problem_handler_json(exception)
    assert res.status_code == status
    assert json.loads(res.data.decode("utf-8"))["status"] == status
    # one log record per handled exception, with problem details
    assert len(caplog.records) == 1
    assert f'"status": {status}' in caplog.records[0].getMessage()


def test__problem_handler_json_closest_base_class():
    """Test problem handler; closest mapped base class is used."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    app.config.foca.exceptions.mapping[ItemNotFound] = {
        "title": "Item not found",
        "status": 404,
    }
    with app.app_context():
        res = _problem_handler_json(ItemGone())
    assert json.loads(res.data.decode("utf-8"))["title"] == "Item not found"


def test__problem_handler_json_request_validation_error():
    """Test problem handler with request validation error."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    with app.app_context():
        res = _problem_handler_json(BadRequestProblem(detail="invalid"))
        assert res.status_code == 400
        response = json.loads(res.data.decode("utf-8"))
        assert response == {"title": "Bad Request", "status": 400}


def test__problem_handler_json_no_fallback_exception():
    """Test problem handler; unlisted error without fallback."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    del app.config.foca.exceptions.mapping[Exception]
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == "500 INTERNAL SERVER ERROR"
        assert res.mimetype == "application/problem+json"
        response = res.data.decode("utf-8")
        assert response == ""


def test__problem_handler_json_with_public_members():
    """Test problem handler with public members."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    app.config.foca.exceptions.public_members = PUBLIC_MEMBERS
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == "500 INTERNAL SERVER ERROR"
        assert res.mimetype == "application/problem+json"


def test__problem_handler_json_with_private_members():
    """Test problem handler with private members."""
    app = Flask(__name__)
    setattr(app.config, "foca", Config())
    app.config.foca.exceptions.private_members = PRIVATE_MEMBERS
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == "500 INTERNAL SERVER ERROR"
        assert res.mimetype == "application/problem+json"
