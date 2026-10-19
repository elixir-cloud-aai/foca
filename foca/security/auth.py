"""Functions for validating JWT Bearer tokens."""

import logging
import time
from typing import Any

import jwt
import requests
from connexion.exceptions import Unauthorized
from cryptography.hazmat.primitives import serialization
from jwt.types import Options

from foca.config.access import get_config
from foca.models.config import AuthConfig
from foca.utils.logging import escape_log_value

# Get logger instance
logger = logging.getLogger(__name__)

# Path of OpenID Connect discovery documents, relative to the issuer
# Cf. https://openid.net/specs/openid-connect-discovery-1_0.html
OIDC_CONFIG_PATH = ".well-known/openid-configuration"

# Minimum time (in seconds) between fetching an issuer's JSON Web Key (JWK)
# set again because a token was signed with a key that is not in the set, and
# between attempts to fetch a discovery document or JWK set after a failure
JWKS_REFETCH_COOLDOWN = 30

# OpenID Connect discovery documents and JWK sets, with time of retrieval,
# by issuer and JWK set URL, respectively
_oidc_configs: dict[str, tuple[float, dict]] = {}
_jwk_sets: dict[str, tuple[float, jwt.PyJWKSet]] = {}
# Time of last failed attempt to fetch a discovery document or JWK set, by URL
_failed_fetches: dict[str, float] = {}


def validate_token(token: str) -> dict:
    """Validate JSON Web Token (JWT) Bearer token.

    Only tokens issued by one of the trusted issuers configured via
    `security.auth.issuers` are accepted. Tokens are verified locally with
    the issuer's public keys, obtained via OpenID Connect discovery, and,
    if configured, additionally via the issuer's user info endpoint.

    Args:
        token: JSON Web Token (JWT).

    Returns:
        Token info, including the verified claims.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the JWT could not be
            successfully validated.
    """
    conf: AuthConfig = get_config().security.auth
    if not conf._warnings_logged:
        log_auth_config_warnings(conf)
        conf._warnings_logged = True
    claims = _validate(token=token, conf=conf)
    logger.debug(
        f"Token issued by '{escape_log_value(claims['iss'])}' validated for "
        f"subject '{escape_log_value(claims[conf.claim_identity])}'"
    )

    return {
        "jwt": token,
        "claims": claims,
        "user_id": claims[conf.claim_identity],
        "scope": claims.get("scope", ""),
    }


def log_auth_config_warnings(conf: AuthConfig) -> None:
    """Log warnings about incomplete or insecure authentication settings.

    Args:
        conf: Authentication settings.
    """
    if not conf.issuers:
        logger.warning(
            "No trusted token issuers configured "
            "(`security.auth.issuers`): all bearer tokens will be rejected."
        )
    if conf.audience is None:
        logger.warning(
            "Token audience validation is disabled "
            "(`security.auth.audience`): tokens issued for other services "
            "by trusted issuers are accepted."
        )
    if conf.allow_expired:
        logger.warning(
            "Expired tokens are accepted (`security.auth.allow_expired`)."
        )


def _validate(token: str, conf: AuthConfig) -> dict:
    """Validate JSON Web Token (JWT).

    Args:
        token: JSON Web Token (JWT).
        conf: Authentication settings.

    Returns:
        Verified token claims.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the JWT could not be
            successfully validated.
    """
    # Reject tokens of untrusted issuers before any network request
    try:
        unverified_claims = jwt.decode(
            token, options={"verify_signature": False}
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized("JWT could not be decoded") from exc
    issuer = unverified_claims.get("iss")
    if not isinstance(issuer, str) or issuer not in conf.issuers:
        raise Unauthorized("JWT issuer is not trusted")

    oidc_config = _get_oidc_config(issuer=issuer, conf=conf)
    claims = _verify_signature(
        token=token,
        issuer=issuer,
        jwks_uri=oidc_config.get("jwks_uri"),
        conf=conf,
    )
    if conf.validate_userinfo:
        _validate_userinfo(
            token=token,
            subject=claims.get("sub"),
            url=oidc_config.get("userinfo_endpoint"),
            conf=conf,
        )
    return claims


def _fetch_json(
    url: Any, conf: AuthConfig, throttle: bool = False, **kwargs
) -> Any:
    """Fetch JSON document from identity provider.

    Args:
        url: URL of the document.
        conf: Authentication settings.
        throttle: Whether to refrain from fetching the document again within
            `JWKS_REFETCH_COOLDOWN` seconds after a failed attempt, so that
            requests do not amplify load on an identity provider during an
            outage. Use only for documents that do not depend on the token.
        **kwargs: Additional arguments for :py:func:`requests.get`.

    Returns:
        Parsed JSON document.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the document could not
            be fetched.
    """
    if not isinstance(url, str) or not url:
        raise Unauthorized("Identity provider endpoint not available")
    if throttle:
        failed = _failed_fetches.get(url)
        if (
            failed is not None
            and time.monotonic() - failed < JWKS_REFETCH_COOLDOWN
        ):
            raise Unauthorized(
                f"Document from identity provider temporarily unavailable: "
                f"{url}"
            )
    try:
        response = requests.get(
            url,
            timeout=conf.timeout,
            allow_redirects=False,
            **kwargs,
        )
        response.raise_for_status()
        document = response.json()
    except Exception as exc:
        if throttle:
            _failed_fetches[url] = time.monotonic()
        raise Unauthorized(
            f"Could not fetch document from identity provider: {url}"
        ) from exc
    _failed_fetches.pop(url, None)
    return document


def _get_oidc_config(issuer: str, conf: AuthConfig) -> dict:
    """Get OpenID Connect discovery document of a trusted issuer.

    Args:
        issuer: Trusted issuer.
        conf: Authentication settings.

    Returns:
        OpenID Connect discovery document.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the discovery document
            could not be fetched or was not issued for `issuer`.
    """
    cached = _oidc_configs.get(issuer)
    if cached is not None and time.monotonic() - cached[0] < conf.cache_ttl:
        return cached[1]
    oidc_config = _fetch_json(
        url=f"{issuer.rstrip('/')}/{OIDC_CONFIG_PATH}",
        conf=conf,
        throttle=True,
    )
    if (
        not isinstance(oidc_config, dict)
        or oidc_config.get("issuer") != issuer
    ):
        raise Unauthorized("OpenID Connect discovery document is invalid")
    _oidc_configs[issuer] = (time.monotonic(), oidc_config)
    return oidc_config


def _get_jwk_set(url: Any, conf: AuthConfig, refetch: bool = False):
    """Get JSON Web Key (JWK) set of a trusted issuer.

    Args:
        url: URL of the JWK set.
        conf: Authentication settings.
        refetch: Whether to fetch the JWK set again, e.g., because a token
            was signed with an unknown key. Ignored if the JWK set was
            fetched within the last `JWKS_REFETCH_COOLDOWN` seconds.

    Returns:
        JWK set.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the JWK set could not
            be fetched.
    """
    cached = _jwk_sets.get(url)
    if cached is not None:
        age = time.monotonic() - cached[0]
        if age < conf.cache_ttl and not (
            refetch and age >= JWKS_REFETCH_COOLDOWN
        ):
            return cached[1]
    try:
        jwk_set = jwt.PyJWKSet.from_dict(
            _fetch_json(url=url, conf=conf, throttle=True)
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized("Issuer's JWK set is invalid") from exc
    _jwk_sets[url] = (time.monotonic(), jwk_set)
    return jwk_set


def _verify_signature(
    token: str,
    issuer: str,
    jwks_uri: Any,
    conf: AuthConfig,
) -> dict:
    """Verify JSON Web Token (JWT) with the issuer's public keys.

    Args:
        token: JSON Web Token (JWT).
        issuer: Trusted issuer of the token.
        jwks_uri: URL of the issuer's JSON Web Key (JWK) set.
        conf: Authentication settings.

    Returns:
        Verified token claims.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the JWT could not be
            verified.
    """
    # Header can be decoded, as the token was decoded before
    key_id = jwt.get_unverified_header(token).get("kid")

    # Use key with matching ID, fetching JWK set again if key is unknown;
    # try all signing keys if token does not specify a key
    keys = []
    for refetch in (False, True):
        jwk_set = _get_jwk_set(url=jwks_uri, conf=conf, refetch=refetch)
        keys = [
            key
            for key in jwk_set.keys
            if key.public_key_use in (None, "sig")
            and (key_id is None or key.key_id == key_id)
        ]
        if keys:
            break
    if not keys:
        raise Unauthorized("JWT signing key not found among issuer's JWKs")

    options: Options = {
        "require": ["exp", "iat", "iss", conf.claim_identity],
        "verify_aud": conf.audience is not None,
        "verify_exp": not conf.allow_expired,
    }
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=conf.algorithms,
                audience=conf.audience,
                issuer=issuer,
                options=options,
            )
        # Try the next key if this one does not fit the token (e.g., another
        # key type or algorithm, if the token specifies no key ID); errors
        # with the claims apply to all keys
        except (
            jwt.InvalidSignatureError,
            jwt.InvalidAlgorithmError,
            jwt.InvalidKeyError,
        ):
            continue
        except jwt.PyJWTError as exc:
            raise Unauthorized(f"JWT could not be validated: {exc}") from exc
        if conf.add_key_to_claims:
            claims["public_key"] = key.key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
        return claims
    raise Unauthorized("JWT signature could not be verified")


def _validate_userinfo(
    token: str,
    subject: Any,
    url: Any,
    conf: AuthConfig,
) -> None:
    """Validate JSON Web Token (JWT) via the issuer's user info endpoint.

    Args:
        token: JSON Web Token (JWT), already verified locally.
        subject: Subject (`sub` claim) of the token.
        url: URL of the issuer's user info endpoint.
        conf: Authentication settings.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the user info could not
            be fetched or is not about the subject of the token.
    """
    userinfo = _fetch_json(
        url=url,
        conf=conf,
        headers={"Authorization": f"Bearer {token}"},
    )
    if not isinstance(userinfo, dict) or userinfo.get("sub") != subject:
        raise Unauthorized("User info does not match JWT subject")
