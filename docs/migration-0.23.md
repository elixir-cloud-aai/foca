# Migrating to 0.23.0

FOCA 0.23.0 namespaces subjects by issuer also with a single trusted issuer,
names users outside the path in the endpoints for managing roles, lets patches of API specifications remove keys, the permissions API reports
invalid fields, and the plugin store limits the size of each namespace. Apps
and deployments need to change only if:

- they use access control or store subjects (e.g., owners), as [subjects are
  always namespaced by issuer](#subjects-are-always-namespaced-by-issuer);
- their [patches set values to `null`](#null-in-patches-removes-keys);
- they route operations to `putUserRole` or `deleteUserRole`, as [the
  endpoints for managing roles name users outside the
  path](#the-endpoints-for-managing-roles-name-users-outside-the-path);
- their tests or clients expect the [permissions API's plain `400`
  responses](#the-permissions-api-reports-invalid-fields);
- their plugins keep more than 16 MiB in the [plugin
  store](#the-plugin-store-limits-namespaces), or they implement a store
  backend of their own.

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
- users named to the endpoints for managing roles are namespaced, so they
  contain slashes and `|` with issuers as namespaces; the endpoints therefore
  [name users outside the
  path](#the-endpoints-for-managing-roles-name-users-outside-the-path).

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

## The endpoints for managing roles name users outside the path

The ready-made controllers `putUserRole` and `deleteUserRole` of
`foca.security.access_control.ownership_server` (cf. [endpoints for managing
roles](access-control.md#endpoints-for-managing-roles)) take the user from
the request body (`{"user": "...", "role": "..."}`) and from the query
parameter `user`, instead of the last path parameter. Users are namespaced by
issuer (e.g., `https://login.example.org/oidc/|alice`), so in paths they
needed `format: path` routes and `*` in rules, and proxies that merge slashes
(e.g., nginx's `merge_slashes`) or decode paths changed them. The resource is
still taken from the path: up to its last path parameter. The group
controllers are unchanged.

Before upgrading, change the operations routed to the user controllers (and to
aliases or wrappers of them), their rules and their clients, e.g., for the
operations of the [petstore-access-control
example](https://github.com/elixir-cloud-aai/foca/tree/dev/examples/petstore-access-control):

1. In the API specification, drop the user's path parameter: route
   `PUT /pets/{id}/permissions/users` to `putUserRole`, with `user` (a
   string) next to `role` in the request body, and
   `DELETE /pets/{id}/permissions/users` to `deleteUserRole`, with a
   required query parameter `user` (a string):

    ```yaml
    /pets/{id}/permissions/users:
      put:
        operationId: putPetUserRole
        parameters:
          - {name: id, in: path, required: true, schema: {type: integer}}
        requestBody:
          required: true
          content:
            application/json:
              schema:
                type: object
                required: [user, role]
                properties:
                  user: {type: string, minLength: 1}
                  role: {type: string, enum: [owner, maintainer]}
      delete:
        operationId: deletePetUserRole
        parameters:
          - {name: id, in: path, required: true, schema: {type: integer}}
          - {name: user, in: query, required: true, schema: {type: string, minLength: 1}}
    ```

    With the old routes, requests without `user` in the body or query are
    rejected with `400`; with a user in the body or query, the controllers
    would take the path up to the user as the resource, which has no owners
    (`404`).

2. In the rules, match the new paths, without `*`, e.g.,
   `[p, owner, "/pets/:id/permissions/users", "(PUT)|(DELETE)"]` instead of
   `[p, owner, "/pets/:id/permissions/users/*", "(PUT)|(DELETE)"]`.
3. In wrappers of the controllers, don't take the user from the arguments:
   Connexion passes the caller as `user`, so `kwargs["user"]` is the caller,
   not the user named in the query. Pass the arguments on as before.
4. In clients, send the user in the body to grant a role, and percent-encoded
   in the query to revoke the roles, e.g.,
   `DELETE /pets/0/permissions/users?user=https%3A%2F%2Flogin.example.org%2Foidc%2F%7Cbob`.

The path variants are gone without a transition period, as FOCA is not yet at
1.0.

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
