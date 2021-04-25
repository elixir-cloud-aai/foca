"""Tests for authentication module."""

from copy import deepcopy
from unittest.mock import MagicMock

from connexion.exceptions import Unauthorized
from flask import Flask
from jwt.exceptions import (InvalidKeyError, InvalidTokenError)
import pytest
from requests.exceptions import ConnectionError

from foca.models.config import (Config, ValidationChecksEnum)
from foca.security.auth import (
    _get_public_keys,
    _validate_jwt_userinfo,
    _validate_jwt_public_key,
    validate_token,
)

DICT_EMPTY = {}
MOCK_BYTES = b'my-mock-bytes'
MOCK_CLAIMS_ISSUER = {"iss": "some-mock-issuer"}
MOCK_USER_ID = '1234567890'
MOCK_CLAIMS_NO_SUB = {
    'azp': 'my-azp',
    'scope': 'email openid profile',
    'iss': 'https://my.issuer.org/oidc/',
    'exp': 1000010000,
    'iat': 1000000000,
    'jti': 'my-jti',
}
MOCK_CLAIMS = deepcopy(MOCK_CLAIMS_NO_SUB)
MOCK_CLAIMS['sub'] = 'user@issuer.org'
MOCK_KEYS = {
    "abc": (
        "uVHPfUHVEzpgOnDNi3e2pVsbK1hsINsTy_1mMT7sxDyP-1eQSjzYsGSUJ3GH"
        "q9LhiVndpwV8y7Enjdj0purywtwk_D8z9IIN36RJAh1yhFfbyhLPEZlCDdzx"
        "as5Dku9k0GrxQuV6i30Mid8OgRQ2q3pmsks414Afy6xugC6u3inyjLzLPrhR"
        "0oRPTGdNMXJbGw4sVTjnh5AzTgX-GrQWBHSjI7rMTcvqbbl7M8OOhE3MQ_gf"
        "VLXwmwSIoKHODC0RO-XnVhqd7Qf0teS1JiILKYLl5FS_7Uy2ClVrAYd2T6X9"
        "DIr_JlpRkwSD899pq6PR9nhKguipJE0qUXxamdY9nw"
    ),
}
MOCK_JWK = {
    "kty": "RSA",
    "e": "AQAB",
    "kid": "rsa1",
    "alg": "RS256",
    "n": (
        "uVHPfUHVEzpgOnDNi3e2pVsbK1hsINsTy_1mMT7sxDyP-1eQSjzYsGSUJ3GH"
        "q9LhiVndpwV8y7Enjdj0purywtwk_D8z9IIN36RJAh1yhFfbyhLPEZlCDdzx"
        "as5Dku9k0GrxQuV6i30Mid8OgRQ2q3pmsks414Afy6xugC6u3inyjLzLPrhR"
        "0oRPTGdNMXJbGw4sVTjnh5AzTgX-GrQWBHSjI7rMTcvqbbl7M8OOhE3MQ_gf"
        "VLXwmwSIoKHODC0RO-XnVhqd7Qf0teS1JiILKYLl5FS_7Uy2ClVrAYd2T6X9"
        "DIr_JlpRkwSD899pq6PR9nhKguipJE0qUXxamdY9nw"
    ),
}
MOCK_JWK_PRIVATE = {
    "kty": "RSA",
    "kid": "juliet@capulet.lit",
    "use": "enc",
    "n": (
        "t6Q8PWSi1dkJj9hTP8hNYFlvadM7DflW9mWepOJhJ66w7nyoK1gPNqFMSQRy"
        "O125Gp-TEkodhWr0iujjHVx7BcV0llS4w5ACGgPrcAd6ZcSR0-Iqom-QFcNP"
        "8Sjg086MwoqQU_LYywlAGZ21WSdS_PERyGFiNnj3QQlO8Yns5jCtLCRwLHL0"
        "Pb1fEv45AuRIuUfVcPySBWYnDyGxvjYGDSM-AqWS9zIQ2ZilgT-GqUmipg0X"
        "OC0Cc20rgLe2ymLHjpHciCKVAbY5-L32-lSeZO-Os6U15_aXrk9Gw8cPUaX1"
        "_I8sLGuSiVdt3C_Fn2PZ3Z8i744FPFGGcG1qs2Wz-Q"
    ),
    "e": "AQAB",
    "d": (
        "GRtbIQmhOZtyszfgKdg4u_N-R_mZGU_9k7JQ_jn1DnfTuMdSNprTeaSTyWfS"
        "NkuaAwnOEbIQVy1IQbWVV25NY3ybc_IhUJtfri7bAXYEReWaCl3hdlPKXy9U"
        "vqPYGR0kIXTQRqns-dVJ7jahlI7LyckrpTmrM8dWBo4_PMaenNnPiQgO0xnu"
        "ToxutRZJfJvG4Ox4ka3GORQd9CsCZ2vsUDmsXOfUENOyMqADC6p1M3h33tsu"
        "rY15k9qMSpG9OX_IJAXmxzAh_tWiZOwk2K4yxH9tS3Lq1yX8C1EWmeRDkK2a"
        "hecG85-oLKQt5VEpWHKmjOi_gJSdSgqcN96X52esAQ"
    ),
    "p": (
        "2rnSOV4hKSN8sS4CgcQHFbs08XboFDqKum3sc4h3GRxrTmQdl1ZK9uw-PIHf"
        "QP0FkxXVrx-WE-ZEbrqivH_2iCLUS7wAl6XvARt1KkIaUxPPSYB9yk31s0Q8"
        "UK96E3_OrADAYtAJs-M3JxCLfNgqh56HDnETTQhH3rCT5T3yJws"
    ),
    "q": (
        "1u_RiFDP7LBYh3N4GXLT9OpSKYP0uQZyiaZwBtOCBNJgQxaj10RWjsZu0c6I"
        "edis4S7B_coSKB0Kj9PaPaBzg-IySRvvcQuPamQu66riMhjVtG6TlV8CLCYK"
        "rYl52ziqK0E_ym2QnkwsUX7eYTB7LbAHRK9GqocDE5B0f808I4s"
    ),
    "dp": (
        "KkMTWqBUefVwZ2_Dbj1pPQqyHSHjj90L5x_MOzqYAJMcLMZtbUtwKqvVDq3t"
        "bEo3ZIcohbDtt6SbfmWzggabpQxNxuBpoOOf_a_HgMXK_lhqigI4y_kqS1wY"
        "52IwjUn5rgRrJ-yYo1h41KR-vz2pYhEAeYrhttWtxVqLCRViD6c"
    ),
    "dq": (
        "AvfS0-gRxvn0bwJoMSnFxYcK1WnuEjQFluMGfwGitQBWtfZ1Er7t1xDkbN9G"
        "QTB9yqpDoYaN06H7CFtrkxhJIBQaj6nkF5KKS3TQtQ5qCzkOkmxIe3KRbBym"
        "Xxkb5qwUpX5ELD5xFc6FeiafWYY63TmmEAu_lRFCOJ3xDea-ots"
    ),
    "qi": (
        "lSQi-w9CpyUReMErP1RsBLk7wNtOvs5EQpPqmuMvqW57NBUczScEoPwmUqqa"
        "bu9V0-Py4dQ57_bapoKRu1R90bvuFnU63SHWEFglZQvJDMeAvmj4sm-Fp0oY"
        "u_neotgQ0hzbI5gry7ajdYy9-2lNx_76aBZoOUu9HCJ-UsfSOI8"
    ),
}
MOCK_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODk"
    "wIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyLCJpc3MiOiJ"
    "pc3N1ZXIifQ.4i3V6q3dT30uK0ESa7ia5oM4Y06IZM8Yc6Z3l0-IiyA"
)
MOCK_TOKEN_HEADER_KID = (
    "eyJhbGciOiJIUzI1NiIsImtpZCI6InJzYTEifQ.eyJzdWIiOiIxMjM0NTY3O"
    "DkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyLCJpc3MiO"
    "iJpc3N1ZXIifQ.nTQVs9sJVSiwUtbvMMuBrPvIzmxhDETVTRInuAFU9rY"
)
MOCK_TOKEN_INVALID = "my-invalid-token"
MOCK_URL = "https://some-url-that-does-not.exist"


def _raise(exception) -> None:
    """General purpose exception raiser."""
    raise exception


class TestValidateToken:
    """Tests for `validate_token()`."""

    def test_success_all_validation_checks(self, monkeypatch):
        """Test for validating token successfully via all methods."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = {
            'userinfo_endpoint': MOCK_URL,
            'jwks_uri': MOCK_URL,
        }
        monkeypatch.setattr('requests.get', request)
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_userinfo',
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_public_key',
            lambda **kwargs: None,
        )
        with app.app_context():
            res = validate_token(token=MOCK_TOKEN_HEADER_KID)
            assert res['user_id'] == MOCK_USER_ID

    def test_success_any_validation_check(self, monkeypatch):
        """Test for validating token successfully via any method."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        app.config['FOCA'].security.auth.\
            validation_checks = ValidationChecksEnum.any
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = {
            'userinfo_endpoint': MOCK_URL,
            'jwks_uri': MOCK_URL,
        }
        monkeypatch.setattr('requests.get', request)
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_userinfo',
            lambda **kwargs: None,
        )
        with app.app_context():
            res = validate_token(token=MOCK_TOKEN_HEADER_KID)
            assert res['user_id'] == MOCK_USER_ID

    def test_no_validation_methods(self):
        """Test for failed validation due to missing validation methods."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        app.config['FOCA'].security.auth.validation_methods = []
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN)

    def test_invalid_token(self):
        """Test for failed validation due to invalid token."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN_INVALID)

    def test_no_claims(self, monkeypatch):
        """Test for token with no issuer claim."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        monkeypatch.setattr(
            'jwt.decode',
            lambda *args, **kwargs: {},
        )
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN_INVALID)

    def test_oidc_config_unavailable(self, monkeypatch):
        """Test for mocking an unavailable OIDC configuration server."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        monkeypatch.setattr(
            'requests.get',
            lambda **kwargs: _raise(ConnectionError)
        )
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN_HEADER_KID)

    def test_success_no_subject_claim(self, monkeypatch):
        """Test for validating token without subject claim."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        monkeypatch.setattr(
            'jwt.decode',
            lambda *args, **kwargs: MOCK_CLAIMS_NO_SUB,
        )
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = {
            'userinfo_endpoint': MOCK_URL,
            'jwks_uri': MOCK_URL,
        }
        monkeypatch.setattr('requests.get', request)
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_userinfo',
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_public_key',
            lambda **kwargs: None,
        )
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN_HEADER_KID)

    def test_fail_all_validation_checks_all_required(self, monkeypatch):
        """Test for all token validation methods failing when all methods
        are required to pass."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = {
            'userinfo_endpoint': MOCK_URL,
            'jwks_uri': MOCK_URL,
        }
        monkeypatch.setattr('requests.get', request)
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_userinfo',
            lambda **kwargs: _raise(ConnectionError),
        )
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_public_key',
            lambda **kwargs: _raise(Unauthorized),
        )
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN_HEADER_KID)

    def test_fail_all_validation_checks_any_required(self, monkeypatch):
        """Test for all token validation methods failing when any method
        is required to pass."""
        app = Flask(__name__)
        app.config['FOCA'] = Config()
        app.config['FOCA'].security.auth.\
            validation_checks = ValidationChecksEnum.any
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = {
            'userinfo_endpoint': MOCK_URL,
            'jwks_uri': MOCK_URL,
        }
        monkeypatch.setattr('requests.get', request)
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_userinfo',
            lambda **kwargs: _raise(ConnectionError),
        )
        monkeypatch.setattr(
            'foca.security.auth._validate_jwt_public_key',
            lambda **kwargs: _raise(Unauthorized),
        )
        with app.app_context():
            with pytest.raises(Unauthorized):
                validate_token(token=MOCK_TOKEN_HEADER_KID)


class TestValidateJwtUserinfo:
    """Tests for `_validate_jwt_userinfo()`."""

    def test_success(self, monkeypatch):
        """Test for validating a token successfully."""
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = {}
        monkeypatch.setattr('requests.get', request)
        res = _validate_jwt_userinfo(
            token=MOCK_TOKEN,
            url=MOCK_URL,
        )
        assert res is None

    def test_ConnectionError(self, monkeypatch):
        """Test for being unable to connect to user info endpoint."""
        monkeypatch.setattr(
            'requests.get',
            lambda **kwargs: _raise(ConnectionError)
        )
        with pytest.raises(ConnectionError):
            _validate_jwt_userinfo(
                token=MOCK_TOKEN,
                url=MOCK_URL,
            )


class TestValidateJwtPublicKey:
    """Tests for `_validate_jwt_public_key()`."""

    def test_success(self, monkeypatch):
        """Test for validating a token successfully."""
        monkeypatch.setattr(
            'foca.security.auth._get_public_keys',
            lambda **kwargs: MOCK_KEYS,
        )
        monkeypatch.setattr(
            'jwt.decode',
            lambda *args, **kwargs: MOCK_CLAIMS,
        )
        res = _validate_jwt_public_key(
            token=MOCK_TOKEN,
            url=MOCK_URL,
            allow_expired=True,
        )
        assert res is None

    def test_InvalidKeyError(self, monkeypatch):
        """Test for invalid key."""
        monkeypatch.setattr(
            'foca.security.auth._get_public_keys',
            lambda **kwargs: MOCK_KEYS,
        )
        monkeypatch.setattr(
            'jwt.decode',
            lambda **kwargs: _raise(InvalidKeyError),
        )
        with pytest.raises(Unauthorized):
            _validate_jwt_public_key(
                token=MOCK_TOKEN,
                url=MOCK_URL,
            )

    def test_InvalidTokenError(self, monkeypatch):
        """Test for invalid token."""
        monkeypatch.setattr(
            'foca.security.auth._get_public_keys',
            lambda **kwargs: MOCK_KEYS,
        )
        monkeypatch.setattr(
            'jwt.decode',
            lambda **kwargs: _raise(InvalidTokenError),
        )
        with pytest.raises(Unauthorized):
            _validate_jwt_public_key(
                token=MOCK_TOKEN,
                url=MOCK_URL,
            )

    def test_no_header_claims(self, monkeypatch):
        """Test for token without header claims."""
        monkeypatch.setattr(
            'foca.security.auth._get_public_keys',
            lambda **kwargs: MOCK_KEYS,
        )
        with pytest.raises(Unauthorized):
            _validate_jwt_public_key(
                token=MOCK_TOKEN_INVALID,
                url=MOCK_URL,
            )

    def test_kid_mismatch(self, monkeypatch):
        """Test for token and JWK set with mismatching JWK identifiers."""
        monkeypatch.setattr(
            'foca.security.auth._get_public_keys',
            lambda **kwargs: MOCK_KEYS,
        )
        with pytest.raises(KeyError):
            _validate_jwt_public_key(
                token=MOCK_TOKEN_HEADER_KID,
                url=MOCK_URL,
            )


class TestGetPublicKeys:
    """Tests for `_get_public_keys()`."""

    def test_success(self, monkeypatch):
        """Test for successfully fetching keys."""
        mock_jwk_set = {"keys": [MOCK_JWK, {}]}
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = mock_jwk_set
        monkeypatch.setattr('requests.get', request)
        res = _get_public_keys(url=MOCK_URL, pem=True)
        assert MOCK_JWK['kid'] in res

    def test_ConnectionError(self, monkeypatch):
        """Test for being unable to connect to keys endpoint."""
        monkeypatch.setattr(
            'requests.get',
            lambda **kwargs: _raise(ConnectionError)
        )
        with pytest.raises(ConnectionError):
            _get_public_keys(url=MOCK_URL)

    def test_non_public_key(self, monkeypatch):
        """Test for non-public keys."""
        mock_jwk_set = {"keys": [MOCK_JWK_PRIVATE]}
        request = MagicMock(name='requests')
        request.status_code = 200
        request.return_value.json.return_value = mock_jwk_set
        monkeypatch.setattr('requests.get', request)
        res = _get_public_keys(url=MOCK_URL)
        assert res == {}
