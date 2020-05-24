"""
Tests for errors.py
"""

from foca.errors.errors import (
    __handle_internal_server_error,
    __handle_not_found,
    __handle_forbidden,
    __handle_unauthorized,
    handle_bad_request,
    register_error_handlers,
)
import json
from unittest.mock import MagicMock


def test_bad_request():
    """Test for functioning of custom handler for BadRequest error"""
    error = handle_bad_request(Exception)
    assert error.status == '400 BAD REQUEST'
    assert error.mimetype == "application/problem+json"
    response = json.loads(error.data.decode('utf-8'))
    assert response == {
        "msg": "The request is malformed.",
        "status_code": "400"
        }


def test_forbidden():
    """Test for functioning of custom handler for Forbidden error"""
    error = __handle_forbidden(Exception)
    assert error.status == '403 FORBIDDEN'
    assert error.mimetype == "application/problem+json"
    response = json.loads(error.data.decode('utf-8'))
    assert response == {
        "msg": "The requester is not authorized to perform this action.",
        "status_code": "403"
        }


def test_internal_server_error():
    """Test for functioning of custom handler for InternalServerError"""
    error = __handle_internal_server_error(Exception)
    assert error.status == '500 INTERNAL SERVER ERROR'
    assert error.mimetype == "application/problem+json"
    response = json.loads(error.data.decode('utf-8'))
    assert response == {
        "msg": "An unexpected error occurred.",
        "status_code": "500"
        }


def test_not_found():
    """Test for functioning of custom handler for NotFound error"""
    error = __handle_not_found(Exception)
    assert error.status == '404 NOT FOUND'
    assert error.mimetype == "application/problem+json"
    response = json.loads(error.data.decode('utf-8'))
    assert response == {
        "msg": "The requested resource was not found.",
        "status_code": "404"
        }


def test_unauthorized():
    """Test for functioning of custom handler for Unauthorized error"""
    error = __handle_unauthorized(Exception)
    assert error.status == '401 UNAUTHORIZED'
    assert error.mimetype == "application/problem+json"
    response = json.loads(error.data.decode('utf-8'))
    assert response == {
        "msg": "The request is unauthorized.",
        "status_code": "401"
        }


def test_register_error_handlers(monkeypatch):
    test_app = MagicMock(name='App')
    monkeypatch.setattr('foca.errors.errors.App', test_app)
    test_app = register_error_handlers(test_app)
    assert type(test_app) == type(test_app)
