# Migrating to 0.16.0

FOCA 0.16.0 tightens token validation for the ELIXIR On-Cloud authentication
policy and requires Python 3.11 or later. Apps using LS Login need no other
changes; apps whose identity provider does not issue RFC 9068 access tokens
should check the breaking changes below.
Upgrading from 0.14 or earlier? Start with [Migrating to
0.15.0](migration-0.15.md).

## Breaking changes

### Python 3.11 or later

Python 3.10 is no longer supported; FOCA requires Python 3.11 or later.

### Access tokens only

Bearer tokens must be OAuth 2.0 access tokens as per
[RFC 9068](https://www.rfc-editor.org/rfc/rfc9068): their `typ` header must be
`at+jwt` or `application/at+jwt` (compared case-insensitively). Other JWTs of
trusted issuers, such as ID tokens, and tokens without `typ` header are
rejected with `401 Unauthorized` (`WWW-Authenticate: Bearer
error="invalid_token"`), without contacting the issuer. LS Login issues such
access tokens, so apps using it need no changes.

If your identity provider sets another `typ` header on its access tokens, list
it in [`security.auth.token_types`](configuration/auth.md) (a generic type such
as `JWT` does not tell access tokens apart from ID tokens, though); if it sets
none, disable the check with `security.auth.verify_token_type: false` (FOCA
then logs a warning on the first validation, as for `verify_audience: false`):

```yaml
security:
  auth:
    verify_token_type: false
```

See [Authentication](authentication.md#how-tokens-are-validated) for details.

### Stricter subject and claim options

Service clients are namespaced with `client:` in access control rules (see
[new options](#new-options)). A `security.access_control.subject_prefix`
overlapping with it (e.g., `client:` or `c`) is rejected on startup, as for
`group:`; choose another prefix, and migrate the rules accordingly.

Likewise, roles of `security.access_control.group_mapping` must not start with
`subject_prefix`, `group:` or `client:`, as mapped groups would otherwise get
the rights of that subject, and `security.auth.userinfo_claims` must not
contain `client_id` or `azp`, as user info must not change which client a
token was obtained by.

## New options

- [`security.auth.token_types`](configuration/auth.md) and
  `security.auth.verify_token_type`: accepted values of the `typ` header of
  bearer tokens (default: `at+jwt` and `application/at+jwt`), and whether to
  check them (default: `true`; see above).
- [`security.auth.clients`](configuration/auth.md): IDs of the clients whose
  tokens are accepted (`client_id` claim, or `azp` claim if there is none),
  e.g., a gateway in front
  of the app; usable in addition to `audience` or instead of it, with
  `verify_audience: false`
  ([details](authentication.md#how-tokens-are-validated)). By default,
  clients are not restricted.
- [`security.auth.service_clients`](configuration/auth.md) and
  `security.auth.on_behalf_of_header`: clients calling the app with
  client-credentials tokens of their own (service tokens), for which no user
  info is requested, and the header in which allowed service clients name
  the user they act for ([details](authentication.md#service-tokens)). Each
  service client is tied to a trusted issuer (`issuer`), which is required
  if several issuers are trusted.
- [`security.access_control.client_mapping`](configuration/access-control.md):
  roles of service clients, which access control checks as
  `client:<client ID>` ([details](access-control.md#service-clients)).
- [`security.access_control.resource_roles`](configuration/access-control.md):
  roles on resources (default: `owner` and `maintainer`) that the new
  resource ownership helpers grant and revoke, e.g., to make the creator of a
  resource its owner and to filter listings to the caller's resources
  ([details](access-control.md#resource-ownership)).
