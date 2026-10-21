# `security.auth`

Validation of bearer tokens ([JSON Web Tokens](https://jwt.io/), JWTs) issued
by [OpenID Connect](https://openid.net/connect/) identity providers.

```yaml
security:
  auth:
    issuers:
      - https://login.elixir-czech.org/oidc/
    audience:
      - my-app
    verify_audience: true
    token_types:
      - at+jwt
      - application/at+jwt
    verify_token_type: true
    clients: null
    claim_identity: sub
    algorithms:
      - RS256
    allow_expired: false
    validate_userinfo: false
    userinfo_claims: []
    timeout: 10
    cache_ttl: 300
```

Only tokens issued by one of the trusted `issuers` are accepted; all others
are rejected without contacting their issuers. With trusted `issuers`,
`audience` is required, so that tokens issued for other apps by the same
identity provider are rejected; to accept tokens for any audience instead
(not recommended), set `verify_audience: false`. Only OAuth 2.0 access tokens
as per [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068) (`typ` header
`at+jwt` or `application/at+jwt`) are accepted, so that other JWTs of the same
identity provider, such as ID tokens, cannot be used as access tokens; to
accept tokens of other types, list them in `token_types`, or set
`verify_token_type: false` if the identity provider does not set the `typ`
header of its access tokens. To accept only tokens obtained by specific
clients (`client_id` or `azp` claim), e.g., by a gateway in front of the app,
list their IDs in `clients`, in addition to `audience` or instead of it (with
`verify_audience: false`). See [Authentication](../authentication.md) for how
tokens are validated and how to require them for operations.

::: foca.models.config.AuthConfig
    options:
      members: false
