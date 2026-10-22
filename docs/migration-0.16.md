# Migrating to 0.16.0

FOCA 0.16.0 tightens token validation for the ELIXIR On-Cloud authentication
policy, accepts tokens of service clients, adds resource ownership helpers and
requires Python 3.11 or later. Apps using LS Login need no other changes;
apps whose identity provider does not issue RFC 9068 access tokens, and apps
setting `swagger_ui_path`, should check the breaking changes below.
Upgrading from 0.14 or earlier? Start with [Migrating to
0.15.0](migration-0.15.md).

## Breaking changes

### Python 3.11 or later

Python 3.10 is no longer supported; FOCA requires Python 3.11 or later. Images
are published for Python 3.11 to 3.14; there are no `py3.10` images anymore.

`typing_extensions` is no longer a dependency of FOCA. If your app imports it,
declare it as a dependency of your app.

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

Roles of `group_mapping` (and of the new `client_mapping`) must not be roles of
`security.access_control.resource_roles` (by default, `owner` and
`maintainer`) either: a mapped role is a subject of its own, which matches the
rules of that role on every resource, so all members of a group mapped to
`owner` would own all resources. Such configurations are rejected on startup;
map groups to global roles such as `admin` instead, and grant resource roles
to groups on single resources (see [granting roles to
groups](access-control.md#granting-roles-to-groups)).

### Swagger UI path

The `swagger_ui_path` option of
[`api.specs[].connexion`](configuration/api.md) now takes effect. FOCA passed
it to Connexion under the wrong key, so the Swagger UI stayed at `/ui`
regardless of its value. If your configuration sets it, the Swagger UI is now
served at that path (relative to the base path); remove the option to keep
`/ui`. Empty values and values consisting of slashes only (e.g., `/`) are
rejected on startup, as Connexion would then serve the UI's static files for
all paths under the base path.

## Behaviour changes

- Error responses keep the headers of werkzeug HTTP exceptions and of
  Connexion's `ProblemException` (e.g., `Allow`, `WWW-Authenticate` and
  `Retry-After`), except headers describing the body, unless the exception
  is mapped to another status. `401 Unauthorized` responses of FOCA's token
  validation and access control carry a Bearer challenge as per RFC 6750
  (`WWW-Authenticate: Bearer error="invalid_token"` for invalid tokens), and
  other security errors of Connexion get `WWW-Authenticate: Bearer`
  ([details](configuration/exceptions.md#headers)). Clients that relied on
  responses without these headers may need adjustments.
- On Python 3.11 and 3.12, default problem titles for `413`, `414`, `416` and
  `422` are the RFC 9110 reason phrases (`Content Too Large`, `URI Too Long`,
  `Range Not Satisfiable` and `Unprocessable Content`), as on Python 3.13 and
  later. Adjust clients or tests that compare titles.
- Tokens of issuers whose JWK set is malformed (not a JSON object, or with
  keys of the wrong type) are rejected with `401 Unauthorized` instead of
  `500 Internal Server Error`, and fetching the JWK set again is throttled,
  as for unreachable JWK sets.
- The Swagger UI works for APIs served at `/`; with Connexion 2, it returned
  `500 Internal Server Error`.
- The Swagger UI is not served without the specification, which it needs (it
  returned `500 Internal Server Error`): with `serve_spec: false` in
  [`api.specs[].connexion`](configuration/api.md), the UI is disabled too,
  and `swagger_ui: true` with `serve_spec: false` is rejected on startup.
  `security.access_control.api_swagger_ui: true` also serves the
  specification of the permissions API, unless `api_serve_spec` is set to
  `false`, which is rejected.
- The petstore example and the
  [deployment docs](deployment.md#running-in-production) start Gunicorn with `--no-control-socket`. With Gunicorn 25.1 or later on
  a read-only root file system (e.g., in Kubernetes), add it to your app's
  command too, as Gunicorn otherwise logs an error for its control server.
- Package metadata, Docker image labels and the default permissions API
  specification name "ELIXIR On-Cloud" instead of "ELIXIR Cloud & AAI".

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
  resource its owner and to filter listings to the caller's resources, or to
  those of the user that a service client acts for
  ([details](access-control.md#resource-ownership)).

## New features

- Resource ownership helpers in `foca.security.access_control`
  (`add_owner()`, `grant_role()`, `revoke_roles()`, `list_roles()`,
  `list_subject_roles()`, `resource_filter()` and `is_admin()`), with
  last-owner protection (`LastOwner`, `409 Conflict`)
  ([details](access-control.md#resource-ownership)), and ready-made
  controllers for endpoints managing roles on resources in
  `foca.security.access_control.ownership_server`, which require access
  control policies to be enforced on their operations (apps routing
  unenforced operations to them do not start)
  ([details](access-control.md#endpoints-for-managing-roles)). Apps managing
  owners with the resource role helpers of 0.15 can switch to them.
- `foca.utils.urls` for checking URLs (`http`, `https` and `mailto`), with
  the allowed schemes chosen per field by the app, and for normalizing HTTP(S)
  URLs, e.g., to compare base URLs
  ([details](utilities.md#checking-and-normalizing-urls)).
- `foca-common`, a Helm library chart for deploying FOCA apps, versioned and
  released separately (`chart-v*` tags,
  `oci://ghcr.io/elixir-cloud-aai/charts`), optionally with a Celery worker and RabbitMQ broker
  ([details](helm-chart.md)).

## Dependencies

- Python 3.11 or later (see [above](#python-311-or-later)).
- New: `idna>=3.15,<4` (for `foca.utils.urls`; 3.15 fixes CVE-2026-45409).
- Raised lower bound: `PyJWT>=2.15` (fixes CVE-2026-48523, which let tokens
  signed with an algorithm not in `security.auth.algorithms` pass, and
  CVE-2026-32597).
- Removed: `typing_extensions`.
- Building FOCA from source requires `setuptools-scm>=9`.
