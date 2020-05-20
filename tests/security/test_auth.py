"""
Tests for auth.py
"""
import pytest
from unittest.mock import MagicMock

from connexion.exceptions import Unauthorized

from foca.security.auth import param_pass


def test_not_authorized():
    """Test for checking authorization requirement"""
    @param_pass(authorization_required=False)
    def mock_func():
        p = locals()
        return len(p)
    assert mock_func() == 0


def test_no_validation_methods_present():
    """Test for the presence of validation methods"""
    with pytest.raises(Unauthorized):
        @param_pass(validation_methods=[])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_valid_validation_methods_args():
    """Test for ensuring the validity of validation checks argument"""
    with pytest.raises(Unauthorized):
        @param_pass(validation_checks="")
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_parse_jwt_from_header_without_auth_header(monkeypatch):
    """Test for the presence of authorization header"""
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


def test_parse_jwt_from_header_with_incorrect_auth_header(monkeypatch):
    """Test for the incorrect format of authorization header"""
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


def test_parse_jwt_from_header_with_invalid_prefix(monkeypatch):
    """Test for the valid authorization header prefix"""
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


def test_validate_jwt_via_userinfo_endpoint_invalid(monkeypatch):
    """Test for invalid jwt decode when validation_methods = userinfo"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)
    print(request)
    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["userinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_validate_jwt_via_public_key_invalid(monkeypatch):
    """Test for invalid jwt decode when validation_methods = public_key"""
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


def test_no_validation_methods(monkeypatch):
    """Test for invalid validation_methods"""
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
