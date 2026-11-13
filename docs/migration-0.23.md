# Migrating to 0.23.0

FOCA 0.23.0 namespaces subjects by issuer also with a single trusted issuer,
lets patches of API specifications remove keys, and the permissions API
reports invalid fields. Apps and deployments need to change only if:

- they use access control or store subjects (e.g., owners), as [subjects are
  always namespaced by issuer](#subjects-are-always-namespaced-by-issuer);
- their [patches set values to `null`](#null-in-patches-removes-keys);
- their tests or clients expect the [permissions API's plain `400`
  responses](#the-permissions-api-reports-invalid-fields).

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
the permissions API lose their roles. Choose one of these before you upgrade:

1. **Keep the subjects bare:** set `namespace: ""` for the trusted issuer, so
   that its subjects stay bare as before, also if another issuer is trusted
   later (whose subjects are then namespaced). Nothing else changes:

    ```yaml
    security:
      auth:
        issuers:
          - issuer: https://login.example.org/oidc/
            namespace: ""
    ```

2. **Namespace the stored subjects:** optionally set a shorter `namespace` for
   the issuer (e.g., `ls`; it must not change later), then, with the
   configuration of the new version:

    1. Namespace the users of configured rules in
       `security.access_control.policies`, e.g., `[g, "user:alice", admin]`
       to `[g, "user:https://login.example.org/oidc/|alice", admin]`. FOCA warns
       on startup of configured and stored rules naming users without the
       namespace of a trusted issuer.
    2. Rewrite the stored rules with the [command-line
       tool](cli.md#namespace-subjects), first with `--dry-run` to count them,
       before the new version serves requests (e.g., in a Helm `pre-upgrade`
       hook, or right after the upgrade; until then, the stored users lose
       their roles):

        ```shell
        foca --app my_app.app:foca namespace-subjects --dry-run https://login.example.org/oidc/
        foca --app my_app.app:foca namespace-subjects https://login.example.org/oidc/
        ```

    3. Rewrite the subjects that your app stores itself, e.g., owners in
       documents (`set_owner_field()`, field `owner` by default) or users
       stored from `get_current_subject()`. For owner fields, e.g., in
       `mongosh`, for the collection `pets` and the issuer as namespace:

        ```javascript
        const ns = "https://login.example.org/oidc/|";
        db.pets.updateMany(
          {"owner.type": "user", "owner.sub": {$not: {$regex: "^https://login\\.example\\.org/oidc/\\|"}}},
          [{$set: {"owner.sub": {$concat: [ns, "$owner.sub"]}}}]
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
