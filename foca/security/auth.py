"""Decorator and utility functions for securing access to endpoints."""

from connexion.exceptions import Unauthorized
from connexion import request
from functools import wraps
import logging
from typing import (Callable, Iterable, List, Mapping, Optional)

from cryptography.hazmat.primitives import serialization
import jwt
from jwt.exceptions import InvalidKeyError
import requests
import json

# Get logger instance
logger = logging.getLogger(__name__)


def param_pass(
    required: bool = True,
    add_key_to_claims: bool = False,
    allow_expired: bool = False,
    audience: Optional[Iterable[str]] = None,
    claim_identity: str = "sub",
    claim_issuer: str = "iss",
    claim_key_id: str = "kid",
    header_name: str = "Authorization",
    token_prefix: str = "Bearer",
    algorithms: Iterable[str] = ["RS256"],
    validation_methods: List[str] = ["userinfo", "public_key"],
    validation_checks: str = "all",
):
    """Decorator for protecting an endpoint against access without a valid
    JSON Web Token (JWT)-based authorization token.

    Args:
        required: Enable/disable JWT validation for endpoints decorated with
            the `@jwt_validation` decorator defined in `foca.security.auth`.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims when handling requests to
            `@jwt_validation`-decorated endpoints.
        allow_expired: Allow/disallow expired JWTs. If `False`, a `401`
            authorization error is raised in response to a request containing
            an expired JWT.
        audience: List of audiences that the app identifies itself with. If
            specified, JWTs that do not contain any of the specified audiences
            are rejected. If `None`, audience validation is disabled.
        claim_identity: The JWT claim used to identify the sender.
        claim_issuer: The JWT claim used to identify the issuer.
        claim_key_id: The JWT claim used to identify the JWK used when the JWT
            was issued.
        header_name: Name of the request header field at which the app is
            expecting the JWT. Cf. `--token-prefix`.
        token_prefix: Prefix that the app expects to precede the JWT, separated
            by whitespace. Together, prefix and JWT constitute the value of
            the request header field specified by `--header-name`.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validation_methods: Lists the methods to be used to validate a JWT.
            Valid choices are `userinfo` and `public_key`. In the former case,
            validation happens via an OpenID Connect-compliant identify
            provider's `/userinfo` endpoint, in the latter via the identity
            provider's JSON Web Key.
        validation_checks: Specify how many of the `validation_methods` need
            to pass before accepting a JWT. One of `any` and `all`. In the
            former case, JWT validation succeeds after the first successful
            validation check, in the latter case, JWT validation fails after
            the first unsuccessful validation check.
    """
    def jwt_validation(fn: Callable) -> Callable:
        """JWT validation decorator.

        Args:
            fn: The function to be decorated.

        Returns:
            The decorated function.
        """
        @wraps(fn)
        def wrapper(*args, **kwargs) -> Callable:
            """Wrapper for JSON Web Token (JWT) validation decorator.

            Args:
                args: positional arguments passed through from `param_pass`.
                kwargs: keyword arguments passed through from `param_pass`.

            Returns:
                Wrapper function.

            Raises:
                connexion.exceptions.Unauthorized: Raised if JWT could not be
                    successfully validated.
            """
            # Check if authentication is enabled
            if required:

                # Ensure that at least one validation method was configured
                if not len(validation_methods):
                    logger.error("No JWT validation methods configured.")
                    raise Unauthorized

                # Ensure that a valid validation checks argument was configured
                if validation_checks not in ['all', 'any']:
                    logger.error(
                        (
                            "Illegal argument '{validation_checks}"
                            "passed to configuration paramater"
                            "'validation_checks'. Allowed "
                            "values: 'any', 'all'"
                        )
                    )
                    raise Unauthorized

                # Parse JWT from HTTP header
                token = parse_jwt_from_header(
                    header_name=header_name,
                    token_prefix=token_prefix,
                )

                # Initialize claims
                claims = {}

                # Validate JWT via /userinfo endpoint
                if 'userinfo' in validation_methods:
                    if not (claims and validation_checks == 'any'):
                        logger.debug(
                            (
                                "Validating JWT via identity provider's "
                                "'/userinfo' endpoint..."
                            )
                        )
                        claims = validate_jwt_via_userinfo_endpoint(
                            token=token,
                            algorithms=algorithms,
                            claim_issuer=claim_issuer,
                        )
                        if not claims and validation_checks == 'all':
                            logger.error(
                                (
                                    "Insufficient number of JWT "
                                    "validation checks passed."
                                )
                            )
                            raise Unauthorized

                # Validate JWT via public key
                if 'public_key' in validation_methods:
                    if not (claims and validation_checks == 'any'):
                        logger.debug(
                            (
                                "Validating JWT via identity provider's"
                                "public key..."
                            )
                        )
                        claims = validate_jwt_via_public_key(
                            token=token,
                            algorithms=algorithms,
                            claim_key_id=claim_key_id,
                            claim_issuer=claim_issuer,
                            add_key_to_claims=add_key_to_claims,
                            audience=audience,
                            allow_expired=allow_expired,
                        )
                        if not claims and validation_checks == 'all':
                            logger.error(
                                (
                                    "Insufficient number of JWT"
                                    "validation checks passed."
                                )
                            )
                            raise Unauthorized

                # Check whether enough validation checks passed
                if not claims:
                    logger.error(
                        (
                            "No JWT validation checks passed."
                        )
                    )
                    raise Unauthorized

                # Ensure that specified identity claim is available
                if not validate_jwt_claims(
                    claim_identity,
                    claims=claims,
                ):
                    raise Unauthorized

                # Log result
                logger.debug(
                    "Access granted."
                )

                # Return wrapped function with token data
                return fn(
                    jwt=token,
                    claims=claims,
                    user_id=claims[claim_identity],
                    *args,
                    **kwargs
                )

            # Return wrapped function without token data
            else:
                return fn(*args, **kwargs)

        return wrapper

    return jwt_validation


def parse_jwt_from_header(
    header_name: str = 'Authorization',
    token_prefix: str = 'Bearer',
) -> str:
    """Parse JSON Web Token (JWT) from request header.

    Args:
        header_name: Name of the request header field at which the app is
            expecting the JWT. Cf. `--token-prefix`.
        token_prefix: Prefix that the app expects to precede the JWT, separated
            by whitespace. Together, prefix and JWT constitute the value of
            the request header field specified by `--header-name`.

    Returns:
        JSON Web Token.

    Raises:
        connexion.exceptions.Unauthorized: Raised if JWT cannot be extracted
            from request header.
    """
    # Ensure that authorization header is present
    auth_header = request.headers.get(header_name, None)
    if not auth_header:
        logger.error(
            "No HTTP header with name"
            "'{header_name}' found.".format(header_name=header_name,)
        )
        raise Unauthorized

    # Ensure that authorization header is formatted correctly
    try:
        (prefix, token) = auth_header.split()
    except ValueError as e:
        logger.error(
            (
                "Authentication header is malformed."
                "Original error message: "
                "{type}: {msg}"
            ).format(
                type=type(e).__name__,
                msg=e,
            )
        )
        raise Unauthorized

    if prefix != token_prefix:
        logger.error(
            (
                "Expected token prefix in authentication header is "
                "'{token_prefix}', but '{prefix}' was found."
            ).format(
                token_prefix=token_prefix,
                prefix=prefix,
            )
        )
        raise Unauthorized

    return token


def validate_jwt_via_userinfo_endpoint(
    token: str,
    algorithms: Iterable[str] = ['RS256'],
    claim_issuer: str = 'iss',
    service_document_field: str = 'userinfo_endpoint',
) -> Mapping:
    """Extract claims from a JSON Web Token (JWT) via an OpenID
    Connect-compliant identity provider's `/userinfo` endpoint.

    Args:
        token: JSON Web Token.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        claim_issuer: The JWT claim used to identify the issuer.
        service_document_field: Field in identity provider's service discovery
            endpoint response that points to the provider's `/userinfo`
            endpoint.

    Returns:
        Dictionary of JWT claims, or an empty dictionary if claims could not
            be successfully decoded.
    """
    # Decode JWT
    try:
        claims = jwt.decode(
            jwt=token,
            verify=False,
            algorithms=algorithms,
        )
    except Exception as e:
        logger.warning(
            (
                "JWT could not be decoded. Original error message: "
                "{type}: {msg}"
            ).format(
                type=type(e).__name__,
                msg=e,
            )
        )
        return {}

    # Verify existence of issuer claim
    if not validate_jwt_claims(
        claim_issuer,
        claims=claims,
    ):
        return {}

    # Get /userinfo endpoint URL
    url = get_entry_from_idp_service_discovery_endpoint(
        issuer=claims[claim_issuer],
        entry=service_document_field,
    )

    # Validate JWT via /userinfo endpoint
    if url:
        logger.debug(f"Issuer's '/userinfo' endpoint URL: {url}")
        try:
            validate_jwt_via_endpoint(
                url=url,
                token=token,
            )
        except Exception:
            return {}
    else:
        return {}

    # Log success and return claims
    logger.debug(
        f"Claims decoded: {claims}"
    )
    return claims


def validate_jwt_via_public_key(
    token: str,
    algorithms: Iterable[str] = ['RS256'],
    claim_key_id: str = 'kid',
    claim_issuer: str = 'iss',
    service_document_field: str = 'jwks_uri',
    add_key_to_claims: bool = True,
    audience: Optional[Iterable[str]] = None,
    allow_expired: bool = False,
) -> Mapping:
    """Extract claims from a JSON Web Token (JWT) with an OpenID
    Connect-compliant identity provider's public key.

    Args:
        token: JSON Web Token.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        claim_key_id: The JWT claim used to identify the JWK used when the JWT
            was issued.
        claim_issuer: The JWT claim used to identify the issuer.
        service_document_field: Field in identity provider's service discovery
            endpoint response that points to the provider's JSON Web Key set
            endpoint.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims when handling requests to
            `@jwt_validation`-decorated endpoints.
        audience: List of audiences that the app identifies itself with. If
            specified, JWTs that do not contain any of the specified audiences
            are rejected. If `None`, audience validation is disabled.
        allow_expired: Allow/disallow expired JWTs. If `False`, a `401`
            authorization error is raised in response to a request containing
            an expired JWT.

    Returns:
        Dictionary of JWT claims, or an empty dictionary if claims could not
            be successfully decoded.
    """
    # Extract JWT claims
    try:
        claims = jwt.decode(
            jwt=token,
            verify=False,
            algorithms=algorithms,
        )
    except Exception as e:
        logger.error(
            (
                "JWT could not be decoded. Original error message:"
                "{type}: {msg}"
            ).format(
                type=type(e).__name__,
                msg=e,
            )
        )
        return {}

    # Extract JWT Sheader claims
    try:
        header_claims = jwt.get_unverified_header(token)
    except Exception as e:
        logger.error(
            "Could not extract JWT header claims. Original error message: "
            f"{type(e).__name__}: {e}"
        )
        return {}

    # Get JWK set endpoint URL
    url = get_entry_from_idp_service_discovery_endpoint(
        issuer=claims[claim_issuer],
        entry=service_document_field,
    )

    # Obtain identity provider's public keys
    if url:
        logger.debug(f"Issuer's JWK set endpoint URL: {url}")
        public_keys = get_public_keys(
            url=url,
            claim_key_id=claim_key_id,
        )
    else:
        return {}

    # If currently used public key is specified, verify that it exists and
    # remove all other keys
    if claim_key_id in header_claims:
        if header_claims[claim_key_id] in public_keys:
            public_keys = {
                header_claims[claim_key_id]:
                    public_keys[header_claims[claim_key_id]]
            }
        else:
            logger.error(
                "JWT key ID not found among issuer's JWKs."
            )
            return {}
    else:
        logger.debug(
            "JWT key ID not specified. Trying all available JWKs..."
        )

    # Set validations
    validation_options = {}
    if audience is None:
        validation_options['verify_aud'] = False
    if allow_expired:
        validation_options['verify_exp'] = False

    # Try public keys one after the other
    pem = ''
    claims = {}
    for key in public_keys.values():

        # Get PEM representation of key
        pem = key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode('utf-8').encode('unicode_escape').decode('utf-8')

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
                "JWT could not be decoded with current JWK:\n"
                f"{pem}\n"
                f"Original error message: {type(e).__name__}: {e}"
            )
        # Key seems okay but token seems invalid
        except Exception as e:
            logger.error(
                "JWT could not be validated. Original error message: "
                f"{type(e).__name__}: {e}"
            )
            return {}

        # Do not try other keys if token was decoded
        if claims:
            break

    # Verify that token was decoded
    if not claims:
        logger.error(
            "JWT could not be validated with any of the issuer's JWKs."
        )
        return {}

    # Add public key to claims
    if add_key_to_claims:
        claims['public_key'] = pem

    # Log success and return claims
    logger.debug(
        f"Claims decoded: {claims}"
    )
    return claims


def validate_jwt_claims(
    *args: str,
    claims: Mapping,
) -> bool:
    """
    Validates the existence of one or more JWT claims.

    Args:
        *args: Claims whose existence in `claims` is to be verified.
        claims: Available claims, as keys of a mapping.

    Returns:
        `False` as soon as a missing claim is encountered and `True` otherwise.
    """
    # Check for existence of required claims
    for claim in args:
        if claim not in claims:
            logger.warning(
                (
                    "Required claim '{claim}' not found in JWT."
                ).format(
                    claim=claim,
                )
            )
            return False
    else:
        return True


def get_entry_from_idp_service_discovery_endpoint(
        issuer: str,
        entry: str,
) -> Optional[str]:
    """
    Retrieve specific entry from OpenID Connect-compliant identity provider's
    service discovery endpoint.

    Args:
        issuer: Base of JSON Web Token (JWT) issuer.
        entry: Entry to be retrieved.

    Returns:
        The desired entry, or `None` if either the service discovery endpoint
        could not be reached, or the entry is not available.
    """
    # Build endpoint URL
    base_url = issuer.rstrip("/")
    url = "{base_url}/.well-known/openid-configuration".format(
        base_url=base_url
    )

    # Send GET request to service discovery endpoint
    try:
        response = requests.get(url)
        response.raise_for_status()
    except Exception as e:
        logger.warning(
            (
                "Could not connect to endpoint '{url}'. Original error "
                "message: {type}: {msg}"
            ).format(
                url=url,
                type=type(e).__name__,
                msg=e,
            )
        )
        return None

    # Return entry or None
    if entry not in response.json():
        logger.warning(
            (
                "Required entry '{entry}' not found in "
                "identity provider's documentation accessed "
                "at endpoint '{url}'."
            ).format(
                entry=entry,
                url=url,
            )
        )
        return None
    else:
        return response.json()[entry]


def validate_jwt_via_endpoint(
    url: str,
    token: str,
    header_name: str = 'Authorization',
    prefix: str = 'Bearer'
) -> None:
    """
    Returns True if a JWT-headed request to a specified URL yields the
    specified status code.

    Args:
        url: URL of identity provider's `/userinfo` endpoint.
        token: JSON Web Token.
        header_name: Name of the request header field at which the app is
            expecting the JWT. Cf. `--token-prefix`.
        prefix: Prefix that the app expects to precede the JWT, separated by
            whitespace. Together, prefix and JWT constitute the value of
            the request header field specified by `--header-name`.

    Returns: `None` if validation succeeds.

    Raises:
        Exception: Raised if validation fails.
    """
    headers = {
        "{header_name}".format(
            header_name=header_name
        ): "{prefix} {token}".format(
            header_name=header_name,
            prefix=prefix,
            token=token,
        )
    }
    try:
        response = requests.get(
            url,
            headers=headers,
        )
        response.raise_for_status()
    except Exception as e:
        logger.warning(
            (
                "Could not connect to endpoint '{url}'. Original error "
                "message: {type}: {msg}"
            ).format(
                url=url,
                type=type(e).__name__,
                msg=e,
            )
        )
        raise

    return None


def get_public_keys(
    url: str,
    claim_key_id: str = 'kid',
) -> Mapping:
    """
    Obtain the identity provider's JSON Web Key (JWK) set.

    Args:
        url: Endpoint providing the identity providers JWK set.
        claim_key_id: The JWT claim encoding a JWK identifier.

    Returns:
        A dictionary of JWK identifiers (keys) and public keys (values); in
            case a connection to the identity provider could not be
            established, an empty dictionary is returned.
    """
    # Get JWK sets from identity provider
    try:
        response = requests.get(url)
        response.raise_for_status()
    except Exception as e:
        logger.warning(
            (
                "Could not connect to endpoint '{url}'. Original error "
                "message: {type}: {msg}"
            ).format(
                url=url,
                type=type(e).__name__,
                msg=e,
            )
        )
        return {}

    # Iterate over all JWK sets and store public keys in dictionary
    public_keys = {}
    for jwk in response.json()['keys']:
        token_algo = jwt.algorithms.RSAAlgorithm
        public_keys[jwk[claim_key_id]] = token_algo.from_jwk(
            json.dumps(jwk)
        )

    # Return dictionary of public keys
    return public_keys
