"""Tests for the logging decorator"""

import pytest
import logging
from foca.utils.logging import log_traffic
from flask import Flask

app = Flask(__name__)
REQ = {'REQUEST_METHOD': 'GET',
       'PATH_INFO': '/',
       'SERVER_PROTOCOL': 'HTTP/1.1',
       'REMOTE_ADDR': '192.168.1.1',
       }

# Get logger instance
logger = logging.getLogger(__name__)


def test_log_level(caplog):
    """Verify that the 'log_level argument modifies the log level properly"""

    @log_traffic(log_level=30)
    def mock_func():
        return {'foo': 'bar'}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert 'WARNING' in caplog.text


def test_req_only(caplog):
    """Verify that only the request gets logged"""
    caplog.set_level(logging.INFO)

    @log_traffic(log_response=False)
    def mock_func():
        return {'foo': 'bar'}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert 'Incoming request' in caplog.text \
           and 'Response to request' not in caplog.text


def test_res_only(caplog):
    """Verify that only the response gets logged"""
    caplog.set_level(logging.INFO)

    @log_traffic(log_request=False)
    def mock_func():
        return {'foo': 'bar'}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert 'Incoming request' not in caplog.text \
           and 'Response to request' in caplog.text


def test_logging(caplog):
    """Verify the default settings work properly"""
    caplog.set_level(logging.INFO)

    @log_traffic
    def mock_func():
        return {'foo': 'bar'}

    with app.test_request_context(environ_base=REQ):
        mock_func()
    assert 'Incoming request' in caplog.text \
           and 'Response to request' in caplog.text
