import sys
print(sys.path)
import pytest
from foca.config.config_parser import YAMLConfigParser
from foca.security.auth import parse_jwt_from_header
import unittest
from unittest.mock import patch

@patch('foca.security.auth.request.headers.get')
def test_parse_jwt_from_header(mock_get):
    mock_get.return_value = 'Auth Bearer'
    ret = parse_jwt_from_header()
    assert ret=='Auth'
