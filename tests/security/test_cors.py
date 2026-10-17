"""Unit test for security.cors.py"""

from unittest.mock import patch

from flask import Flask

from foca.security.cors import enable_cors


def test_enable_cors():
    """Test that CORS is called with app as a parameter."""
    app = Flask(__name__)
    with patch("foca.security.cors.CORS") as mock_cors:
        enable_cors(app)
        mock_cors.assert_called_once_with(app)
