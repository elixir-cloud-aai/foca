"""Functions for validating JWT Bearer tokens."""

import copy
import hashlib
import ipaddress
import logging
import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import jwt
import requests
from connexion.operations import AbstractOperation
from connexion.resolver import Resolution, Resolver
from cryptography.hazmat.primitives import serialization
from flask import Flask, g, has_request_context, request
from jwt.types import Options
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import Unauthorized as AnyUnauthorized
from werkzeug.http import quote_header_value

from foca.config.access import _get_app_config, get_config
from foca.errors import Unauthorized
from foca.models.config import (
    NAMESPACE_SEPARATOR,
    AuthConfig,
    ServiceClientConfig,
)
from foca.security.resolvers import _normalize_resolver
from foca.utils.http import _redact
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

# Claims identifying the client a token was issued to, in order of
# precedence: `client_id` of access tokens as per RFC 9068,
# `azp` (authorized party) of identity providers using OpenID Connect's claim
CLIENT_CLAIMS = ("client_id", "azp")

# Description of the `invalid_token` error of `WWW-Authenticate` challenges,
# deliberately generic, so that clients learn nothing about the identity
# provider or why validation failed
_INVALID_TOKEN_DESCRIPTION = "The access token could not be validated"  # noqa: S105 (not a password)

# Attribute of `flask.g` with the rejection of the token of the request
_TOKEN_ERROR_ATTRIBUTE = "foca_token_error"  # noqa: S105 (attribute name)
# Attribute of `flask.g` set if the token of the request was found valid
_TOKEN_VALID_ATTRIBUTE = "foca_token_valid"  # noqa: S105 (attribute name)
# Name of `validate_token()` in security schemes (`x-bearerInfoFunc`)
_VALIDATE_TOKEN_FUNCTION = "foca.security.auth.validate_token"  # noqa: S105 (name)

# OpenID Connect discovery documents and JWK sets, with time of retrieval,
# by issuer and JWK set URL, respectively
_oidc_configs: dict[str, tuple[float, dict[str, Any]]] = {}
_jwk_sets: dict[str, tuple[float, jwt.PyJWKSet]] = {}
# Time of last failed attempt to fetch a discovery document or JWK set, by URL
_failed_fetches: dict[str, float] = {}
# User info, with time until which it is valid, by hash of token
_userinfo_cache: dict[str, tuple[float, float, dict[str, Any]]] = {}
_userinfo_cache_lock = threading.Lock()


def clear_caches() -> None:
    """Forget what was fetched from identity providers, e.g., between tests."""
    _oidc_configs.clear()
    _jwk_sets.clear()
    _failed_fetches.clear()
    with _userinfo_cache_lock:
        _userinfo_cache.clear()


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
    challenge = _BearerChallenge("bearer", parameters).to_header()
    return Unauthorized(description, headers={"WWW-Authenticate": challenge})


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
    issuer's user info endpoint. For service tokens of service clients (cf.
    `security.auth.service_clients` and :py:func:`get_service_client`), user
    info is not requested.

    Args:
        token: JSON Web Token (JWT).

    Returns:
        Token info: the token (`jwt`), the verified claims (`claims`), the
        subject (`user_id`), the scope (`scope`) and, for service tokens,
        the client ID of the service client (`service_client`; `None` for
        other tokens).

    Raises:
        foca.errors.Unauthorized: Raised if the JWT could not be
            successfully validated.
    """
    conf: AuthConfig = get_config().security.auth
    if not conf._warnings_logged:
        log_auth_config_warnings(conf)
        conf._warnings_logged = True
    try:
        claims = _validate(token=token, conf=conf)
    except AnyUnauthorized as exc:
        # Kept, so that it needn't be validated again (cf.
        # `_reject_invalid_token()`)
        if has_request_context():
            setattr(g, _TOKEN_ERROR_ATTRIBUTE, exc)
        raise
    if has_request_context():
        setattr(g, _TOKEN_VALID_ATTRIBUTE, True)
    # Logs the issuer and subject only, not the token (a false positive of
    # semgrep's registry rules, `p/python`)
    # nosemgrep: python-logger-credential-disclosure
    logger.debug(
        "Token issued by '%s' validated for subject '%s'",
        escape_log_value(claims["iss"]),
        escape_log_value(claims[conf.claim_identity]),
    )

    service_client = get_service_client(claims=claims, conf=conf)
    return {
        "jwt": token,
        "claims": claims,
        "user_id": claims[conf.claim_identity],
        "scope": claims.get("scope", ""),
        "service_client": None if service_client is None else service_client.client_id,
    }


def _get_bearer_token() -> str | None:
    """Get the bearer token sent with the current request.

    Returns:
        The token in the `Authorization` header if its scheme is `Bearer`
        (in any case, cf. RFC 6750; empty if it has none), or `None`. Split
        at any whitespace, as Connexion does.
    """
    parts = request.headers.get("Authorization", "").split(None, 1)
    if not parts or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def _reject_invalid_token(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator rejecting invalid tokens on optional authentication.

    Connexion handles requests whose token it can't validate as anonymous on
    such operations. Here, those with a token that is invalid are rejected
    (`401`, with an `invalid_token` challenge), as on operations requiring
    tokens. The rejection of Connexion's validation is used, if any (cf.
    :py:func:`validate_token`); tokens are validated only if Connexion didn't
    (e.g., if the empty security requirement comes first, or another scheme
    of the operation accepted the request). The decorator only
    rejects: requests that Connexion left anonymous stay anonymous, also with
    a valid token. If the token can't be validated for other reasons (e.g.,
    the identity provider is unavailable), the request stays anonymous, so
    that public operations keep working, and this is logged.

    Args:
        fn: Controller.

    Returns:
        The decorated controller.
    """

    @wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _get_bearer_token()
        # Not Connexion's `token_info`, which another security scheme may have
        # set for the same request
        if token is not None and not g.get(_TOKEN_VALID_ATTRIBUTE):
            try:
                error = g.pop(_TOKEN_ERROR_ATTRIBUTE, None)
                if error is not None:
                    raise error
                validate_token(token)
            except AnyUnauthorized as exc:
                if _has_invalid_token_challenge(exc):
                    raise
                # Logs why the token was rejected, not the token (a false positive
                # of semgrep's registry rules, `p/python`)
                # nosemgrep: python-logger-credential-disclosure
                logger.info(
                    "Token not validated, request handled as anonymous: %s",
                    escape_log_value(str(exc.description)),
                )
        return fn(*args, **kwargs)

    return _wrapper


def _has_invalid_token_challenge(exc: AnyUnauthorized) -> bool:
    """Check whether an error has a Bearer challenge for an invalid token.

    Args:
        exc: Error of the token validation.

    Returns:
        Whether a challenge has the scheme `Bearer` and the error code
        `invalid_token`.
    """
    for name, value in exc.get_headers():
        if name.lower() != "www-authenticate":
            continue
        challenge = WWWAuthenticate.from_header(value)
        if (
            challenge is not None
            and challenge.type == "bearer"
            and challenge.get("error") == "invalid_token"
        ):
            return True
    return False


def _validates_optional_token(operation: AbstractOperation) -> bool:
    """Check whether an operation takes optional tokens validated by FOCA.

    Args:
        operation: Connexion operation.

    Returns:
        Whether its effective security requirements have an empty alternative
        (allowing anonymous requests) and one with a scheme validated by
        :py:func:`validate_token`. Tokens of operations with other schemes,
        e.g., API keys or other validation functions, are not FOCA's to
        judge.
    """
    security = operation.security or []
    schemes = operation.security_schemes or {}
    return not all(security) and any(
        schemes.get(name, {}).get("x-bearerInfoFunc") == _VALIDATE_TOKEN_FUNCTION
        for alternative in security
        for name in alternative
    )


def _other_token_validators(operation: AbstractOperation) -> set[str]:
    """Get the functions validating an operation's optional tokens otherwise.

    Args:
        operation: Connexion operation, for which
            :py:func:`_validates_optional_token` is `False`.

    Returns:
        Names of the functions in `x-bearerInfoFunc` and `x-tokenInfoFunc` of
        its schemes, if its effective security requirements have an empty
        alternative.
    """
    security = operation.security or []
    if all(security):
        return set()
    schemes = operation.security_schemes or {}
    return {
        function
        for alternative in security
        for name in alternative
        for field in ("x-bearerInfoFunc", "x-tokenInfoFunc")
        if (function := schemes.get(name, {}).get(field)) is not None
    }


class InvalidTokenResolver(Resolver):
    """Connexion resolver rejecting invalid tokens on optional authentication.

    Used for all API specifications if `security.auth.reject_invalid_tokens`
    is set: resolved functions of operations with optional authentication
    by FOCA's token validation (security requirements with an empty
    alternative and a scheme naming :py:func:`validate_token`, e.g.,
    `[{bearerAuth: []}, {}]`) are wrapped, so that invalid tokens are
    rejected. Other operations are not changed; a warning names other
    functions that validate optional tokens (e.g., wrappers of
    :py:func:`validate_token`), once per function and resolver, i.e., API
    specification, as the setting doesn't reach them.

    Args:
        resolver: Resolver to resolve functions with (any object with a
            `resolve()` method), or a function resolving operation
            identifiers to functions, as accepted by Connexion. Defaults to
            Connexion's default resolver.

    Attributes:
        resolver: Resolver to resolve functions with.
    """

    def __init__(self, resolver: Resolver | Callable[..., Any] | None = None):
        """Construct resolver."""
        super().__init__()
        self.resolver = _normalize_resolver(resolver)
        self._warned: set[str] = set()

    def _warn_other_validators(self, operation: AbstractOperation) -> None:
        """Warn of functions validating tokens that the setting doesn't reach.

        Args:
            operation: Connexion operation that wasn't wrapped.
        """
        for function in sorted(_other_token_validators(operation) - self._warned):
            self._warned.add(function)
            logger.warning(
                "'security.auth.reject_invalid_tokens' has no effect on operations "
                "with optional authentication by '%s': it only applies to schemes "
                "of type 'http' and scheme 'bearer' naming %s in 'x-bearerInfoFunc'. "
                "Use such a scheme, or reject invalid tokens in your own function.",
                function,
                _VALIDATE_TOKEN_FUNCTION,
            )

    def resolve(self, operation: AbstractOperation) -> Resolution:
        """Resolve operation and wrap function of optional authentication.

        Args:
            operation: Connexion operation to resolve.

        Returns:
            Resolution of the operation.
        """
        resolution = self.resolver.resolve(operation)
        if not _validates_optional_token(operation):
            self._warn_other_validators(operation)
            return resolution
        return Resolution(
            function=_reject_invalid_token(resolution.function),
            operation_id=resolution.operation_id,
        )


def get_token_client(claims: dict[str, Any]) -> str | None:
    """Get the client that a token was issued to.

    Args:
        claims: Claims of the token.

    Returns:
        Value of the first of the claims `client_id` (RFC 9068) and `azp`
        that the token has, if it is a non-empty string; otherwise `None`.
    """
    for claim in CLIENT_CLAIMS:
        if claim in claims:
            client = claims[claim]
            return client if isinstance(client, str) and client else None
    return None


def get_service_client(
    claims: dict[str, Any], conf: AuthConfig
) -> ServiceClientConfig | None:
    """Get the service client of a service token.

    A token is a service token if it was issued by the issuer of a service
    client (by default, the only trusted issuer) to the client (cf.
    :py:func:`get_token_client`), and its subject (`conf.claim_identity`) is
    the subject of the client's service tokens (by default, its client ID).
    Other tokens issued to service clients, e.g., tokens of users, are user
    tokens.

    Args:
        claims: Claims of a validated token.
        conf: Authentication settings, with the service clients
            (`service_clients`).

    Returns:
        Service client of the token, or `None` if the token is not a
        service token.
    """
    client_id = get_token_client(claims)
    # Implied issuer of service clients, if only one issuer is trusted
    for client in conf.service_clients:
        if client.client_id == client_id:
            issuer = get_client_issuer(client, conf)
            subject = client.client_id if client.subject is None else client.subject
            is_service_token = (
                issuer is not None
                and claims.get("iss") == issuer
                and claims.get(conf.claim_identity) == subject
            )
            return client if is_service_token else None
    return None


def get_client_issuer(client: ServiceClientConfig, conf: AuthConfig) -> str | None:
    """Get the issuer of the service tokens of a service client.

    Args:
        client: Service client.
        conf: Authentication settings.

    Returns:
        The client's `issuer` or, if not set, the only trusted issuer;
        `None` if neither is set.
    """
    if client.issuer is not None:
        return client.issuer
    issuers = conf.issuer_urls
    return issuers[0] if len(issuers) == 1 else None


def namespace_subject(
    subject: str, issuer: Any, conf: AuthConfig | None = None
) -> str | None:
    """Namespace a subject by its issuer, as used for access control.

    Subjects are namespaced as `<namespace>|<subject>` if the issuer has a
    namespace (cf. :py:class:`foca.models.config.IssuerConfig`), so that
    equal subjects of different issuers are different users. Use this to
    name users of an issuer, e.g., in policy rules or when granting them
    roles.

    Args:
        subject: Subject, as issued (e.g., the `sub` claim).
        issuer: Issuer of the subject (`iss` claim); any value, as claims
            are untyped, but only trusted issuers are namespaced.
        conf: Authentication settings; defaults to those of the current
            app.

    Returns:
        The subject, namespaced if its issuer has a namespace; `None` if
        subjects are namespaced, but the issuer is not trusted, or the
        subject is bare and contains `|`, so that it could collide with a
        namespaced subject.
    """
    if conf is None:
        conf = get_config().security.auth
    namespaces = conf.get_namespaces()
    if not any(namespaces.values()):
        return subject
    if not isinstance(issuer, str) or issuer not in namespaces:
        return None
    namespace = namespaces[issuer]
    if namespace is None:
        return None if NAMESPACE_SEPARATOR in subject else subject
    return f"{namespace}{NAMESPACE_SEPARATOR}{subject}"


def _reached_without_tls(issuer: str) -> bool:
    """Check whether an issuer is reached without TLS, outside the host.

    Args:
        issuer: Issuer.

    Returns:
        Whether it is an `http` URL of a host other than the loopback
        interface; URLs that can't be parsed count as such.
    """
    try:
        parts = urlsplit(issuer)
        if parts.scheme.lower() != "http":
            return False
        host = parts.hostname
        return host != "localhost" and not ipaddress.ip_address(host or "").is_loopback
    except ValueError:  # host names and invalid URLs
        return True


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
    for issuer in conf.issuer_urls:
        if _reached_without_tls(issuer):
            logger.warning(
                "Trusted issuer %s is reached without TLS: anyone on the network "
                "can change its signing keys, and so forge what it signs.",
                _redact(issuer),
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
    if conf.on_behalf_of_header is not None and not any(
        client.on_behalf_of for client in conf.service_clients
    ):
        logger.warning(
            "An on-behalf-of header is configured "
            "(`security.auth.on_behalf_of_header`), but no service client may "
            "act for users (`on_behalf_of`): requests naming users in the "
            "header are rejected."
        )


def require_no_service_clients(app: Flask | None = None) -> None:
    """Refuse service clients, for apps whose endpoints don't support them.

    With service clients (`security.auth.service_clients`), requests may be
    made by clients instead of users, or by clients for users they name
    (`security.auth.on_behalf_of_header`), e.g., FOCA's resource ownership
    helpers attribute new resources to them. Apps whose endpoints take the
    token's subject as the user call this check on startup, e.g., after
    `Foca.create_app()`; FOCA doesn't call it.

    Args:
        app: Flask app to check (e.g., `Foca.create_app().app`). If not
            given, the current FOCA app is checked: the app of the current
            app context, if any, otherwise the most recently created one
            (cf. :py:func:`foca.config.access.get_config`).

    Raises:
        RuntimeError: Service clients or the header naming the users they
            act for are configured for the app.
    """
    config = get_config() if app is None else _get_app_config(app)
    auth = config.security.auth
    if auth.service_clients or auth.on_behalf_of_header is not None:
        raise RuntimeError(
            "Service clients are not supported by this app: remove "
            "`security.auth.service_clients` and "
            "`security.auth.on_behalf_of_header`."
        )


def _validate(token: str, conf: AuthConfig) -> dict[str, Any]:
    """Validate JSON Web Token (JWT).

    Args:
        token: JSON Web Token (JWT).
        conf: Authentication settings.

    Returns:
        Verified token claims.

    Raises:
        foca.errors.Unauthorized: Raised if the JWT could not be
            successfully validated.
    """
    # Reject tokens of untrusted issuers before any network request; the
    # signature is verified in `_verify_signature()`
    try:
        # nosemgrep: jwt-decode-without-verification
        unverified_claims = jwt.decode(
            token,
            # Verified later, as above (semgrep's registry rules, `p/python`)
            # nosemgrep: unverified-jwt-decode
            options={"verify_signature": False},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized("JWT could not be decoded") from exc
    issuer = unverified_claims.get("iss")
    if not isinstance(issuer, str) or issuer not in conf.issuer_urls:
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
    missing_claims = [
        claim for claim in conf.get_userinfo_claims(issuer) if claim not in claims
    ]
    service_client = get_service_client(claims=claims, conf=conf)
    if service_client is not None:
        # Service tokens represent clients, which have no user info
        logger.debug(
            "Request of service client '%s': user info not requested",
            escape_log_value(service_client.client_id),
        )
    elif conf.validate_userinfo or missing_claims:
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
        foca.errors.Unauthorized: Raised if the document could not
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
        foca.errors.Unauthorized: Raised if the discovery document
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
        foca.errors.Unauthorized: Raised if the JWK set could not
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
    # Invalid JWK sets are throttled like failed fetches (see `_fetch_json()`)
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        _failed_fetches[url] = time.monotonic()
        raise _unauthorized("Issuer's JWK set is invalid", invalid_token=False)
    # For some badly typed key members (e.g., a list as `alg`, or numbers as
    # key material in 2.11), PyJWT raises `TypeError` or `ValueError`
    try:
        jwk_set = jwt.PyJWKSet.from_dict(document)
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        _failed_fetches[url] = time.monotonic()
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
        foca.errors.Unauthorized: Raised if the JWT could not be
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
                audience=conf.get_audience(issuer),
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
        foca.errors.Unauthorized: Raised if the client is not
            accepted or the token has neither claim.
    """
    client = get_token_client(claims)
    if client is None or client not in (conf.clients or []):
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
    `userinfo_cache_ttl` seconds (by default, `cache_ttl`), but not beyond the
    token's expiry.

    Args:
        token: JSON Web Token (JWT), already verified locally.
        claims: Verified claims of the token.
        url: URL of the issuer's user info endpoint.
        conf: Authentication settings.

    Returns:
        User info.

    Raises:
        foca.errors.Unauthorized: Raised if the user info could not
            be fetched or is not about the subject of the token.
    """
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    ttl = conf.get_userinfo_cache_ttl()
    if not conf.validate_userinfo:
        with _userinfo_cache_lock:
            cached = _userinfo_cache.get(key)
        now = time.monotonic()
        if cached is not None and now < cached[1] and now - cached[0] < ttl:
            return cached[2]
    # The age of the entry counts from before the request, and a slow response
    # doesn't replace or take precedence over what a later request found (e.g.,
    # after a revocation)
    started = time.monotonic()
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
        lifetime = min(ttl, float(claims["exp"]) - time.time())
    except (TypeError, ValueError):
        lifetime = 0
    with _userinfo_cache_lock:
        cached = _userinfo_cache.get(key)
        if cached is not None and cached[0] > started:
            return userinfo if conf.validate_userinfo else cached[2]
        _userinfo_cache.pop(key, None)
        now = time.monotonic()
        # Not if it expired during the request: it would evict a live entry
        if not conf.validate_userinfo and started + lifetime > now:
            for cached_key, entry in list(_userinfo_cache.items()):
                if entry[1] <= now:
                    del _userinfo_cache[cached_key]
            while len(_userinfo_cache) >= USERINFO_CACHE_SIZE:
                del _userinfo_cache[next(iter(_userinfo_cache))]
            _userinfo_cache[key] = (started, started + lifetime, userinfo)
    return userinfo
