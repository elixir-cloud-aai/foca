# Migrating to 0.18.0

FOCA 0.18.0 keeps one cached Casbin enforcer per app instance instead of
loading all policy rules for each enforced request, so requests cost about
the same with many rules as with few (cf. [scaling](access-control.md#scaling)).
Most apps need no changes to run with FOCA 0.18.0; check the breaking
changes below if your app uses access control and uses the enforcer
directly, changes rules in the database directly or maps groups to roles,
or if its clients or tests rely on the responses to invalid requests, if
it trusts several token issuers, and
the other changes if your app creates FOCA's Casbin adapter itself.
Upgrading from 0.16 or earlier? Start with [Migrating to
0.17.0](migration-0.17.md).

## Breaking changes

### Shared enforcer

`get_enforcer()` (in `foca.security.access_control.enforcement`), which
returned a new enforcer with all rules loaded for each call, is removed.
The enforcer is now shared by the threads of the app instance and kept up
to date with the stored rules; use it via the new context manager
`locked_enforcer()`, which holds its lock, so that it is not updated while
in use (e.g., by another thread with gunicorn's `--threads`):

```python
from foca.security.access_control.enforcement import locked_enforcer

with locked_enforcer() as enforcer:
    allowed = enforcer.enforce(subject, path, method)
```

- Use it briefly, as other requests of the instance wait meanwhile.
- Do not store the enforcer for later use or change its rules other than
  via its methods that also store them (e.g., `add_policy()`): changes made
  via the adapter are recorded and applied to the enforcers of all app
  instances, while, e.g., `enforcer.clear_policy()` would change the
  shared enforcer of one instance only. Updating rules (e.g., via
  `update_policy()`) and removing several rules at once (e.g., via
  `remove_policies()`), which FOCA's adapter does not support, now raise
  `NotImplementedError` before changing the enforcer (before, they raised
  an `AttributeError` or returned `False`, or changed only the enforcer of
  the call, without storing the change).

### Rules changed in the database directly

App instances now notice changed rules via a record of the changes, in
collection `<collection_name>_changes` next to the policy collection (e.g.,
`policy_rules_changes`), which FOCA's adapter updates on each write. Rules
added, changed or removed in the database directly (e.g., with `mongosh`)
are therefore no longer applied from the next request. After such changes,
call `mark_changed()` on the app's adapter
(`current_app.config["casbin_adapter"]`), or restart the app; all rules are
then loaded again on the next request of each instance.

During a rolling update from 0.17, instances of 0.17 do not record their
changes, so instances of 0.18 may miss rules they change: upgrade all
instances before granting or revoking roles, or call `mark_changed()` once
the update is complete.

### Group settings that cannot apply

With access control enabled, `Foca.create_app()` now refuses to start
(`ValueError`, naming the setting to change) if
`security.access_control.group_mapping` maps groups, but `claim_groups` is
not set, as no mapping can then apply, or if `claim_groups` has surrounding
whitespace, as the claim is looked up as written (cf. [startup
checks](configuration/access-control.md#startup-checks)). Before, such apps
started, but no user got the roles of their groups. Set `claim_groups` to
the claim with the groups of users, without whitespace, or unset
`group_mapping`. An empty `group_mapping` without `claim_groups` is still
allowed.

### Subjects of several issuers

If several token issuers are trusted (`security.auth.issuers`), subjects
are now namespaced by issuer, as `<namespace>|<subject>`, so that equal
subjects of different issuers are different users: by default, the
namespace is the issuer (e.g., `https://login.example.org/oidc/|alice`, in
rules `user:https://login.example.org/oidc/|alice`). This applies to
`get_current_subject()`, policies, resource roles and owners, and to users
named by service clients (`get_on_behalf_of_user()`), which are namespaced
by the issuer of the service client. Before, users of different issuers
with the same subject got each other's rights. Apps with a single trusted
issuer are not affected.

Rules, role grants and owners stored for bare subjects then no longer
apply. To keep them for the users of one issuer (e.g., the one trusted
before), set its `namespace` to `""`, so that its subjects stay bare; or
rewrite them to the namespaced form (cf. [trusting a second
issuer](authentication.md#several-trusted-issuers)). Issuers can be listed
with settings of their own (`issuer`, `namespace`) instead of as URLs;
issuers with the same namespace, or several issuers with bare subjects, are
refused on startup. Apps that name other users (e.g., in rules or when
granting roles) must name users of namespaced issuers in the namespaced
form, e.g., via `namespace_subject()` (in `foca.security.auth`).

### Validation details by default

`exceptions.validation_details` now defaults to `true`: responses to
requests rejected by Connexion's validation (`BadRequestProblem` and
`ExtraParameterProblem`) get the reason as `detail` (e.g., `"'name' is a
required property"`) instead of the `detail` mapped to the exception
class, and the reason is logged at `INFO` instead of the exception, while
Connexion's own, unescaped messages on rejected requests are filtered out
(cf. [request-specific
details](configuration/exceptions.md#request-specific-details)). This
applies to the app's own `BadRequestProblem` and `ExtraParameterProblem`
exceptions (and their subclasses) too, e.g., FOCA's own for an invalid user
acted for: they are sent with their own `detail` and logged at `INFO`
instead of as exceptions, so keep internals out of their `detail`. Update
clients and tests that expect the mapped `detail`, or set
`exceptions.validation_details: false` to keep the previous responses and
logs (Connexion's validation logger is filtered for the whole process, so
its messages on rejected requests are only logged again if no app of the
process has validation details on).

## Other changes

### Python 3.15

FOCA supports Python 3.11 to 3.15. Images are published for Python 3.15
too (`0.18.0-py3.15`); the unsuffixed tags (`0.18.0`, `latest`) stay on
Python 3.13, the newest version in its security-fix phase (cf.
[Docker](getting-started.md#docker)). PyYAML publishes no wheels for Python
3.15 yet: pip builds it from source, without its optional C extension
unless libyaml is installed, which FOCA does not need. On Python 3.15, FOCA
requires pydantic 2.14 or later, as older versions need pydantic-core
versions without Python 3.15 wheels.

### Indexes and adapter

- Policy collections may no longer be named such that
  `<collection_name>_changes` is the name of an app collection in the same
  database: FOCA refuses to start then, as for the policy collection
  itself.
- Rules and recorded changes are read from the primary of a replica set,
  also if the app's connection prefers secondaries.
- FOCA's adapter creates two more indexes on the policy collection (on
  `ptype`, `v0` and `v1`, and on `ptype` and `v2`), on its first write, to
  read changed rules quickly. If creating them fails (e.g., as the database
  user may not create indexes), a warning is logged and rules are read
  without them.
- `Adapter` takes a new optional argument `changes_collection` for the
  collection recording changes (default: the policy collection's name with
  suffix `_changes`, in the same database), and has new methods:
  `mark_changed()`, `get_changes()` (the recorded changes),
  `load_filtered_rules()` (stored rules matching a filter) and
  `remove_policy_by_id()`, which the permissions API now uses to delete
  rules, so that their removal is recorded.

### Gunicorn settings

FOCA now has default Gunicorn settings for apps in containers, in the
module `foca.gunicorn_config` (e.g., `gunicorn --config
python:foca.gunicorn_config "wsgi:create_app()"`; cf. [running in
production](deployment.md#running-in-production)). They are opt-in: apps
that pass their own Gunicorn options are not affected. FOCA does not depend
on Gunicorn; apps still install it themselves. The petstore example's image
uses the module: run on its own, it now has one worker with four threads
and a keep-alive of 100 seconds; with the Helm chart, the chart's `gunicorn`
values apply as before (via `GUNICORN_CMD_ARGS`). The petstore chart's
keep-alive (`petstore.gunicorn.keepalive`) is now 100 seconds too (before:
75), longer than the idle timeouts of common proxies (e.g., 90 seconds for
Traefik); app charts that copied its values can raise theirs likewise.

### Ownership by document field

Apps whose resources are never shared can now store the owner in each
resource's document (`set_owner_field()`) and filter by it
(`owner_filter()`), instead of storing a policy rule per resource (cf.
[ownership by document field](access-control.md#ownership-by-document-field)).
Resource ownership via policy rules (`add_owner()` etc.) is unchanged and
stays the default of FOCA's helpers.

### Startup checks

`Foca.create_app()` logs new warnings about settings that let the app run,
but likely not as intended (cf. [startup
checks](deployment.md#startup-checks)); apps behave as before (except for
the errors about [group settings that cannot
apply](#group-settings-that-cannot-apply)):

- With access control enabled: no group or service client is mapped to the
  role `admin` and no rule of `policies` grants it (roles granted via
  stored rules still apply); `group_mapping` is set without
  `keep_group_subjects`, or is empty without `claim_groups`. The groups
  and service clients mapped to `admin` are logged (`INFO`). Set the new
  option
  `security.access_control.require_admin: true` (default: `false`) to
  refuse to start without admins instead (cf. [startup
  checks](configuration/access-control.md#startup-checks)).
- The service info has values in reserved example domains, e.g., `id:
  org.example.my-service` or `https://example.org`, as in templates (cf.
  [placeholders](configuration/service-info.md#placeholders)).

Apps that checked these settings themselves can drop their checks. Apps
that don't support service clients can call the new
`foca.security.auth.require_no_service_clients()` on startup (cf. [service
tokens](authentication.md#service-tokens)).

### Public URLs and typed custom configuration

Apps can now set their public URL, as clients see it behind a reverse proxy
or ingress, in [`server.public_url`](configuration/server.md#public-url),
and build URLs of their paths with
[`get_public_url()`](utilities.md#building-public-urls) (in
`foca.utils.urls`), e.g., for `Location` headers. `get_custom_config()` (in
`foca`) returns the [custom configuration](configuration/custom.md) of the
app, typed. Both are opt-in: apps that don't use them need no changes.

### Logging

Casbin no longer logs all stored rules (including user identifiers of
resource-scoped roles) at level `INFO` when the enforcer is created; only
the model is logged.
