# Authentication

FOCA validates bearer tokens ([JSON Web Tokens](https://jwt.io/), JWTs) issued
by trusted [OpenID Connect](https://openid.net/connect/) (OIDC) identity
providers, such as [LS Login](https://lifescience-ri.eu/ls-login/).

## Requiring tokens

Which operations require a token is defined by the security requirements of
your API specification, as usual with
[Connexion](https://connexion.readthedocs.io/en/2.14.2/security.html). Add a
bearer security scheme and require it for all operations (or individual
ones):

```yaml title="api/openapi.yaml"
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
security:
  - bearerAuth: []
```

Then let FOCA validate the tokens, by adding its validation function to the
security schemes, and configure the trusted issuers:

```yaml title="config.yaml"
api:
  specs:
    - path: api/openapi.yaml
      add_security_fields:
        x-bearerInfoFunc: foca.security.auth.validate_token
security:
  auth:
    issuers:
      - https://login.elixir-czech.org/oidc/
    audience:
      - my-app
```

Requests to operations requiring a token are rejected with `401
Unauthorized` if they carry no valid token. With `disable_auth: true` in a
specification's config, its security requirements are removed, e.g., for
local development.

## How tokens are validated

1. The token's issuer (`iss` claim) must be one of the configured
   [`issuers`](configuration/auth.md). Tokens of other issuers are rejected
   without contacting the issuer, so that clients cannot make the app send
   requests to arbitrary hosts.
2. The issuer's [discovery document](https://openid.net/specs/openid-connect-discovery-1_0.html)
   and public keys (JWK set) are fetched and cached (`cache_ttl`). If a token
   was signed with an unknown key (e.g., after key rotation), the keys are
   fetched again, at most every 30 seconds. Failed requests to an identity
   provider are not repeated for every request either.
3. The token's signature is verified locally with the issuer's public key,
   using one of the configured `algorithms`. Its expiry (unless
   `allow_expired`) and its audience (unless `verify_audience: false`) are
   validated: the `aud` claim must contain one of the configured `audience`
   values.
4. The token must contain the claims `exp`, `iat` and `iss`, and the claim
   configured as `claim_identity` (default: `sub`).
5. If `validate_userinfo` is set, the token is additionally sent to the
   issuer's user info endpoint, which needs to report the token's subject.
   This detects revoked tokens, at the cost of a request per validation.

FOCA logs a warning on the first validation if no issuers are configured,
audience validation is disabled, or expired tokens are accepted.

!!! warning "Audience required"
    With trusted `issuers`, an `audience` is required; otherwise, FOCA
    refuses to start. Register the app with the identity provider and set
    its client ID (or the audience the provider adds to tokens for the app)
    as `audience`. Only if the provider does not include the app in the
    `aud` claim of its tokens, set `verify_audience: false`: then any token
    issued by a trusted issuer is accepted, including tokens issued for
    other apps.

## Using token claims

Connexion passes the result of the validation to controllers that accept
arguments `user` (the subject) or `token_info`:

```python
def get_pets(user: str, token_info: dict) -> list[dict]:
    claims = token_info["claims"]
    ...
```

`token_info` contains the token (`jwt`), the verified `claims`, the subject
(`user_id`) and the token's `scope`.

To additionally restrict what authenticated subjects may do, use
[access control](access-control.md).
