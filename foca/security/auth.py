"""Functions for validating JWT Bearer tokens."""

from connexion.exceptions import Unauthorized
import logging
from typing import (Dict, Iterable, List, Optional)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from flask import current_app
import jwt
from jwt.exceptions import InvalidKeyError
import requests
from requests.exceptions import ConnectionError
import json

# Get logger instance
logger = logging.getLogger(__name__)


def validate_token(token: str) -> Dict:
    """
    Validate JSON Web Token (JWT) Bearer token.

    Returns:
        Token claims.

    Raises:
        connexion.exceptions.Unauthorized: Raised if JWT could not be
            successfully validated.
    """
    # Set parameters defined by OpenID Connect specification
    # Cf. https://openid.net/specs/openid-connect-discovery-1_0.html
    oidc_suffix_config: str = ".well-known/openid-configuration"
    oidc_config_claim_userinfo: str = 'userinfo_endpoint'
    oidc_config_claim_public_keys: str = 'jwks_uri'

    # Fetch security parameters
    conf = current_app.config['FOCA'].security.auth
    add_key_to_claims: bool = conf.add_key_to_claims
    allow_expired: bool = conf.allow_expired
    audience: Optional[Iterable[str]] = conf.audience
    claim_identity: str = conf.claim_identity
    claim_issuer: str = conf.claim_issuer
    algorithms: Iterable[str] = conf.algorithms
    validation_methods: List[str] = [e.value for e in conf.validation_methods]
    validation_checks: str = conf.validation_checks.value

    # Ensure that validation methods are configured
    if not len(validation_methods):
        raise Unauthorized(
            "Authentication is enabled, but no JWT validation methods "
            "configured"
        )

    # Decode JWT
    try:
        claims = jwt.decode(
            jwt=token,
            verify=False,
            algorithms=algorithms,
        )
    except Exception as e:
        raise Unauthorized("JWT could not be decoded") from e
    logger.debug(f"Decoded claims: {claims}")

    # Verify existence of issuer claim
    if claim_issuer not in claims:
        raise Unauthorized(
            f"Required identity claim not available: {claim_identity}"
        )

    # Get OIDC configuration
    url = f"{claims[claim_issuer].rstrip('/')}/{oidc_suffix_config}"
    logger.debug(f"Issuer's configuration URL: {url}")
    try:
        oidc_config = requests.get(url)
        oidc_config.raise_for_status()
    except Exception as e:
        raise Unauthorized(
            "Could not fetch issuer's configuration from: {url}"
        ) from e

    # Validate token
    passed_any = False
    for method in validation_methods:
        logger.debug(f"Validating JWT via method: {method}")
        try:
            if method == 'userinfo':
                _validate_jwt_userinfo(
                    url=oidc_config.json()[oidc_config_claim_userinfo],
                    token=token,
                )
            if method == 'public_key':
                _validate_jwt_public_key(
                    url=oidc_config.json()[oidc_config_claim_public_keys],
                    token=token,
                    algorithms=algorithms,
                    add_key_to_claims=add_key_to_claims,
                    audience=audience,
                    allow_expired=allow_expired,
                )
        except Exception as e:
            if validation_checks == 'all':
                raise Unauthorized(
                    "Insufficient number of JWT validation checks passed"
                ) from e
            continue
        passed_any = True
        if validation_checks == 'any':
            break
    if not passed_any:
        raise Unauthorized("No JWT validation checks passed")

    # Verify existence of specified identity claim
    if claim_identity not in claims:
        raise Unauthorized(
            f"Required identity claim '{claim_identity} not available"
        )

    # Log result
    logger.debug(f"Access granted to user: {claims[claim_identity]}")

    # Return token info
    return {
        'jwt': token,
        'claims': claims,
        'user_id': claims[claim_identity],
        'scope': claims.get('scope', ""),
    }


def _validate_jwt_userinfo(
    token: str,
    url: str,
    header_name: str = 'Authorization',
    prefix: str = 'Bearer',
) -> None:
    """Validate JSON Web Token (JWT) via an OpenID Connect-compliant
    identity provider's user info endpoint.

    Args:
        url: URL to OpenID Connect identity provider's user info endpoint.
        header_name: Name of the request header field at which the service is
            expecting the JWT. Cf. `prefix`.
        token: JSON Web Token (JWT).
        prefix: Prefix that the app expects to precede the JWT, separated
            by whitespace. Together, `prefix` and `token`, separated by
            whitespace, constitute the value of the request header field
            specified by `header-name`.

    Raises:
        requests.exceptions.ConnectionError: Raised if the identity provider's
            user info or configuration endpoints could not be reached.
    """
    logger.debug(f"Issuer's user info endpoint URL: {url}")
    headers = {f"{header_name}": f"{prefix} {token}"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except Exception as e:
        raise ConnectionError(f"Could not connect to endpoint '{url}'") from e
    logger.debug("Validation via user info endpoint succeeded")


def _validate_jwt_public_key(
    token: str,
    url: str,
    algorithms: Iterable[str] = ['RS256'],
    add_key_to_claims: bool = True,
    audience: Optional[Iterable[str]] = None,
    allow_expired: bool = False,
    claim_key_id: str = 'kid',
) -> None:
    """Validate JSON Web Token (JWT) via an OpenID Connect-compliant
    identity provider's public key.

    Args:
        url: URL to OpenID Connect identity provider's public keys endpoint.
        token: JSON Web Token (JWT).
        algorithms: Lists the JWT-signing algorithms supported by the app.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims.
        audience: List of audiences that the app identifies itself with. If
            specified, JSON Web Tokens (JWT) that do not contain any of the
            specified audiences are rejected. Set to ``None`` to disable
            audience validation.
        allow_expired: Allow/disallow expired JSON Web Tokens (JWT).
        claim_key_id: The JSON Web Token (JWT) claim used to specify the
            identifier of the JSON Web Key (JWK) used to issue that token.

    Returns:
        Dictionary of JWT claims, or an empty dictionary, if claims could not
        be successfully decoded.

    Raises:
        KeyError: Raised if used JSON Web Key (JWK) identifer was not found
            among public JWK set.
        Unauthorized: Raised if token could not be decoded.
    """
    logger.debug(f"Issuer's JWK set endpoint URL: {url}")

    # Obtain identity provider's public keys
    public_keys = _get_public_keys(
        url=url,
        pem=False,
        claim_key_id=claim_key_id,
    )

    # Extract JWT header claims, if available
    try:
        header_claims = jwt.get_unverified_header(token)
        logger.debug(f"Decoded header claims: {header_claims}")
    except Exception:
        logger.debug("Could not extract JWT header claims")
        header_claims = {}

    # Set used JWK identifier, if available
    try:
        jwk_id = header_claims[claim_key_id]
    except KeyError:
        logger.debug("JWT key ID not specified, trying all available JWKs")
        jwk_id = False

    # Verify that used JWK exists and remove all other JWKs
    if jwk_id:
        try:
            public_keys = {jwk_id: public_keys[jwk_id]}
        except KeyError:
            raise KeyError("JWT key ID not found among issuer's JWKs")

    # Set validations
    validation_options = {}
    if audience is None:
        validation_options['verify_aud'] = False
    if allow_expired:
        validation_options['verify_exp'] = False

    # Try public keys one after the other
    used_key = {}
    claims = {}
    for key in public_keys.values():
        used_key = key

        # Decode JWT and validate via public key
        try:
            claims = jwt.decode(
                jwt=token,
                verify=True,
                key=key,
                algorithms=algorithms,
                audience=audience,
                options=validation_options,
            )
        # Wrong or faulty key was used; try next one
        except InvalidKeyError as e:
            logger.debug(
                "JWT could not be decoded with current JWK '{key}': "
                f"{type(e).__name__}: {e}"
            )
        # Key seems okay but token is invalid
        except Exception as e:
            raise Unauthorized("JWT could not be validated") from e

        # Do not try other keys if token was decoded
        if claims:
            break

    # Verify that token was decoded
    if not claims:
        raise Unauthorized("JWT could not be validated with issuer's JWKs")

    # Add public key to claims
    if add_key_to_claims:
        claims['public_key'] = used_key

    # Log success and return claims
    logger.debug("Validation via issuer's public keys succeeded")


def _get_public_keys(
    url: str,
    pem: bool = False,
    claim_key_id: str = 'kid',
    claim_keys: str = 'keys',
) -> Dict[str, RSAPublicKey]:
    """Obtain the identity provider's public JSON Web Key (JWK) set.

    Args:
        url: Endpoint providing the identity provider's JSON Web Key (JWK) set.
        pem: Whether public JSON Web Keys (JWK) shall be returned in Privacy
            Enhanced-Mail (PEM) format rather than as JSON dumps.
        claim_key_id: The JWT claim encoding a JSON Web Key (JWK) identifier.
        claim_keys: The JSON Web Key (JWK).

    Returns:
        JSON Web Key (JWK) public keys mapped to their identifiers.

    Raises:
        requests.exceptions.ConnectionError: Raised if the identity provider's
            JWK endpoint could not be reached.
    """
    # Get JWK sets from identity provider
    try:
        response = requests.get(url)
        response.raise_for_status()
    except Exception as e:
        raise ConnectionError(f"Could not connect to endpoint '{url}'") from e

    # Iterate over JWK set and store public keys in dictionary
    public_keys = {}
    for jwk in response.json().get(claim_keys, []):
        try:
            key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))

            # Ensure key is public
            if not isinstance(key, RSAPublicKey):
                logger.warning(f"JSON Web Key '{jwk}' is not public.")
                continue

            # Convert to PEM if requested
            if pem:
                key = key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode('utf-8').encode('unicode_escape').decode('utf-8')

            public_keys[jwk[claim_key_id]] = key
        except Exception as e:
            logger.warning(
                f"JSON Web Key '{jwk}' could not be processed: "
                f"{type(e).__name__}: {e}"
            )

    # Return dictionary of public keys
    return public_keys
