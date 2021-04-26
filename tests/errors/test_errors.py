"""
Tests for exceptions.py
"""

from copy import deepcopy
import json

from flask import (Flask, Response)
from connexion import App
import pytest

from foca.errors.exceptions import (
    _exc_to_str,
    _exclude_key_nested_dict,
    _problem_handler_json,
    _log_exception,
    register_exception_handler,
    _subset_nested_dict,
)
from foca.models.config import Config

EXCEPTION_INSTANCE = Exception()
INVALID_LOG_FORMAT = 'unknown_log_format'
TEST_DICT = {
    "title": "MyException",
    "details": {
        "code": 400,
        "description": "Some exception",
    },
    "status": 400,
}
TEST_KEYS = ['details', 'code']
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
PUBLIC_MEMBERS = [['title']]
PRIVATE_MEMBERS = [['status']]


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


@pytest.mark.parametrize("format", ['oneline', 'minimal', 'regular'])
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
    res = _subset_nested_dict(
        obj=TEST_DICT,
        key_sequence=deepcopy(TEST_KEYS)
    )
    assert res == EXPECTED_SUBSET_RESULT


def test__exclude_key_nested_dict():
    """Test function to exclude a key from a nested dictionary."""
    res = _exclude_key_nested_dict(
        obj=TEST_DICT,
        key_sequence=deepcopy(TEST_KEYS)
    )
    assert res == EXPECTED_EXCLUDE_RESULT


def test__problem_handler_json():
    """Test problem handler with instance of custom, unlisted error."""
    app = Flask(__name__)
    app.config['FOCA'] = Config()
    EXPECTED_RESPONSE = app.config['FOCA'].exceptions.mapping[Exception]
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == '500 INTERNAL SERVER ERROR'
        assert res.mimetype == "application/problem+json"
        response = json.loads(res.data.decode('utf-8'))
        assert response == EXPECTED_RESPONSE


def test__problem_handler_json_no_fallback_exception():
    """Test problem handler; unlisted error without fallback."""
    app = Flask(__name__)
    app.config['FOCA'] = Config()
    del app.config['FOCA'].exceptions.mapping[Exception]
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == '500 INTERNAL SERVER ERROR'
        assert res.mimetype == "application/problem+json"
        response = res.data.decode("utf-8")
        assert response == ""


def test__problem_handler_json_with_public_members():
    """Test problem handler with public members."""
    app = Flask(__name__)
    app.config['FOCA'] = Config()
    app.config['FOCA'].exceptions.public_members = PUBLIC_MEMBERS
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == '500 INTERNAL SERVER ERROR'
        assert res.mimetype == "application/problem+json"


def test__problem_handler_json_with_private_members():
    """Test problem handler with private members."""
    app = Flask(__name__)
    app.config['FOCA'] = Config()
    app.config['FOCA'].exceptions.private_members = PRIVATE_MEMBERS
    with app.app_context():
        res = _problem_handler_json(UnknownException())
        assert isinstance(res, Response)
        assert res.status == '500 INTERNAL SERVER ERROR'
        assert res.mimetype == "application/problem+json"
