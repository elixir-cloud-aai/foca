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

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=[])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_no_validation_checks_are_any(monkeypatch):
    """Test for validation_checks = 'any'"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}

    monkeypatch.setattr('foca.security.auth.request', request)

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_checks='any')
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_verify_issuer_claim(monkeypatch):
    """Test to verify existence of issuer claim"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}
    mock_jwt = MagicMock(name='jwt')
    mock_jwt.return_value = {'some': 'payload'}
    monkeypatch.setattr('foca.security.auth.request', request)
    monkeypatch.setattr('foca.security.auth.jwt.decode', mock_jwt)

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["userinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_invalid_url_request(monkeypatch):
    """Test if entry point url missing"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}
    mock_jwt = MagicMock(name='jwt')
    mock_jwt.return_value = {'some': 'payload', 'iss': 'payload1'}
    monkeypatch.setattr('foca.security.auth.request', request)
    monkeypatch.setattr('foca.security.auth.jwt.decode', mock_jwt)
    monkeypatch.setattr('foca.security.auth.requests.get', None)

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["userinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_check_get_request_entry_for_userinfo(monkeypatch):
    """Test if entry missing in the url requested"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}
    request_url = MagicMock(name='requests')
    request_url.status_code = 200
    request_url.return_value.json.return_value = {'random': 'random_url'}
    mock_jwt = MagicMock(name='jwt')
    mock_jwt.return_value = {'some': 'payload', 'iss': 'payload1'}
    monkeypatch.setattr('foca.security.auth.request', request)
    monkeypatch.setattr('foca.security.auth.jwt.decode', mock_jwt)
    monkeypatch.setattr('foca.security.auth.requests.get', request_url)

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["userinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_check_get_request_entry(monkeypatch):
    """Test if entry present in the url requested"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}
    request_url = MagicMock(name='requests')
    request_url.status_code = 200
    request_url.return_value.json.return_value = {
        'random': 'random_url',
        'userinfo_endpoint': 'check1'
        }
    mock_jwt = MagicMock(name='jwt')
    mock_jwt.return_value = {'some': 'payload', 'iss': 'payload1'}
    monkeypatch.setattr('foca.security.auth.request', request)
    monkeypatch.setattr('foca.security.auth.jwt.decode', mock_jwt)
    monkeypatch.setattr('foca.security.auth.requests.get', request_url)

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["userinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_validate_jwt_via_endpoint(monkeypatch):
    """Test for correct flow of validate_jwt_via_endpoint"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}
    request_url = MagicMock(name='requests')
    request_url.status_code = 200
    request_url.return_value.json.return_value = {
        'random': 'random_url',
        'userinfo_endpoint': 'check1'
        }
    request_url.return_value.headers.return_value = {
        'Authorization': 'prefix suffix'
        }
    mock_jwt = MagicMock(name='jwt')
    mock_jwt.return_value = {'iss': 'payload1'}
    monkeypatch.setattr('foca.security.auth.request', request)
    monkeypatch.setattr('foca.security.auth.jwt.decode', mock_jwt)
    monkeypatch.setattr('foca.security.auth.requests.get', request_url)

    with pytest.raises(Unauthorized):
        @param_pass(token_prefix="prefix", validation_methods=["userinfo"])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0


def test_param_pass_through_userinfo(monkeypatch):
    """Test for correct flow through validation_methods=userinfo"""
    request = MagicMock(name='request')
    request.args = {}
    request.headers = {'Authorization': 'prefix suffix'}
    request.cookies = {}
    request.params = {}
    request_url = MagicMock(name='requests')
    request_url.status_code = 200
    request_url.return_value.json.return_value = {
        'random': 'random_url',
        'userinfo_endpoint': 'check1'
        }
    request_url.return_value.headers.return_value = {
        'Authorization': 'prefix suffix'
        }
    mock_jwt = MagicMock(name='jwt')
    mock_jwt.return_value = {'iss': 'payload1'}
    monkeypatch.setattr('foca.security.auth.request', request)
    monkeypatch.setattr('foca.security.auth.jwt.decode', mock_jwt)
    monkeypatch.setattr('foca.security.auth.requests.get', request_url)

    @param_pass(token_prefix="prefix",
                validation_methods=["userinfo"], claim_identity="iss")
    def mock_func(*args, **kwargs):
        p = locals()
        return len(p)
    assert mock_func() == 2
