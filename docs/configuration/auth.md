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
(not recommended), set `verify_audience: false`. See [Authentication](../authentication.md) for how
tokens are validated and how to require them for operations.

::: foca.models.config.AuthConfig
    options:
      members: false
