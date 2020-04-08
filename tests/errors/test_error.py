"""
Test for error.py
"""
import json
from foca.errors.error import (
    handle_bad_request,
    __handle_forbidden,
    __handle_unauthorized,
    __handle_task_not_found,
    __handle_internal_server_error
)


def test_handle_bad_request():
    """Test for BadRequest Error Handler
    """
    get400 = handle_bad_request(Exception)
    assert get400.status == "400 BAD REQUEST"
    assert get400.mimetype == 'application/problem+json'
    error400 = json.loads(get400.data.decode('utf-8'))
    assert error400['msg'] == 'The request is malformed.'
    assert error400['status_code'] == '400'
    assert 'instance' not in error400


def test___handle_forbidden():
    """Test for Forbidden Error Handler
    """
    get403 = __handle_forbidden(Exception)
    assert get403.status == "403 FORBIDDEN"
    assert get403.mimetype == 'application/problem+json'
    error403 = json.loads(get403.data.decode('utf-8'))
    message = 'The requester is not authorized to perform this action.'
    assert error403['msg'] == message
    assert error403['status_code'] == '403'
    assert 'instance' not in error403


def test___handle_unauthorized():
    """Test for Unauthorized Error Handler
    """
    get401 = __handle_unauthorized(Exception)
    assert get401.status == "401 UNAUTHORIZED"
    assert get401.mimetype == 'application/problem+json'
    error401 = json.loads(get401.data.decode('utf-8'))
    assert error401['msg'] == 'The request is unauthorized.'
    assert error401['status_code'] == '401'
    assert 'instance' not in error401


def test___handle_task_not_found():
    """Test for Task Not found Error Handler
    """
    get404 = __handle_task_not_found(Exception)
    assert get404.status == "404 NOT FOUND"
    assert get404.mimetype == 'application/problem+json'
    error404 = json.loads(get404.data.decode('utf-8'))
    assert error404['msg'] == 'The requested task was not found.'
    assert error404['status_code'] == '404'
    assert 'instance' not in error404


def test___handle_internal_server_error():
    """Test for Internal Server Error Handler
    """
    get500 = __handle_internal_server_error(Exception)
    assert get500.status == "500 INTERNAL SERVER ERROR"
    assert get500.mimetype == 'application/problem+json'
    error500 = json.loads(get500.data.decode('utf-8'))
    assert error500['msg'] == 'An unexpected error occurred.'
    assert error500['status_code'] == '500'
    assert 'instance' not in error500
