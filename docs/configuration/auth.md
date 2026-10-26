# `security.auth`

Validation of bearer tokens ([JSON Web Tokens](https://jwt.io/), JWTs) issued
by [OpenID Connect](https://openid.net/connect/) identity providers.

```yaml
security:
  auth:
    issuers:
      - https://login.example.org/oidc/
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
    service_clients:
      - client_id: conquest-client-id
        issuer: null
        subject: null
        on_behalf_of: false
    on_behalf_of_header: null
    timeout: 10
    cache_ttl: 300
```

Only tokens issued by one of the trusted `issuers` are accepted; all others
are rejected without contacting their issuers. With trusted `issuers`,
`audience` is required, so that tokens issued for other apps by the same
identity provider are rejected; to accept tokens for any audience instead
(not recommended, unless `clients` are restricted), set
`verify_audience: false`. Only OAuth 2.0 access tokens
as per [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068) (`typ` header
`at+jwt` or `application/at+jwt`) are accepted, so that other JWTs of the same
identity provider, such as ID tokens, cannot be used as access tokens; to
accept tokens of other types, list them in `token_types`, or set
`verify_token_type: false` if the identity provider does not set the `typ`
header of its access tokens. To accept only tokens obtained by specific
clients (`client_id` claim, or `azp` claim if there is none), e.g., by a
gateway in front of the app, list their IDs in `clients`, in addition to
`audience` or instead of it (with `verify_audience: false`). Clients calling
the app with client-credentials tokens of their own are listed as
`service_clients`; no user info is requested for their tokens. Each
service client is tied to a trusted issuer, which is required if several
`issuers` are trusted (cf. [Service
tokens](../authentication.md#service-tokens)). See
[Authentication](../authentication.md) for how tokens are validated and how to
require them for operations.

Issuers are listed as URLs, or with settings of their own, e.g., to choose
the namespace of their subjects (cf. [several trusted
issuers](../authentication.md#several-trusted-issuers)). Identity providers
differ, so issuers may also have an `audience`, a group claim
(`claim_groups`, cf. [`security.access_control`](access-control.md)) and
`userinfo_claims` of their own, which replace the global settings for their
tokens; unset ones fall back to the global settings:

```yaml
security:
  auth:
    audience:
      - my-app
    issuers:
      - issuer: https://login.example.org/oidc/
        namespace: ""
      - issuer: https://login.example.com/oidc/
        namespace: example-com
        audience:
          - my-app-client-id
        claim_groups: groups
        userinfo_claims: []
```

With an `audience` for each issuer, the global `audience` is not required.

::: foca.models.config.AuthConfig
    options:
      members: false

::: foca.models.config.IssuerConfig
    options:
      members: false

::: foca.models.config.ServiceClientConfig
    options:
      members: false
