"""Local identity provider, for tests of authentication.

Serves OIDC discovery, a JSON Web Key (JWK) set and user info on a local port,
and signs access tokens, so that tests run FOCA's token validation as in
production, including audience, token type, groups and service clients, rather
than mocking it. Doesn't depend on a test client or pytest.

```python
from foca.testing.issuer import issuer

with issuer() as idp:
    token = idp.token(sub="alice", aud="my-app", groups=["admins"])
```
"""

import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from foca.security import auth

JWKS_PATH = "/jwks"
USERINFO_PATH = "/userinfo"
_KEY_SIZE = 2048
_KEY_ID = "foca-testing"


class Issuer:
    """Identity provider serving on a local port.

    Use :py:func:`issuer` to start and stop one.

    Attributes:
        url: Identifier of the issuer, for `security.auth.issuers`; also the
            value of the `iss` claim of its tokens.
    """

    def __init__(self) -> None:
        """Generate the signing key, and start listening on a free port."""
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
        # Status and claims, set and read together
        self._userinfo: tuple[int, dict[str, Any]] = (200, {})
        provider = self

        class _Handler(BaseHTTPRequestHandler):
            """Handler of the requests to the identity provider."""

            def do_GET(self) -> None:
                """Respond to a request, for the paths of the issuer."""
                status, body = provider._respond(
                    self.path, self.headers.get("Authorization")
                )
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                """Don't write requests to the standard error stream."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def _start(self) -> None:
        """Start serving requests in the background."""
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _stop(self) -> None:
        """Stop serving requests, and release the port."""
        self._server.shutdown()
        self._server.server_close()
        # Another issuer may get the port, with other keys
        auth.clear_caches()

    def _respond(
        self, path: str, authorization: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Get the response to a request.

        Args:
            path: Path of the request.
            authorization: Value of the `Authorization` header, if any.

        Returns:
            Status code and JSON body.
        """
        if path == f"/{auth.OIDC_CONFIG_PATH}":
            return 200, {
                "issuer": self.url,
                "jwks_uri": f"{self.url}{JWKS_PATH}",
                "userinfo_endpoint": f"{self.url}{USERINFO_PATH}",
            }
        if path == JWKS_PATH:
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
                self._key.public_key(), as_dict=True
            )
            return 200, {"keys": [{**jwk, "kid": _KEY_ID, "use": "sig"}]}
        if path == USERINFO_PATH:
            return self._respond_userinfo(authorization)
        return 404, {"error": "not_found"}

    def _respond_userinfo(
        self, authorization: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Get the response to a request for user info.

        Args:
            authorization: Value of the `Authorization` header, if any.

        Returns:
            Status code and JSON body: the subject of the token and what was
            set with :py:meth:`set_userinfo`.
        """
        status, userinfo = self._userinfo
        if status != 200:
            return status, {"error": "userinfo_failed"}
        scheme, _, token = (authorization or "").partition(" ")
        try:
            if scheme.lower() != "bearer":
                raise jwt.InvalidTokenError
            claims = jwt.decode(
                token,
                self._key.public_key(),
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError:
            return 401, {"error": "invalid_token"}
        return 200, {"sub": claims.get("sub"), **userinfo}

    def set_userinfo(self, status: int = 200, **claims: Any) -> None:
        """Set the responses to requests for user info.

        Args:
            status: Status code; for others than 200, an error response.
            **claims: Claims in addition to the subject of the token, replacing
                those of earlier calls.
        """
        self._userinfo = (status, claims)
        auth.clear_caches()

    def token(
        self,
        sub: str = "alice",
        aud: str | list[str] = "foca",
        typ: str = "at+jwt",
        groups: list[str] | None = None,
        client_id: str | None = None,
        expires_in: int = 3600,
        **claims: Any,
    ) -> str:
        """Sign an access token.

        Args:
            sub: Subject.
            aud: Audience.
            typ: Token type, for the `typ` header.
            groups: Groups, for the `groups` claim, if any.
            client_id: Client that the token was issued to, for the `client_id`
                claim, if any.
            expires_in: Seconds until it expires; negative for expired tokens.
            **claims: Further claims, replacing those above (e.g., `iss`).

        Returns:
            The signed token.
        """
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.url,
            "sub": sub,
            "aud": aud,
            "iat": now,
            "exp": now + expires_in,
            "jti": secrets.token_hex(8),
        }
        if groups is not None:
            payload["groups"] = groups
        if client_id is not None:
            payload["client_id"] = client_id
        return jwt.encode(
            {**payload, **claims},
            self._key,
            algorithm="RS256",
            headers={"kid": _KEY_ID, "typ": typ},
        )


@contextmanager
def issuer() -> Iterator[Issuer]:
    """Run an identity provider on a local port.

    Yields:
        The identity provider. Each has its own key and port.
    """
    instance = Issuer()
    instance._start()
    try:
        yield instance
    finally:
        instance._stop()
