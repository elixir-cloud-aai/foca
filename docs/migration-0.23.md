# Migrating to 0.23.0

FOCA 0.23.0 namespaces subjects by issuer also with a single trusted issuer,
lets patches of API specifications remove keys, the permissions API reports
invalid fields, and the plugin store limits the size of each namespace. Apps
and deployments need to change only if:

- they use access control or store subjects (e.g., owners), as [subjects are
  always namespaced by issuer](#subjects-are-always-namespaced-by-issuer);
- their [patches set values to `null`](#null-in-patches-removes-keys);
- their tests or clients expect the [permissions API's plain `400`
  responses](#the-permissions-api-reports-invalid-fields);
- their plugins keep more than 16 MiB in the [plugin
  store](#the-plugin-store-limits-namespaces), or they implement a store
  backend of their own;
- they are WES gateways with plugins, as [plugins get WES runs in a defined
  shape](#plugins-get-wes-runs-in-a-defined-shape);
- they are gateways that route only some of their backends, or their clients
  choose backends with `gateway.backends`, as [choices of unknown backends
  are refused](#choices-of-unknown-backends-are-refused);
- their clients send values with a trailing newline to [patterns ending in
  `$`](#patterns-match-at-the-end-only);
- they set no `server.max_content_length` and accept request bodies larger
  than 10 MiB, set it to `null` (as the configuration template did), or
  accept URL-encoded bodies larger than 500 kB (whatever
  `server.max_content_length` is), as [request bodies are limited by
  default](#request-bodies-are-limited-by-default);
- their tests or clients expect `413` for anonymous requests with too large
  bodies, or replace FOCA's token validation in ways it doesn't see, as
  [bearer tokens are checked before bodies are
  read](#bearer-tokens-are-checked-before-bodies-are-read).

WES gateways may also drop workarounds for [backend lists, which now know
WES](#backend-lists-know-wes), and gateways their own token clients, as
[FOCA resolves the credentials of
backends](#foca-resolves-the-credentials-of-backends).

## Subjects are always namespaced by issuer

Subjects are namespaced by their issuer (`<namespace>|<subject>`, by default
with the issuer as namespace, e.g., `https://login.example.org/oidc/|alice`)
also if only one issuer is trusted, so that trusting another issuer later
changes nothing about what is stored (see [several trusted
issuers](authentication.md#several-trusted-issuers)). Before, subjects of a
single trusted issuer were bare (`alice`). This changes what FOCA and apps get
and store for users:

- `get_current_subject()`, `get_on_behalf_of_user()` and
  `namespace_subject()` return namespaced subjects, and so do the identity of
  the `Principal` of [gateway plugins](plugins.md) and the users of audit events;
- rules and roles that FOCA stores (e.g., owners stored by `add_owner()`, roles
  granted via the [endpoints for managing
  roles](access-control.md#endpoints-for-managing-roles)) name users as
  `user:<namespace>|<subject>`, and the `sub` of owners in documents (cf.
  `set_owner_field()`) is namespaced as well;
- users in the paths of the endpoints for managing roles are namespaced, so
  they contain slashes with issuers as namespaces: route them with
  `format: path`, and match them with `*` in rules (e.g., `/pets/:id/permissions/users/*`
  instead of `.../users/:userId`; see the example there), or set namespaces
  without slashes.

Stored bare users then no longer apply, e.g., owners and admins granted via
the permissions API lose their roles, and the app refuses to start while
configured rules (`security.access_control.policies`) name bare users.
Choose one of these before you upgrade:

1. **Keep the subjects bare:** set `namespace: ""` for the trusted issuer, so
   that its subjects stay bare as before, also if another issuer is trusted
   later (whose subjects are then namespaced). Nothing else changes, and no
   data needs to be moved. This is an explicit opt-out for one issuer, for
   existing deployments; it will be reconsidered for FOCA 1.0, so new
   deployments shouldn't use it:

    ```yaml
    security:
      auth:
        issuers:
          - issuer: https://login.example.org/oidc/
            namespace: ""
    ```

2. **Namespace the stored subjects:** optionally set a shorter `namespace` for
   the issuer (e.g., `ls`; it must not change later, and the issuer's subjects
   must not start with the namespace of any trusted issuer and `|`, e.g., not
   `auth0` for subjects like `auth0|123`), then, with the configuration of the
   new version:

    1. Namespace the users of configured rules in
       `security.access_control.policies`, e.g., `[g, "user:alice", admin]`
       to `[g, "user:https://login.example.org/oidc/|alice", admin]`. FOCA
       refuses to start if a configured rule names a user without the
       namespace of a trusted issuer (`ValueError`, naming the rules in
       `policies`), so that admins aren't lost unnoticed (with a custom
       `model` that isn't a copy of one that FOCA ships, it only warns), and
       warns on startup of stored rules naming such users.
    2. Rewrite the stored rules with the [command-line
       tool](cli.md#namespace-subjects), first with `--dry-run` to count them,
       before the new version serves requests (e.g., in a Helm `pre-upgrade`
       hook, or right after the upgrade; until then, the stored users lose
       their roles, and roles on resources that are changed meanwhile keep
       their change, while the bare user's earlier role is kept bare, see
       the command's output):

        ```shell
        foca --app my_app.app:foca namespace-subjects --dry-run https://login.example.org/oidc/
        foca --app my_app.app:foca namespace-subjects https://login.example.org/oidc/
        ```

    3. Rewrite the subjects that your app stores itself, e.g., owners in
       documents (`set_owner_field()`, field `owner` by default) or users
       stored from `get_current_subject()`. For owner fields, e.g., in
       `mongosh`, for the collection `pets` and the issuer as namespace; the
       owner field stores the issuer, so only owners of that issuer are
       rewritten:

        ```javascript
        const iss = "https://login.example.org/oidc/";
        db.pets.updateMany(
          {
            "owner.iss": iss,
            "owner.type": "user",
            "owner.sub": {$type: "string", $not: {$regex: "^https://login\\.example\\.org/oidc/\\|"}},
          },
          [{$set: {"owner.sub": {$concat: [iss, "|", "$owner.sub"]}}}]
        );
        ```

    4. Entries that [gateway plugins](plugins.md) stored per user, keyed by
       the identity of the `Principal`, are no longer found under the
       namespaced identity; they expire with their time to live.

Clients that name users, e.g., service clients naming the user they act for
(`on_behalf_of_header`), keep naming them bare: FOCA namespaces them with the
issuer of the service client. Clients of the endpoints for managing roles name
users namespaced. An issuer that is the namespace of its subjects (no
`namespace` set) must not contain `|`, commas or line breaks, and must have at
most 256 characters, also if it is the only one: set a `namespace` otherwise;
FOCA refuses to start.

## `null` in patches removes keys

[Patches](configuration/api.md#patching-specifications) (`patches` of a
specification) are now applied as a JSON Merge Patch
([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396)): `null` removes a key, at
any depth, instead of setting its value to `null`. This lets patches, e.g.,
turn an API key scheme into an HTTP bearer scheme (`name: null`, `in: null`)
or replace a reference by a path item of their own (`$ref: null`).

Patches that set a value to `null` (e.g., `default: null` or `example: null`
in a schema) now remove it instead. Set such values in a file of `path`
instead: files of `path` keep `null` values by default. FOCA doesn't warn of
such removals; to find affected patches, look for `null` in them (e.g.,
`grep -n null patches/*.yaml`). Patches that set a whole mapping to `null`
to replace it in a later patch (e.g., a path item) keep working.

Files of `path` merged into the first one keep `null` as a value; to let them
remove keys, too, set `merge_null_removes: true` for the specification (see
[merging specification
files](configuration/api.md#merging-specification-files)).

## The permissions API reports invalid fields

FOCA's [permissions API](access-control.md#rule-values) (`/permissions`)
rejects invalid rules, identifiers and pages as
[invalid fields](problems/invalid-request.md), like the app's own endpoints:
`400` responses have the `type` and `code` (`invalid-request`) of invalid
requests and name the invalid field in `errors`, with a [stable
code](problems/invalid-request.md#codes-of-the-permissions-api) and without
the rejected value. They were plain `400 Bad Request` problems with only a
`detail`.

The controllers of `foca.security.access_control.access_control_server`
now raise `foca.errors.InvalidFieldsProblem` instead of
`foca.errors.BadRequest`, so these responses get the problem details mapped
to `RequestValidationProblem`, not those mapped to `BadRequest`; with
`exceptions.public_members`, list `type`, `code` and `errors` to send the
fields. Tests that expect `BadRequest` from these controllers expect
`InvalidFieldsProblem` instead. `validate_rule()` and `validate_rule_value()`
of `foca.security.access_control.foca_casbin_adapter.casbin_rule` raise
`InvalidRuleError`, a `ValueError` with the invalid field and its code, so
code catching `ValueError` keeps working.

## The plugin store limits namespaces

The entries of each namespace of the [plugin
store](configuration/plugins.md#plugin-store) (`ctx.store`; one per
distribution of plugins) now take at most `max_namespace_size` bytes
(`plugins.defaults.store`, or per plugin `plugins.available.<id>.store`;
in FOCA's Helm library chart 0.6.0, `maxNamespaceSize`), by default 16 MiB:
each entry counts its key and value plus 64 bytes. Writes that would exceed
it raise `foca.plugins.StoreFullError` (a `StoreError`, so plugins that
catch `StoreError` keep working), which fails the plugin call unless the
plugin catches it. Before, a namespace was bounded only by `max_ttl`.

- If plugins may keep more (e.g., large caches with long TTLs), raise the
  limit before upgrading. Apps log failed plugin calls as warnings; look for
  `Plugin store namespace full`.
- With a database, FOCA keeps the size of each namespace in the new
  collection `plugin_store_sizes` of the same database, and each entry's
  size in a new field `size` (see [how namespace sizes are
  counted](configuration/plugins.md#how-namespace-sizes-are-counted)).
  The database user needs the same rights on `plugin_store_sizes` as on
  `plugin_store`: nothing changes with the `readWrite` role on the
  database, but roles limited to the collection `plugin_store` need the new
  collection too. Entries written by earlier versions count 64 bytes each
  until they are replaced or expire. During a rolling upgrade, writes of
  replicas of earlier versions aren't counted until the next count (within
  5 minutes of each upgraded process), so namespaces may exceed the limit
  briefly.
- **Breaking** for store backends of their own (implementations of
  `foca.plugins.store.StoreBackend`): `ctx.store` now passes the keyword
  argument `max_size` to `set()` and `increment()`, so backends without it
  fail with `TypeError` at the first such call. Add it, and raise
  `StoreFullError` if the namespace would exceed it.

## Plugins get WES runs in a defined shape

Plugins of WES gateways now get runs (`POST /runs`) in one shape for every
gateway (see [WES requests](plugins.md#wes-requests)): the payload is a
`dict` of the form fields, with `workflow_params`, `tags` and
`workflow_engine_parameters` parsed from JSON, and the files as a list of
`foca.plugins.Attachment` under `workflow_attachment`. The engine checks
this shape after each `request` and `forward` hook that got a run, with the
new limits [`plugins.attachments`](configuration/plugins.md#attachments)
(by default 256 files and 32 MiB per run).

- **Breaking** for WES apps that pass a payload of another shape (e.g.,
  `tags` as a JSON string, or the web framework's uploads as files): every
  hook then fails with `invalid_output`. Build the payload with
  `foca.plugins.wes.parse_run_request()` and check it with
  `check_run_request(run, host.conf.attachments)` before `host.start()`;
  forward it with `run_request_form()`.
- `host.start()` now reads the tags of WES runs from the payload, as for TES
  tasks: `tags=` is no longer needed (passing it still works).
- Raise `plugins.attachments` before upgrading if runs may have more or
  larger files.

## Patterns match at the end only

A `$` in a `pattern` of a request body or parameter now matches only at the
end of the value, as in ECMA 262, to which JSON Schema and OpenAPI refer (see
[stricter request checks](configuration/api.md#stricter-request-checks)).
Before, it also matched before a newline at the end, as in Python, so that,
e.g., `^[0-9a-f]{24}$` accepted `"<24 hex digits>\n"`, and controllers that
parsed such values further could fail with `500`. Requests with such values
are now refused with `400`, naming the field (`pattern-mismatch`).

- Clients that send values with a trailing newline to such patterns (e.g.,
  identifiers read from a file without stripping them) must strip it.
- Apps may drop their own checks of such values (e.g., `re.fullmatch()` of
  page tokens in controllers).
- Apps with a custom `body` or `parameter` validator in `validator_map`
  check bodies or parameters as their validator does, not with FOCA's check.

## Backend lists know WES

[Backend lists](plugins.md#backend-lists) take WES instances
(`type: ga4gh-wes`, API `wes` in `foca.plugins.backends.API_TYPES`), and
`ServiceEntry` has an optional `api_path` (by default the API's path, e.g.,
`/ga4gh/wes/v1`). `BackendList(entries, api=...)` now refuses backend
entries of another API's type. Nothing needs to change, but WES gateways
can drop their own registration of the WES API type, subclasses of
`ServiceEntry` that only change its `type` or add `api_path`, and
subclasses of `BackendList` that bypass its validation, and pass
`api="wes"` instead.

## FOCA resolves the credentials of backends

FOCA now resolves the [credentials of
backends](plugins.md#credentials-of-backends) that `auth` of backend lists
names, for TES and WES gateways alike: `ClientCredentialsConfig` for the
gateway's client at its identity provider (next to the backend list, e.g.,
`custom.wes.client_credentials`), `TokenProvider` for its access tokens
(client credentials grant, cached per process), `get_credentials()` for the
credentials of each request, and `check_backends()` for the startup checks.
`ServiceEntry` has an optional `audience`, for gateways that request a
token per backend (`audience_parameter`); it needs `auth:
client_credentials`.

Nothing needs to change. Gateways with token clients of their own (e.g.,
proTES's `TokenProvider` and `check_backends`) can switch to FOCA's: move
the URL schemes of the identity provider into `client_credentials`
(`client_credentials.url_schemes`), create a `TokenProvider` with the names
of the variables of the client ID and secret, and catch `CredentialsError`
where they caught their own error. Subclasses of `ServiceEntry` that add
`audience` themselves keep working, but FOCA now checks it too.

## Choices of unknown backends are refused

A client's [backend choice](plugins.md#per-request-pipelines-and-backends)
`{"type": "allow", ...}` (tag `gateway.backends`) of identifiers only, none
of which names a backend of the gateway (e.g., a mistyped identifier), is
now refused with `400` (problem type `invalid-backends`, "None of the chosen
backends is known") by `run.route()`, before the `route` phase. Before, it
ended without a backend, which apps answer as an outage (e.g., proTES with
`503`), and the client couldn't tell why. Choices with a URL, `order` and
`forbid` are unchanged.

- Clients that pin requests by identifier get `400` instead of `503` for an
  unknown identifier: the gateway's configuration has no such backend.
- Apps that pass only some of their backends to `run.route()` (e.g., those
  that can run the request) pass the others as `known`
  (`run.route(candidates, known=others)`), so that an `allow` of one of them
  still ends without a backend, which they can explain (e.g., proTES's `400`
  for a TES version that no chosen TES instance supports); otherwise, it is
  refused as unknown.
- Apps that store failed runs from `GatewayError.run` get the run of the
  refusal too.

## Request bodies are limited by default

Request bodies larger than 10 MiB are rejected with `413` by default
([`server.max_content_length`](configuration/server.md#request-body-size),
before without a limit), and URL-encoded bodies
(`application/x-www-form-urlencoded`) larger than 500 kB as well (the new
setting `server.max_form_memory_size`, Flask's `MAX_FORM_MEMORY_SIZE`, which
Werkzeug applied to multipart fields only). Bodies are checked before requests
are authenticated, so that anonymous clients can no longer make the app parse
bodies of any size (Connexion parses the body of every request, also as a
form, before it checks the request's token).

- If the app's config sets `server.max_content_length: null` (as the
  [configuration
  template](https://github.com/elixir-cloud-aai/foca/blob/dev/templates/config.yaml)
  did before), its bodies stay unlimited: remove the key, or set a limit
  (e.g., `10485760`), unless a reverse proxy or ingress limits body sizes.
- If clients may send larger bodies (e.g., WES runs with attachments, by
  default up to 32 MiB), set `server.max_content_length` to the largest size
  you accept, or to `null` if the reverse proxy or ingress in front of the app
  limits body sizes.
- If operations accept URL-encoded form data larger than 500 kB, raise
  `server.max_form_memory_size` (or set it to `null`). Multipart bodies keep
  Flask's limit of each non-file field, which this setting now configures.

## Bearer tokens are checked before bodies are read

On operations that require [bearer tokens validated by
FOCA](authentication.md#tokens-are-checked-before-bodies-are-read), FOCA
checks the token before the request body is read, and before the body size
check. Requests without a valid token get the same `401` as before, but with
their body unread:

- Anonymous requests with too large bodies get `401` instead of `413` on
  these operations; requests with a valid token get `413` as before.
- Each token is validated once per request: Connexion's security handler
  gets the token info of FOCA's check. Replacing
  `foca.security.auth.validate_token` (e.g., with `monkeypatch.setattr()` in
  tests) still applies to both, as long as it's replaced before requests are
  handled. Apps that mock Connexion's security handler instead (e.g.,
  `verify_bearer`) also need to replace `validate_token`, or send valid
  tokens.

Operations with optional tokens, API keys or other validation functions are
handled as before.
