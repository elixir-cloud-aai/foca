"""
Tests for auth.py
"""

import pytest
from connexion import request
from connexion.exceptions import Unauthorized
from unittest.mock import Mock
from foca.security.auth import param_pass

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
        @param_pass(validation_methods = [])
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0

"""
Test for ensuring the validity of validation checks argument
"""

def test_valid_validation_methods_args():
    with pytest.raises(Unauthorized):
        @param_pass(validation_checks = "")
        def mock_func():
            p = locals()
            return len(p)
        assert mock_func() == 0