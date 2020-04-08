"""Unit test for security.cors.py"""

from foca.security.cors import enable_cors
from flask import Flask


from unittest.mock import patch


@patch('flask_cors.CORS')
def test_enable_cors(test_patch):
    """Test that CORS is called with app as a parameter
    """
    app = Flask(__name__)
    assert enable_cors(app) is None
    assert test_patch.called_with(app)
