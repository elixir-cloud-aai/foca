# Migrating to 0.16.0

FOCA 0.16.0 tightens token validation for the ELIXIR On-Cloud authentication
policy. Most apps need no changes; apps whose identity provider does not issue
RFC 9068 access tokens should check the breaking changes below. Upgrading from
0.14 or earlier? Start with [Migrating to 0.15.0](migration-0.15.md).

## Breaking changes

### Access tokens only

Bearer tokens must be OAuth 2.0 access tokens as per
[RFC 9068](https://www.rfc-editor.org/rfc/rfc9068): their `typ` header must be
`at+jwt` or `application/at+jwt` (compared case-insensitively). Other JWTs of
trusted issuers, such as ID tokens, and tokens without `typ` header are
rejected with `401 Unauthorized` (`WWW-Authenticate: Bearer
error="invalid_token"`), without contacting the issuer. LS Login issues such
access tokens, so apps using it need no changes.

If your identity provider sets another `typ` header on its access tokens, list
it in [`security.auth.token_types`](configuration/auth.md); if it sets none,
disable the check with `security.auth.verify_token_type: false` (FOCA then
logs a warning on the first validation, as for `verify_audience: false`):

```yaml
security:
  auth:
    verify_token_type: false
```

See [Authentication](authentication.md#how-tokens-are-validated) for details.

## New options

- [`security.auth.token_types`](configuration/auth.md) and
  `security.auth.verify_token_type`: accepted values of the `typ` header of
  bearer tokens (default: `at+jwt` and `application/at+jwt`), and whether to
  check them (default: `true`; see above).
