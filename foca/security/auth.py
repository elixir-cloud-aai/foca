"""Functions for validating JWT Bearer tokens."""

import copy
import hashlib
import logging
import threading
import time
from typing import Any

import jwt
import requests
from connexion.exceptions import Unauthorized
from cryptography.hazmat.primitives import serialization
from jwt.types import Options
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.http import quote_header_value

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

# Maximum number of tokens whose user info is cached
USERINFO_CACHE_SIZE = 1000

# Description of the `invalid_token` error of `WWW-Authenticate` challenges,
# deliberately generic, so that clients learn nothing about the identity
# provider or why validation failed
_INVALID_TOKEN_DESCRIPTION = "The access token could not be validated"  # noqa: S105 (not a password)

# OpenID Connect discovery documents and JWK sets, with time of retrieval,
# by issuer and JWK set URL, respectively
_oidc_configs: dict[str, tuple[float, dict[str, Any]]] = {}
_jwk_sets: dict[str, tuple[float, jwt.PyJWKSet]] = {}
# Time of last failed attempt to fetch a discovery document or JWK set, by URL
_failed_fetches: dict[str, float] = {}
# User info, with time until which it is valid, by hash of token
_userinfo_cache: dict[str, tuple[float, float, dict[str, Any]]] = {}
_userinfo_cache_lock = threading.Lock()


class _BearerChallenge(WWWAuthenticate):
    """`WWW-Authenticate` challenge of the Bearer scheme (RFC 6750, §3).

    Unlike for :py:class:`werkzeug.datastructures.WWWAuthenticate`, parameter
    values are always quoted, as in RFC 6750.
    """

    def to_header(self) -> str:
        """Produce `WWW-Authenticate` header value of the challenge.

        Returns:
            Header value, e.g., `Bearer error="invalid_token"`.
        """
        if not self.parameters:
            return "Bearer"
        parameters = ", ".join(
            f"{key}={quote_header_value(value, allow_token=False)}"
            for key, value in self.parameters.items()
        )
        return f"Bearer {parameters}"


def _unauthorized(description: str, invalid_token: bool = True) -> Unauthorized:
    """Create `401 Unauthorized` exception with a Bearer challenge.

    As per RFC 6750, §3.1, the challenge (`WWW-Authenticate`) has the error
    code `invalid_token` and a generic description if the token is invalid,
    and no error code otherwise, e.g., if the token could not be validated
    because the identity provider is not available, so that clients do not
    discard tokens or make users log in again in vain.

    Args:
        description: Description of the exception; logged, not sent to
            clients.
        invalid_token: Whether the token is invalid.

    Returns:
        Exception with the challenge.
    """
    parameters: dict[str, str | None] | None = (
        {
            "error": "invalid_token",
            "error_description": _INVALID_TOKEN_DESCRIPTION,
        }
        if invalid_token
        else None
    )
    return Unauthorized(
        description,
        www_authenticate=_BearerChallenge("bearer", parameters),
    )


def validate_token(token: str) -> dict[str, Any]:
    """Validate JSON Web Token (JWT) Bearer token.

    Only tokens issued by one of the trusted issuers configured via
    `security.auth.issuers` are accepted, only OAuth 2.0 access tokens (`typ`
    header as per `security.auth.token_types`, unless
    `security.auth.verify_token_type` is disabled), and, if configured via
    `security.auth.clients`, only tokens obtained by one of the accepted
    clients. Tokens are verified locally with the issuer's public keys,
    obtained via OpenID Connect discovery, and, if configured, additionally
    via the issuer's user info endpoint. Claims configured via
    `security.auth.userinfo_claims` that the token lacks are added from the
    issuer's user info endpoint.

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
    # Logs the issuer and subject only, not the token
    logger.debug(  # nosemgrep
        "Token issued by '%s' validated for subject '%s'",
        escape_log_value(claims["iss"]),
        escape_log_value(claims[conf.claim_identity]),
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
    if not conf.verify_audience and conf.clients is None:
        logger.warning(
            "Token audience validation is disabled "
            "(`security.auth.verify_audience`) and clients are not restricted "
            "(`security.auth.clients`): tokens issued for other services by "
            "trusted issuers are accepted."
        )
    if not conf.verify_token_type:
        logger.warning(
            "Token type validation is disabled "
            "(`security.auth.verify_token_type`): JSON Web Tokens of trusted "
            "issuers that are not access tokens, such as ID tokens, are accepted."
        )
    if conf.allow_expired:
        logger.warning("Expired tokens are accepted (`security.auth.allow_expired`).")


def _validate(token: str, conf: AuthConfig) -> dict[str, Any]:
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
    # Reject tokens of untrusted issuers before any network request; the
    # signature is verified in `_verify_signature()`
    try:
        unverified_claims = jwt.decode(
            token,
            options={"verify_signature": False},  # nosemgrep
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized("JWT could not be decoded") from exc
    issuer = unverified_claims.get("iss")
    if not isinstance(issuer, str) or issuer not in conf.issuers:
        raise _unauthorized("JWT issuer is not trusted")

    # Reject JWTs that are not access tokens (e.g., ID tokens) before any
    # network request; the `typ` header is a media type, so compared
    # case-insensitively (RFC 7515, §4.1.9), ignoring surrounding whitespace
    # in the configured types
    if conf.verify_token_type:
        # Header can be decoded, as the token was decoded before
        token_type = jwt.get_unverified_header(token).get("typ")
        if not isinstance(token_type, str) or token_type.lower() not in {
            typ.strip().lower() for typ in conf.token_types
        }:
            raise _unauthorized("JWT type is not accepted")

    oidc_config = _get_oidc_config(issuer=issuer, conf=conf)
    claims = _verify_signature(
        token=token,
        issuer=issuer,
        jwks_uri=oidc_config.get("jwks_uri"),
        conf=conf,
    )
    if conf.clients is not None:
        _verify_client(claims=claims, conf=conf)
    missing_claims = [claim for claim in conf.userinfo_claims if claim not in claims]
    if conf.validate_userinfo or missing_claims:
        userinfo = _get_userinfo(
            token=token,
            claims=claims,
            url=oidc_config.get("userinfo_endpoint"),
            conf=conf,
        )
        for claim in missing_claims:
            if claim in userinfo:
                claims[claim] = copy.deepcopy(userinfo[claim])
    return claims


def _fetch_json(
    url: Any, conf: AuthConfig, throttle: bool = False, **kwargs: Any
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
        raise _unauthorized(
            "Identity provider endpoint not available", invalid_token=False
        )
    if throttle:
        failed = _failed_fetches.get(url)
        if failed is not None and time.monotonic() - failed < JWKS_REFETCH_COOLDOWN:
            raise _unauthorized(
                f"Document from identity provider temporarily unavailable: {url}",
                invalid_token=False,
            )
    status = None
    try:
        response = requests.get(
            url,
            timeout=conf.timeout,
            allow_redirects=False,
            **kwargs,
        )
        status = response.status_code
        response.raise_for_status()
        document = response.json()
    except Exception as exc:
        if throttle:
            _failed_fetches[url] = time.monotonic()
        # Only rejections of the token by the user info endpoint (`401`)
        # make it invalid; documents that do not depend on the token
        # (throttled ones), failures to respond and `403` (e.g., for
        # insufficient scope) do not
        raise _unauthorized(
            f"Could not fetch document from identity provider: {url}",
            invalid_token=not throttle and status == 401,
        ) from exc
    _failed_fetches.pop(url, None)
    return document


def _get_oidc_config(issuer: str, conf: AuthConfig) -> dict[str, Any]:
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
    if not isinstance(oidc_config, dict) or oidc_config.get("issuer") != issuer:
        raise _unauthorized(
            "OpenID Connect discovery document is invalid",
            invalid_token=False,
        )
    _oidc_configs[issuer] = (time.monotonic(), oidc_config)
    return oidc_config


def _get_jwk_set(url: Any, conf: AuthConfig, refetch: bool = False) -> jwt.PyJWKSet:
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
            be fetched or is invalid (e.g., not a JSON object).
    """
    cached = _jwk_sets.get(url)
    if cached is not None:
        age = time.monotonic() - cached[0]
        if age < conf.cache_ttl and not (refetch and age >= JWKS_REFETCH_COOLDOWN):
            return cached[1]
    document = _fetch_json(url=url, conf=conf, throttle=True)
    # JWK sets are JSON objects with a list of JSON objects as `keys` (RFC
    # 7517, §5); PyJWT does not check the structure in all versions
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        raise _unauthorized("Issuer's JWK set is invalid", invalid_token=False)
    try:
        jwk_set = jwt.PyJWKSet.from_dict(document)
    except jwt.PyJWTError as exc:
        raise _unauthorized("Issuer's JWK set is invalid", invalid_token=False) from exc
    _jwk_sets[url] = (time.monotonic(), jwk_set)
    return jwk_set


def _verify_signature(
    token: str,
    issuer: str,
    jwks_uri: Any,
    conf: AuthConfig,
) -> dict[str, Any]:
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
        raise _unauthorized("JWT signing key not found among issuer's JWKs")

    options: Options = {
        "require": ["exp", "iat", "iss", conf.claim_identity],
        "verify_aud": conf.verify_audience,
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
            raise _unauthorized(f"JWT could not be validated: {exc}") from exc
        if conf.add_key_to_claims:
            claims["public_key"] = key.key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
        return claims
    raise _unauthorized("JWT signature could not be verified")


def _verify_client(claims: dict[str, Any], conf: AuthConfig) -> None:
    """Verify that a JSON Web Token (JWT) was obtained by an accepted client.

    The client is identified by the `client_id` claim (RFC 9068, §2.2) or,
    if the token has no `client_id` claim, by the `azp` claim (OpenID
    Connect Core 1.0, §2), which some identity providers use instead.

    Args:
        claims: Verified claims of the token.
        conf: Authentication settings; `clients` is set.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the client is not
            accepted or the token has neither claim.
    """
    client = claims["client_id"] if "client_id" in claims else claims.get("azp")
    # Claim values may be of any type; only strings can name a client
    if not isinstance(client, str) or client not in (conf.clients or []):
        raise _unauthorized("JWT client is not accepted")


def _get_userinfo(
    token: str,
    claims: dict[str, Any],
    url: Any,
    conf: AuthConfig,
) -> dict[str, Any]:
    """Get user info for a JSON Web Token (JWT) from the issuer.

    With `validate_userinfo`, user info is fetched for every validation, so
    that revoked tokens are detected. Otherwise, it is cached per token for
    `cache_ttl` seconds, but not beyond the token's expiry.

    Args:
        token: JSON Web Token (JWT), already verified locally.
        claims: Verified claims of the token.
        url: URL of the issuer's user info endpoint.
        conf: Authentication settings.

    Returns:
        User info.

    Raises:
        connexion.exceptions.Unauthorized: Raised if the user info could not
            be fetched or is not about the subject of the token.
    """
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not conf.validate_userinfo:
        with _userinfo_cache_lock:
            cached = _userinfo_cache.get(key)
        now = time.monotonic()
        if cached is not None and now < cached[1] and now - cached[0] < conf.cache_ttl:
            return cached[2]
    userinfo = _fetch_json(
        url=url,
        conf=conf,
        headers={"Authorization": f"Bearer {token}"},
    )
    subject = claims.get("sub")
    if (
        not isinstance(subject, str)
        or not isinstance(userinfo, dict)
        or userinfo.get("sub") != subject
    ):
        raise _unauthorized("User info does not match JWT subject")
    try:
        lifetime = min(conf.cache_ttl, float(claims["exp"]) - time.time())
    except (TypeError, ValueError):
        lifetime = 0
    with _userinfo_cache_lock:
        _userinfo_cache.pop(key, None)
        if not conf.validate_userinfo and lifetime > 0:
            now = time.monotonic()
            for cached_key, entry in list(_userinfo_cache.items()):
                if entry[1] <= now:
                    del _userinfo_cache[cached_key]
            while len(_userinfo_cache) >= USERINFO_CACHE_SIZE:
                del _userinfo_cache[next(iter(_userinfo_cache))]
            _userinfo_cache[key] = (now, now + lifetime, userinfo)
    return userinfo
