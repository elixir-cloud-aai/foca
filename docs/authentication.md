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
Unauthorized` if they carry no valid token, with a `WWW-Authenticate: Bearer`
challenge (with `error="invalid_token"` if the token is invalid, cf.
[Headers](configuration/exceptions.md#headers)). With `disable_auth: true` in a
specification's config, its security requirements are removed, e.g., for
local development.

## How tokens are validated

1. The token's issuer (`iss` claim) must be one of the configured
   [`issuers`](configuration/auth.md). Tokens of other issuers are rejected
   without contacting the issuer, so that clients cannot make the app send
   requests to arbitrary hosts.
2. The token must be an OAuth 2.0 access token as per
   [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068): its `typ` header must be
   one of the configured `token_types` (default: `at+jwt` and
   `application/at+jwt`, compared case-insensitively), unless
   `verify_token_type: false`. Other JWTs of the issuer, such as ID tokens,
   are rejected without contacting the issuer.
3. The issuer's [discovery document](https://openid.net/specs/openid-connect-discovery-1_0.html)
   and public keys (JWK set) are fetched and cached (`cache_ttl`). If a token
   was signed with an unknown key (e.g., after key rotation), the keys are
   fetched again, at most every 30 seconds. Failed requests to an identity
   provider are not repeated for every request either.
4. The token's signature is verified locally with the issuer's public key,
   using one of the configured `algorithms`. Its expiry (unless
   `allow_expired`) and its audience (unless `verify_audience: false`) are
   validated: the `aud` claim must contain one of the configured `audience`
   values.
5. The token must contain the claims `exp`, `iat` and `iss`, and the claim
   configured as `claim_identity` (default: `sub`).
6. If `clients` are configured, the token must have been obtained by one of
   them: its `client_id` claim ([RFC 9068](https://www.rfc-editor.org/rfc/rfc9068))
   or, if it has none, its `azp` claim (used by some identity providers
   instead) must be one of the configured client IDs.
7. If `validate_userinfo` is set, the token is additionally sent to the
   issuer's user info endpoint, which needs to report the token's subject.
   This detects revoked tokens, at the cost of a request per validation.
8. Claims listed in `userinfo_claims` that the token lacks are added from
   the issuer's user info endpoint, which needs to report the token's
   subject. Claims of the token are never replaced. Unless
   `validate_userinfo` is set, the user info is cached per token for
   `cache_ttl` seconds, but not beyond the token's expiry.

Steps 7 and 8 are skipped for [service tokens](#service-tokens), which have
no user info.

FOCA logs a warning on the first validation if no issuers are configured,
audience validation is disabled without restricting `clients`, token type
validation is disabled, or expired tokens are accepted.

!!! note "Several trusted issuers"
    Validated tokens of all trusted issuers are treated alike: their subjects
    and other claims are not scoped by issuer. If you trust several issuers,
    make sure that they cannot issue the same subject (`claim_identity`) to
    different users, or check the `iss` claim in your controllers. This also
    applies to [access control](access-control.md#identity-and-group-claims).

!!! warning "Audience required"
    With trusted `issuers`, an `audience` is required; otherwise, FOCA
    refuses to start. Register the app with the identity provider and set
    its client ID (or the audience the provider adds to tokens for the app)
    as `audience`. Only if the provider does not include the app in the
    `aud` claim of its tokens, set `verify_audience: false`: then any token
    issued by a trusted issuer is accepted, including tokens issued for
    other apps, unless you restrict the `clients` (below).

!!! tip "Accepting tokens of specific clients"
    Some apps must accept tokens by the client that obtained them rather
    than by audience. For example, task execution services behind the
    [proTES](https://github.com/elixir-cloud-aai/proTES) gateway of ELIXIR
    On-Cloud should accept only tokens obtained by proTES, whose audience is
    proTES's client ID. List the accepted client IDs in `clients`, and
    disable audience validation if the audience is not the app itself:

    ```yaml
    security:
      auth:
        verify_audience: false
        clients:
          - protes-client-id
    ```

    Tokens whose `client_id` claim (RFC 9068; LS Login) or, if they have
    none, `azp` claim (OpenID Connect; used by some identity providers
    instead) is not listed, and tokens with neither claim, are rejected with
    `401`. With `clients`, FOCA does not warn about disabled audience
    validation. Client IDs are matched for all trusted `issuers`: if you
    trust several, make sure that the listed IDs cannot be claimed by clients
    of another issuer (cf. the note on several trusted issuers above).

!!! warning "Access tokens only"
    Identity providers issue other JWTs than access tokens, e.g., ID tokens,
    which contain user information for the client and are not meant to be
    sent to resource servers. FOCA tells access tokens apart by their `typ`
    header, as required by RFC 9068; LS Login, for example, sets it to
    `at+jwt`. Only if your identity provider does not set the `typ` header of
    its access tokens, set `verify_token_type: false`: then any JWT issued by
    a trusted issuer is accepted, including ID tokens. If it sets another
    value, list it in `token_types` instead; note that a generic type such as
    `JWT` does not tell access tokens apart from ID tokens either, so that
    the `audience` is then the only safeguard.

## Service tokens

Services call each other with tokens of their own, obtained via the OAuth 2.0
client credentials grant: e.g., a workflow engine calls a task execution
service. Such tokens represent the calling client, not a user, and the
identity provider has no user info for them. List the clients that call the
app with such tokens as `service_clients`:

```yaml
security:
  auth:
    service_clients:
      - client_id: conquest-client-id
      - client_id: protes-client-id
        subject: protes-service-subject
        on_behalf_of: true
    on_behalf_of_header: X-On-Behalf-Of
```

A token is a service token of a service client if it was issued to the
client (its `client_id` claim or, if it has none, its `azp` claim is the
client's `client_id`) and its subject (the `claim_identity` claim, default:
`sub`) is the client's `subject` (default: its client ID). Other tokens
issued to a service client, e.g., tokens it obtained for users, are treated
as user tokens. For service tokens, FOCA requests no user info (neither for
`userinfo_claims` nor for `validate_userinfo`), and `token_info` has the
client ID of the service client as `service_client` (`None` for other
tokens). With [access control](access-control.md#service-clients), policies
are enforced on them with the subject `client:<client ID>`.

!!! warning "Check the claims of client-credentials tokens"
    Identity providers differ in which subject they put into
    client-credentials tokens: often the client ID, sometimes another
    identifier of the client. What LS Login puts into its client-credentials
    tokens has not been verified for FOCA. Inspect a client-credentials token
    of each service client and set its `subject` accordingly; otherwise, its
    tokens are treated as user tokens: access control then uses the
    client's subject as a user, and with `userinfo_claims` or
    `validate_userinfo`, user info is requested for its tokens, which
    usually fails.

If `clients` are restricted, service clients must be among them; FOCA refuses
to start otherwise.

Service clients with `on_behalf_of: true` (e.g., a gateway forwarding user
requests) may name the user they act for, in the `on_behalf_of_header` or in
a request field that the app reads. Call
`foca.security.access_control.get_on_behalf_of_user()` (optionally with the
value of the field) to get that user, e.g., to make them the owner of a
resource the client creates. Requests of other callers naming a user are
rejected with `403 Forbidden`, and empty or blank names with `400 Bad
Request`. FOCA does not check whether the named user exists: service clients
allowed to act for users are trusted to name the user whose request they
handle.

## Using token claims

Connexion passes the result of the validation to controllers that accept
arguments `user` (the subject) or `token_info`:

```python
def get_pets(user: str, token_info: dict) -> list[dict]:
    claims = token_info["claims"]
    ...
```

`token_info` contains the token (`jwt`), the verified `claims` (including
any claims added from user info), the subject (`user_id`), the token's
`scope` and, for [service tokens](#service-tokens), the client ID of the
service client (`service_client`).

!!! tip "Claims from user info"
    Some identity providers include claims in their user info only, not in
    access tokens. For example, [LS Login](https://lifescience-ri.eu/ls-login/)
    provides group memberships (`eduperson_entitlement`) via user info. To
    use such claims, e.g., for [access control](access-control.md#identity-and-group-claims),
    list them in `userinfo_claims`:

    ```yaml
    security:
      auth:
        userinfo_claims:
          - eduperson_entitlement
    ```

To additionally restrict what authenticated subjects may do, use
[access control](access-control.md).
