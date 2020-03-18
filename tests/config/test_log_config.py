"""
Tests for log_config.py
"""


from foca.config.config_parser import YAMLConfigParser
from foca.config.log_config import configure_logging
from unittest.mock import MagicMock, patch

import os


CONFIG_VAR_OK = os.path.abspath(
        os.path.join(
            os.path.dirname(
                os.path.realpath(__file__)
            ),
            'log_config.yaml'
        )
    )

CONFIG_PATH_INVALID = os.path.abspath(
        os.path.join(
            os.path.dirname(
                os.path.realpath(__file__)
            ),
            'log_config1.yaml'
        )
    )

"""
dictConfig is called once if config_var is undefined
"""


@patch('foca.config.log_config.dictConfig')
def test_configure_logging_config_var_undefined(test_patch):
    configure_logging("")
    assert test_patch.called is True


"""
dictConfig is not called if config_path does not have any file
"""


@patch('foca.config.log_config.dictConfig')
def test_configure_logging_config_var_not_defined__file_not_found(test_patch):
    # Assuming it will raise FileNotFoundError
    YAMLConfigParser.update_from_yaml = MagicMock(
                                                side_effect=FileNotFoundError
                                                )
    configure_logging(default_path=str(CONFIG_PATH_INVALID))
    assert test_patch.called is False


"""
dictConfig is called once if config_var is defined
"""


@patch('foca.config.log_config.dictConfig')
def test_configure_logging_config_var_defined(test_patch):
    os.environ['TEST_LOG_CONFIG'] = CONFIG_VAR_OK
    YAMLConfigParser.update_from_yaml = MagicMock()
    configure_logging(config_var="TEST_LOG_CONFIG")
    assert test_patch.called is True
