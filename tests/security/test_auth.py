"""
Tests for auth.py
"""

import pytest
from connexion.exceptions import Unauthorized
from foca.security.auth import param_pass
from unittest.mock import MagicMock


"""
Test for checking authorization requirement
"""


def test_not_authorized():
    @param_pass(authorization_required=False)
    def mock_func():
        p = locals()
        return len(p)
    assert mock_func() == 0


"""
Test for the presence of validation methods
"""


def test_no_validation_methods_present():
    with pytest.raises(Unauthorized):
        @param_pass(validation_methods=[])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for ensuring the validity of validation checks argument
"""


def test_valid_validation_methods_args():
    with pytest.raises(Unauthorized):
        @param_pass(validation_checks="")
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for the presence of authorization header
"""


def test_parse_jwt_from_header_without_auth_header(monkeypatch):
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass()
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for the incorrect format of authorization header
"""


def test_parse_jwt_from_header_with_incorrect_auth_header(monkeypatch):
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'incorrect header format'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass()
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for the valid authorization header prefix
"""


def test_parse_jwt_from_header_with_invalid_prefix(monkeypatch):
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="new_prefix")
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for invalid jwt decode when validation_methods = userinfo'
"""


def test_validate_jwt_via_userinfo_endpoint_invalid(monkeypatch):
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["useinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for invalid jwt decode when validation_methods = public_key'
"""


def test_validate_jwt_via_public_key_invalid(monkeypatch):
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["public_key"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


"""
Test for invalid validation_methods'
"""


def test_no_validation_methods(monkeypatch):
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=[])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0
