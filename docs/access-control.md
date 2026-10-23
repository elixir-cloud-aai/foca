# Access control

FOCA provides configurable, [Casbin][casbin-docs]-based access control:
policies can be enforced on all operations of an app (using the claims of
validated bearer tokens), managed via a permissions API and per resource
(e.g., for resource owners). This guide explains how it works and how to
configure it; the [configuration reference](configuration/access-control.md)
lists all options.

## Why do we need it?

This feature is designed to allow you to create more secure applications. You
may need to restrict the API usage on the basis of specific users or perhaps
create user groups with access to specific functionalities. For example, user A
should be allowed to read a resources, while user B should _also_ be able to
modify/write to that resource. User C, on the other hand, should not be able to
access that resource at all!

To address this and similar use cases, [FOCA][res-foca] provides configurable
access control based on the popular [Casbin][casbin-docs] project.

## How does it work?

The access control model is abstracted into a configuration file based on the
**PERM (Policy, Effect, Request, Matchers) metamodel**. You can provide the
required model definition as per your particular use case. Policies can then be
defined on the basis of that model, stored in a MongoDB database and enforced
on incoming requests based on the matchers and effects defined in the policies.

The flow described above is highlighted in the schema below (taken from
[Casbin][casbin-docs]):

![Casbin Flow][img-casbin-flow]

### PERM Model

The PERM model is composed of four foundations - Policy, Effect, Request,
Matchers - describing the relationship between resources and users.

#### Policy

A _policy_ defines _who can do what_ or _who has what permissions_.

The basic syntax for a policy is

```console
p = sub, obj, act, eft
```

which can be read as: Who(`sub`) can/cannot(`eft`) do what(`act`) on some
resource(`obj`)?

#### Effect

An _effect_ can be understood as a model in which a logical combination
judgment is performed on the result of a matcher. For example:

```console
e = some(where(p.eft == allow))
```

This statement expressed that if the matching strategy result `p.eft` has the
result of (some) `allow`, then the final result is `True` and access is
granted.

Here `eft` can be either `allow` or `deny`. If it is not included (which is
frequently the case), the default value is `allow`.

In the [above diagram](#how-does-it-work), as per the defined policy:

1. John has permission to read `RECORD1`
2. John has no permission to modify/write to `RECORD1`
3. Harry has permission to read `RECORD1`
4. Harry has permission to modify/write to `RECORD1`

#### Request

A _request_ defines the names and order of parameters to be passed to the
matchers. A basic request is a tuple object, requiring at least a subject
(accessed entity), object (accessed resource) and action (access method).

For instance, a request definition may look like this:

```console
r = sub , obj, act
```

In the [above diagram](#how-does-it-work), as per the incoming request:

1. John wants to read `RECORD1`
2. John wants to modify/write to `RECORD1`

#### Matchers

A _matcher_ integrates the request (`r`) and policy (`p`). For example:

```console
m = r.sub == p.sub && r.act == p.act && r.obj == p.obj
```

This simple and common matching rule means that if the requested parameters
(entities, resources, and methods) are equal, that is, if they can be found in
the policy, then the policy result (`p.eft`) is returned.

## Configuration

Access control is disabled by default. Enable it via
`security.access_control.enabled`; FOCA then registers the policy
collection, the Casbin adapter and the permissions API. As the permissions
API requires bearer tokens, trusted token issuers must be configured via
`security.auth.issuers`. All other access control options require `enabled:
True`; FOCA refuses to start otherwise.

Note that which operations of your API require authentication is defined by
the security requirements of the operations in your API specifications, not
by FOCA's configuration.

You can tweak the default access control behavior by setting a number of
configuration parameters:

```yaml
security:
  auth:
    issuers:
      - https://login.example.org/oidc/
    audience:
      - my-app
  access_control:
    enabled: True
    model: access_control_model_definition
    api_specs: 'path/to/your/access/control/specs'
    api_swagger_ui: False
    api_serve_spec: False
    db_name: access_control_db_name
    collection_name: access_control_collection_name
    enforce: False
    claim_identity: sub
    subject_prefix: "user:"
    claim_groups: group_claim
    group_mapping:
      group_claim_value: role
    resource_roles_cover_subpaths: True
    id_charset: ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789
    id_length: 6
    self_service: False
    policies:
      - [p, "*", /pets, GET]
    log_traffic: True
```

> The application created with this config would provide an access control
> model `model`. Corresponding permissions could be accessed and altered
> by a user with admin permissions via the endpoints defined in `api_specs`
> (FOCA's default specification if not set), at
> `/admin/access-control/permissions` by default. Permissions will be stored
> in collection `collection_name` of MongoDB database `db_name`.
>  
> Cf. the [API model][docs-models-access-control] for further options and details.

### Configuring the database

FOCA sets up a MongoDB database and collection to manage permission resources.
Default names are set to `access_control_db` and `policy_rules`,
respectively. To manually set the names, provide values for the `db_name` and
`collection_name` parameters, respectively.

If `db_name` is the name of one of the app's databases (as configured in
`db.dbs`), the permission collection is added to that database, and the app's
collections are kept. In that case, `collection_name` must differ from the
names of the app's collections in that database; otherwise, FOCA refuses to
start.

### Configuring the API

FOCA comes with a default API for configuring permission resources, served
at `/admin/access-control`. However, you can provide a custom [Swagger 2.x or
OpenAPI 3.x][res-openapi] definition for `/permissions` endpoints via
`api_specs`. Its `servers` (or `basePath`) define where the endpoints are
served, and the `operationId` of each operation which controller handles it
(e.g., FOCA's default controllers in
`foca.security.access_control.access_control_server`, or your own).

The Swagger UI and the specification of the permissions API are not served
by default. Set `api_swagger_ui: True` to serve the Swagger UI, together with
the specification, which the UI needs (`api_serve_spec: False` is then
rejected), or `api_serve_spec: True` to serve the specification only.

Requests to FOCA's default controllers and their responses are logged (cf.
[`log_traffic`](utilities.md#logging-requests-and-responses)), as in FOCA
0.14; set `log_traffic: False` to not log them. Your own controllers log
their traffic as they are decorated.

Rules created via `POST /permissions` get a random identifier of `id_length`
characters (4 to 64, default: 6) drawn from `id_charset` (letters, digits,
`-`, `.`, `_` and `~`; default: letters and digits), which together must allow
for at least one million different identifiers. Identifiers are unique:
FOCA creates a unique index on them and retries with a new identifier if a
generated one exists already. If stored rules share identifiers (e.g., rules
stored by previous FOCA versions), the index cannot be created: FOCA logs an
error and stores rules without guaranteeing unique identifiers. To resolve
this, delete or re-create (e.g., via `POST /permissions`) the rules with
duplicate identifiers and restart the app. Identifiers supplied via `PUT
/permissions/{id}` must have the same length and characters; otherwise, the
request is rejected with `400`.

The specs and controllers used by default can be accessed via the links below:

* [Default Specifications][default-specs]
* [Default Controller Definitions][default-controllers]

### Configuring policies

Policy rules the app relies on (e.g., which roles may do what) can be set via
`policies`, each given as its policy type followed by its values:

```yaml
security:
  access_control:
    policies:
      - [p, "*", /services, POST]
      - [p, owner, "/services/:id", "(PUT)|(DELETE)"]
      - [g, "user:alice", admin]
```

On startup, FOCA validates the rules against the model (cf. [rule
values](#rule-values)), refusing to start if any rule is invalid. It then
replaces the stored configured rules with those of the configuration:
configured rules that were removed from the configuration are removed,
missing ones are added, and unchanged ones are kept, with their identifiers.
The configuration is the only place to change configured rules: the
permissions API responds with `409` to requests to change or delete them,
and the [resource role helpers](#resource-scoped-roles) keep them, with a
warning. Other stored rules (e.g., added via the permissions API) are kept,
also if they have the values of a configured rule. With `policies: []`, all
configured rules are removed. Without `policies`, stored configured rules
are removed, too, with a warning (e.g., if they were set via a config
overlay that was removed since); apps that never configured rules only check
for them, and start without the database, with a warning, if it is not
available.

Configured rules are replaced atomically, so that requests are always
checked against either the previous or the new configured rules, never a
mix, also while app instances with different rules start concurrently (e.g.,
during a rolling update) or if a startup fails midway; no MongoDB
transactions are needed. FOCA stores the new rules as a new generation and
then makes it the active one, in a single write to a document with
identifier (`_id`) `configured_policies` in the policy collection; only
then, rules of the previous generation are removed. Only the configured
rules of the active generation are loaded, listed by the permissions API and
protected from changes. If instances with different rules start
concurrently, the rules of the instance that activates its generation last
are kept. Rules of generations that never became active (e.g., because an
instance failed while storing them) are removed on a later startup, once
older than an hour. Configured rules stored by FOCA 0.16 and earlier are
used until the first startup with a later version replaces them.

!!! warning "Apps sharing a policy collection"
    All apps using the same policy collection must configure the same rules:
    each startup replaces the configured rules of the others, also if
    `policies` is not set.

Rules can also be given as a mapping of names to rules (named rules), so
that config overlays can add, change or remove (`null`) single rules without
restating the others (see [named
entries](configuration/index.md#named-entries)):

```yaml
security:
  access_control:
    policies:
      create-services: [p, "*", /services, POST]
      owners-manage: [p, owner, "/services/:id", "(PUT)|(DELETE)"]
      alice-admin: [g, "user:alice", admin]
```

Named rules are stored like listed ones (names are not stored, so switching
between both forms does not change stored rules); their names are logged on
startup, and errors name the invalid rule. As with `policies: []`, removing all
named rules removes all configured rules. Apps that expect operators to adapt
their rules should use named rules. To remove a rule that an overlay added
(e.g., to an app without policies), remove it from the overlay; if no
rules remain, the configured rules are removed on the next startup (see
above).

!!! danger "Review overlays like code"
    Rules in overlays grant rights: a broad rule (e.g., `[p, "*", "/*",
    GET]`) also applies to operations it was not meant for, including the
    permissions API, which then exposes all rules to any authenticated user.
    Limit rules to the paths they are meant for.

Rules that depend on the app's data, such as roles on resources, are better
managed by the app (cf. [resource-scoped roles](#resource-scoped-roles)).

### Configuring the access control model

Access control works on the basis of model definitions. If a custom model
definition is not provided via parameter `model`, a [role-based access control
(RBAC) model definition][default-model] will be used by default. To learn more
on writing your own custom models, refer to the [Casbin
documentation][res-casbin-models].

## Enforcing policies with OIDC tokens

Policies can be enforced on all operations of an app's API specifications
through configuration alone:

```yaml
security:
  auth:
    issuers:
      - https://login.example.org/oidc/
    audience:
      - my-app
    # Only needed if group claims are not in access tokens
    userinfo_claims:
      - eduperson_entitlement
  access_control:
    enabled: True
    model: path/to/resource_role_model.conf
    enforce: True
    claim_groups: eduperson_entitlement
    group_mapping:
      "urn:geant:example.org:group:admins#example.org": admin
```

With `enforce: True`, every operation that requires authentication (i.e.,
that has non-empty `security` requirements, either on the operation or the
specification level) is checked against the policies, using the full request
path as the object and the HTTP method as the action. Policies are enforced
_after_ the bearer token was validated (e.g., by FOCA's
`foca.security.auth.validate_token`). Requests are denied with `403` if no
policy allows them.

* Operations with `security: []` are not enforced, i.e., they are public.
  The same applies to operations with optional authentication, i.e., with an
  empty security requirement alternative (e.g., `security: [{}, {bearerAuth:
  []}]`).
* Single operations can opt out via `x-foca-access-control: false`.
* Operations are selected as resolved by Connexion, so operations of path
  items defined via `$ref` are enforced, too.
* `enforce: True` requires `enabled: True`; FOCA refuses to start
  otherwise.
* Specifications with `disable_auth: true` have no security requirements, so
  none of their operations are enforced, i.e., they are all public. FOCA logs
  a warning on startup if `enforce: True` is set as well.
* With `enforce: False` (default), policies are only enforced on the
  permissions API and on controllers decorated with
  `foca.security.access_control.enforce_access_control`, which applies the
  same checks to a single controller.

### Requiring login for reads

Many apps make reads public (operations with `security: []`). Operators who
want all reads to require login, except, e.g., `GET /service-info`, can do so
in a config overlay, without changing the app, by combining a [spec
patch](configuration/api.md#patching-specifications) with a [named policy
rule](#configuring-policies). This requires that the app uses a named
specification and named policy rules, and enforces policies (`enforce:
True`).

!!! danger "With great power comes great responsibility"
    Patches and rules in overlays can weaken or remove authentication and
    access control as easily as they add it. Review them like code and check
    the startup logs, which list the applied patches and named rules and warn
    of operations that patches make public.

Given an app whose specification (named `my-api` in its configuration, with
base path `/api`) makes `GET /items` and `GET /items/{id}` public:

```yaml title="overlay.yaml"
api:
  specs:
    my-api:
      patches:
        # require a token for the public reads, except `/service-info`
        - paths:
            /items:
              get:
                security:
                  - bearerAuth: []
            /items/{id}:
              get:
                security:
                  - bearerAuth: []
security:
  access_control:
    policies:
      # any authenticated user may read the app's API
      read-api: [p, "*", "/api/*", GET]
```

The patch sets the security requirements of each public read operation (here
with the app's security scheme `bearerAuth`); operations not listed, such as
`GET /service-info`, stay public. As the operations now require
authentication, policies are enforced on them, so the rule allows `GET` for
any authenticated user (`*`), on the app's API only (`/api/*`). Without the
rule, reads would be denied with `403` unless other rules allow them. The
rule is added to the app's rules, which are kept. Use the app's actual
specification name, base path and security scheme; for an app that lists its
specifications or rules, the overlay has to restate them.

### Identity and group claims

The subject is taken from the claims of the **already validated** token,
from the claim set via `claim_identity`. If not set, the claim set via
`security.auth.claim_identity` (default: `sub`) is used, i.e., the same claim
that token validation requires. Use a stable identifier assigned by the
identity provider (like `sub`), not a claim users may choose or change (like
`preferred_username` or `email`). Requests with validated tokens lacking that
claim are denied with `401`.

If `claim_groups` is set, the values of that claim (a list or a single
string) are additionally checked as subjects, i.e., they act as roles. If
the identity provider only provides the claim via user info, not in access
tokens (e.g., `eduperson_entitlement` with LS Login), add it to
[`security.auth.userinfo_claims`](configuration/auth.md). If
`group_mapping` is set, claim values are mapped to roles, and values without
mapping are ignored, unless `keep_group_subjects` is set: then all claim
values are _additionally_ used as group subjects (e.g., `group:vo-members`),
as without `group_mapping` (see [granting roles to
groups](#granting-roles-to-groups)). Access is granted if any of the subjects
is allowed.
This way, no rules are needed per user for group-based access, e.g., for
administrators. Only map claim values to global roles such as `admin`:
roles of `resource_roles` (by default, `owner` and `maintainer`) are
rejected on startup, as a mapped role is a subject of its own, which matches
the rules of that role on _every_ resource (e.g., all members of the group
would own all tasks).

!!! warning "Several trusted issuers"
    Subjects and group claim values are not scoped by issuer: a rule for
    `user:<sub>` or `group:<value>` applies to tokens of _all_ trusted
    issuers (`security.auth.issuers`). If several issuers can issue the same
    `sub` or group claim value to different users (e.g., if users can choose
    their identifier or create groups at one of them), these users get each
    other's rights, up to administrator rights. Only trust several issuers
    together with access control if their subjects and group claim values
    cannot collide, e.g., because they are assigned by the same identity
    provider. FOCA logs a warning on startup if access control is enabled
    with several trusted issuers.

To keep users, groups and roles apart, subjects are namespaced in rules:

| Subject | In rules | Example |
| --- | --- | --- |
| User identifier | prefixed with `subject_prefix` (default: `user:`) | `user:alice` |
| Group claim value without `group_mapping` or with `keep_group_subjects` | prefixed with `group:` | `group:vo-members` |
| Client ID of a [service client](#service-clients) | prefixed with `client:` | `client:conquest` |
| Role (from `group_mapping`, `client_mapping` or `g` rules) | as is | `admin` |

This way, a user whose identifier equals a role name (e.g., `admin`), or an
identity provider group of that name, is not granted that role. For the same
reason, `subject_prefix` must not be empty and must not overlap with
`group:` or `client:`. All rules, including those for the permissions API,
must use these forms (e.g., `p, user:alice,
/admin/access-control/permissions, GET`); rules naming unprefixed user
identifiers do not match.

In your controllers, use `get_current_subject()` and `get_current_groups()`
to get the subject (without prefix) and roles of the current request, and
`get_current_group_values()` to get the values of its group claim as is
(neither mapped nor prefixed):

```python
from foca.security.access_control import (
    get_current_group_values,
    get_current_groups,
    get_current_subject,
)
```

### Service clients

Requests with [service tokens](authentication.md#service-tokens) of service
clients (`security.auth.service_clients`) are enforced with the subject
`client:<client ID>` instead of the token's subject, so that a client never
gets the rights of a user of the same name and vice versa. Group claims
apply as for users, if service tokens have them. To give service clients
roles without rules per client, map them to roles via `client_mapping`; only
service clients can be mapped. As for `group_mapping`, mapped roles must not
be roles of `resource_roles` (e.g., `owner`), which the mapped clients would
otherwise have on every resource, and must not start with `subject_prefix`,
`group:` or `client:`, as the mapped clients would otherwise get the rights
of that subject:

```yaml
security:
  auth:
    service_clients:
      - client_id: conquest
  access_control:
    enabled: True
    client_mapping:
      conquest: submitter
    policies:
      - [p, submitter, /tasks, "(GET)|(POST)"]
```

Requests with this client's service tokens are checked as `client:conquest`
and `submitter`, also if they lack the identity claim (`claim_identity`).
Rules may also name `client:conquest` directly. Tokens that the client
obtained for users are user tokens and are checked as their users.

In your controllers, `get_current_client()` returns the client ID of the
service client of the current request (`None` for user tokens), and
`get_on_behalf_of_user()` the user that a service client allowed to act for
users names (cf. [service tokens](authentication.md#service-tokens)). Use
them, e.g., to make the user a gateway acts for, or else the calling client,
the owner of a new resource: pass `subject_type="client"` and the client ID
(without `client:`) to the [resource role helpers](#resource-scoped-roles):

```python
from foca.security.access_control import (
    add_resource_role,
    get_current_client,
    get_current_subject,
    get_on_behalf_of_user,
)


def postTask(**kwargs):
    resource = ...  # create resource
    user = get_on_behalf_of_user()
    client = get_current_client()
    if user is not None:
        add_resource_role(subject=user, role="owner", resource=resource)
    elif client is not None:
        add_resource_role(
            subject=client, role="owner", resource=resource, subject_type="client"
        )
    else:
        add_resource_role(
            subject=get_current_subject(), role="owner", resource=resource
        )
```

`get_on_behalf_of_user()` rejects requests of other callers that name a user
with `403 Forbidden`; call it in every controller that should honor the
header, so that clients notice when they are not allowed to act for users.

`add_owner(resource)` does the same in one call (see [resource
ownership](#resource-ownership)).

### Resource-scoped roles

FOCA ships a [reference model][resource-role-model] for resource-scoped
roles. It supports rules like the following:

```console
p, *, /services, POST                            # any authenticated user
p, owner, /services/:id, (GET)|(PUT)|(DELETE)    # owners: full access
p, maintainer, /services/:id, (GET)|(PUT)        # maintainers: read/update
p, owner, /services/:id/permissions, (GET)|(PUT) # owners: manage access
p, group:vo-members, /services/:id, GET          # raw group claim value
g2, user:alice, owner, /services/123             # alice owns /services/123
g2, group:team-x, maintainer, /services/123      # team-x maintains it
g, user:bob, admin                               # bob is an admin
```

Actions (HTTP methods) in `p` rules are regular expressions that must match
the _whole_ method: `GET` matches `GET` only (not, e.g., a method containing
`GET`), and `(GET)|(PUT)` matches `GET` and `PUT`.

Subjects with role `admin`, assigned via `g` rules or mapped from group
claims, may do everything. Read operations can be made public via
`security: []`. Note that the paths are the full request paths, including
any base path of your API.

Roles granted on a resource also apply to its sub-paths, split on `/`: owning
`/services/123` covers `/services/123/permissions`, but not `/services/1234`.
This way, endpoints below a resource, such as an endpoint for owners to manage
access to their resource, are enforced like the resource itself. Sub-path
matching only makes a role _apply_ there; what the role may do is still
decided by `p` rules. In the example above, owners may manage access, while
maintainers may not, as no rule allows them to. Trailing slashes in resource
paths are ignored, and roles granted on `/` only apply to `/` itself. Set
`resource_roles_cover_subpaths: False` to only match the exact resource path.

Which subjects get which roles on which resources depends on your app's
domain and is up to your app. FOCA provides helpers to manage resource-scoped
roles (`g2` rules) in your controllers, e.g., to make the creator of a
resource its owner, let owners manage access to their resources, and clean up
when a resource is deleted:

```python
from flask import request

from foca.security.access_control import (
    add_resource_role,
    get_current_subject,
    list_resource_roles,
    remove_resource,
    remove_resource_role,
)


def postService(**kwargs):
    service_id = ...  # create resource
    add_resource_role(
        subject=get_current_subject(),
        role="owner",
        resource=f"{request.path.rstrip('/')}/{service_id}",
    )
    return service_id


def deleteService(id, **kwargs):
    ...  # delete resource
    remove_resource(request.path)
    return id
```

`list_resource_roles(resource)` returns the subjects and roles granted on a
resource, and `remove_resource_role(subject, role, resource)` revokes a
single role. The helpers take and return user identifiers without prefix and
add `subject_prefix` to the rules themselves. `add_resource_role()` raises a
`ValueError` for empty subjects and for roles starting with
`subject_prefix`, `group:` or `client:`: as Casbin resolves roles
transitively, granting, e.g., the "role" `group:auditors` to a user would
otherwise give the user all rights of that group. `remove_resource_role()`
accepts both, so
that existing rules with them can be revoked; it removes all stored copies of
a rule, and `list_resource_roles()` returns each subject and role pair
once, also if a rule was stored more than once (e.g., by concurrent
requests).

`remove_resource(resource)` revokes the roles granted on exactly `resource`,
not those granted on its sub-paths (e.g., `/services/123/files` for
`/services/123`) or on the path with a trailing slash; revoke these
separately, e.g., via `list_resource_roles()` and `remove_resource_role()`.
Neither helper removes rules set via [`policies`](#configuring-policies), as
they would be added again on the next start, nor does FOCA's Casbin adapter
when a single rule is removed via the enforcer (e.g.,
`remove_grouping_policy()`).
Other stored copies of such rules (e.g., also added via the permissions API)
are removed, but the role still applies: `remove_resource_role()` then
returns `False`, and both helpers log a warning. Remove such rules from the
configuration instead. `remove_resource()` returns `True` if it revoked any
role, i.e., if it removed a valid rule that is not also configured. It also
removes invalid rules stored on the resource, which do not count as revoked
roles, and returns `False` for rules that a concurrent call removed first.

#### Granting roles to groups

Resource-scoped roles can also be granted to groups, e.g., to let a team
maintained as a group in the identity provider own a resource. Pass
`subject_type="group"` and the group claim value (as is, without `group:`)
to `add_resource_role()` and `remove_resource_role()`; the helpers prefix it
with `group:` in the rule (e.g., `g2, group:<value>, owner, /services/123`).
As users and groups are prefixed differently, a user and a group of the same
name never get each other's roles. Roles granted to a group apply to every
user whose group claim (`claim_groups`) contains the value. If you also map
group claim values to roles via `group_mapping`, set `keep_group_subjects:
True`, as group claim values are otherwise not used as subjects:

```yaml
security:
  access_control:
    claim_groups: eduperson_entitlement
    group_mapping:
      "urn:geant:example.org:group:admins#example.org": admin
    keep_group_subjects: True
```

When running several instances of your app (e.g., during rolling updates),
only enable `keep_group_subjects` once all instances run a FOCA version that
supports it: older versions reject the option, and instances without it deny
access via roles granted to groups.

`list_resource_roles(resource, subject_type="group")` lists the groups
(without `group:`) and their roles on a resource, `subject_type="user"` the
users (without `subject_prefix`). Without `subject_type`, all subjects are
listed, users without prefix and all other subjects as is (e.g.,
`group:<value>`), as in previous versions; as a user identifier may start
with `group:`, this is ambiguous, so new code should always pass
`subject_type`.

`add_resource_role()` logs a warning when granting a role to a group while
`group_mapping` is set and `keep_group_subjects` is not, as the role would
not apply.

Which users may grant roles to which groups is up to your app. FOCA does not
check whether a group exists or who may grant roles to it; anyone allowed to
call your endpoint could otherwise grant, e.g., ownership of their resource
to any group, or roles that give members access they should not have. A
sensible default is to only let users grant roles to groups they are members
of, and only roles meant to be granted:

```python
from connexion.exceptions import BadRequestProblem, Forbidden
from flask import request

from foca.security.access_control import (
    add_resource_role,
    get_current_group_values,
)

GRANTABLE_ROLES = {"owner", "maintainer"}


def putServicePermissions(id, body, **kwargs):
    if body["subject_type"] not in {"user", "group"}:
        raise BadRequestProblem(detail="Invalid subject type")
    if body["role"] not in GRANTABLE_ROLES:
        raise BadRequestProblem(detail="Invalid role")
    if body["subject_type"] == "group" and (
        body["subject"] not in get_current_group_values()
    ):
        raise Forbidden
    add_resource_role(
        subject=body["subject"],
        role=body["role"],
        resource=request.path.rsplit("/", 1)[0],
        subject_type=body["subject_type"],
    )
```

Also keep in mind that:

* Only accept roles from an allowlist of roles users may grant (e.g., not
  `admin`). The helpers already reject roles starting with `subject_prefix`
  or `group:`, so that users cannot make themselves members of other groups,
  but any other role is granted as given. If your app stored resource roles
  before this check was added, look for resource-scoped role rules (by
  default `g2`) whose role (`v1`) starts with `subject_prefix` or `group:`
  and remove them (e.g., via `remove_resource_role()` or the permissions
  API). To revoke a rule whose subject starts with `group:`, pass
  `subject_type="group"`. FOCA logs a warning for each such rule on the
  first policy load of each app instance (per worker process), but does not
  remove them. The permissions API stores rules as given, i.e., it does not
  apply this check, so only let trusted administrators (by default, subjects
  with role `admin`) write rules via it.
* Only map group claim values (`group_mapping`) and service clients
  (`client_mapping`) to global roles such as `admin`, not to names of
  resource-scoped roles such as `owner` or `maintainer`: a mapped role is a
  subject of its own, which matches the `p` rules of that role on _every_
  resource (e.g., all members of the group would own all services). Roles of
  `resource_roles` are rejected on startup; if you use other names for
  resource-scoped roles, add them to `resource_roles` or keep them out of
  the mappings yourself.
* Declare `subject_type` (and, ideally, `role`) as an `enum` in your API
  specification, so that invalid values are rejected with `400` before your
  controller is called; the helpers raise a `ValueError` for unsupported
  subject types, which would otherwise result in `500`.
* Group membership is taken from the claims of each request's token (or user
  info), so a role granted to a group applies as long as the identity
  provider lists the user as a member; removing a user from the group only
  takes effect once their tokens (or cached user info) no longer list it.
* Only use group claims whose values are controlled by the identity provider
  or group managers you trust (e.g., `eduperson_entitlement`), not values
  users may choose themselves.
* Group claim values containing commas or line breaks, or longer than
  allowed for rule values (see [rule values](#rule-values)), cannot be used
  in rules; the helpers raise a `ValueError` for them.

#### Policy types

The helpers manage `g2` rules by default. For models defining resource-scoped
roles under another name, pass it via `ptype` (e.g., `ptype="g3"`). The
helpers raise a `ValueError` if the model does not define that role type with
three fields (subject, role, resource), e.g., with the default model, whose
`g2` groups objects, or if a value is not valid in rules (see [rule
values](#rule-values)).

### Resource ownership

Many apps let users create resources that belong to them, e.g., services in a
registry or tasks in a task execution service: the creator becomes the owner,
owners let others access the resource, and users only see their own
resources, while admins see everything. FOCA's ownership helpers implement
this on top of the [resource role helpers](#resource-scoped-roles), following
the behaviour of [cloud-registry][res-cloud-registry], ELIXIR On-Cloud's
service registry:

* `add_owner(resource)` makes the creator of a resource its owner: the user
  that an allowed service client [acts for](#service-clients), otherwise the
  calling service client, otherwise the calling user. Pass users that service
  clients name in request fields as `on_behalf_of`, so that they are checked
  like the header, and `subject` (and `subject_type`) only to choose the
  owner yourself, as it is not checked against the caller.
* `grant_role(resource, subject, role)` grants a user (or, with
  `subject_type`, a group or service client) a role, replacing the roles it
  had on the resource, and returns the previous ones. The new role is stored
  before the others are removed, so the subject keeps a role throughout
  (e.g., a maintainer promoted to owner); concurrent grants for the same
  subject and resource leave it with one role, that of the grant stored
  last (ordered by a timestamp of the database server in the field
  `granted` of stored rules; rules without it count as the oldest).
  `revoke_roles(resource, subject)` revokes them. Both raise `LastOwner`
  (`409 Conflict`) instead of removing or downgrading the last owner of a
  resource, so that every resource keeps an owner, which may be a group.
* `list_roles(resource)` lists the roles granted on a resource: users first,
  then groups and service clients, as `ResourceRole(subject_type, subject,
  role)`.
* `resource_filter(prefix)` returns a MongoDB filter for listings: all
  resources for admins (`{}`), otherwise those that the calling user or
  service client, or one of its groups, has any role on (e.g., `{"id":
  {"$in": ["1", "2"]}}`), or the user that an allowed service client acts
  for (only the user's direct roles; see the notes below).
  `list_subject_roles(prefix)` lists these roles (e.g., for an endpoint
  telling users which resources they have roles on). Like
  `add_owner()`, both take users named in request fields as `on_behalf_of`.
  Only resources directly below the prefix count, e.g., `/tasks/1` for
  `/tasks`, but not `/tasks/1/logs`. The filter only selects by identifier,
  so if a collection holds resources under several prefixes (e.g., files of
  several projects), combine it with a filter for the scope (e.g.,
  `{"project": "x", **resource_filter("/projects/x/files")}`), also for
  admins.
* `is_admin()` tells whether the caller has the role `admin`, which may do
  everything in the [reference model][resource-role-model]: mapped from its
  groups (`group_mapping`) or its service client (`client_mapping`), or
  granted via `g` rules.

The helpers grant and revoke the roles configured via `resource_roles`
(default: `owner` and `maintainer`; it must contain `owner` and must not
contain `admin`, which is fixed). `grant_role()` rejects other roles with a
`ValueError`, so that users cannot grant, e.g., `admin`, as well as roles
that would not apply: to groups while group claim values are not used as
subjects (`group_mapping` without `keep_group_subjects`), and to clients
that are not service clients. Otherwise, a resource could be left with only
owners that never apply. Roles not in `resource_roles` (e.g., granted via
`add_resource_role()`) are listed, but neither replaced nor revoked by
`revoke_roles()`, which returns `False` if the subject has no roles to
revoke (also for subjects that cannot have roles). Owners whose role does
not apply (e.g., groups granted the role before `keep_group_subjects` was
unset) do not count as owners for the last-owner rule. Every role change is
logged on level `INFO`, naming the resource, the subject, the caller and the
previous roles, for auditing.

For example, an app whose users only see their own tasks:

```yaml
security:
  access_control:
    enabled: True
    model: path/to/resource_role_model.conf
    enforce: True
    claim_groups: eduperson_entitlement
    group_mapping:
      "urn:geant:example.org:group:admins#example.org": admin
    keep_group_subjects: True
    policies:
      - [p, "*", /api/tasks, "(GET)|(POST)"]
      - [p, owner, "/api/tasks/:id", "(GET)|(DELETE)"]
      - [p, maintainer, "/api/tasks/:id", GET]
```

```python
from flask import request

from foca import get_collection
from foca.security.access_control import add_owner, remove_resource, resource_filter


def CreateTask(**kwargs):
    task_id = ...  # create task
    add_owner(f"{request.path}/{task_id}")
    return {"id": task_id}


def ListTasks(**kwargs):
    tasks = get_collection(db="myDb", collection="tasks")
    return {
        "tasks": [{"id": t["id"]} for t in tasks.find(resource_filter(request.path))]
    }


def DeleteTask(id, **kwargs):
    ...  # check that the task exists and may be deleted
    # before deleting, so that nobody can act on the task meanwhile, and
    # after, to revoke roles granted meanwhile
    remove_resource(request.path)
    ...  # delete task
    remove_resource(request.path)
    return {"id": id}
```

The resource paths are full request paths, including the base path of the
API, as policies are enforced on them; the collection path passed to
`resource_filter()` must match the prefix of the paths passed to
`add_owner()`. Revoke all roles on a deleted resource via `remove_resource()`.
Apps that let owners manage access expose endpoints (e.g.,
`PUT /tasks/{id}/permissions/users/{userId}`) calling `grant_role()` and
`revoke_roles()`, or route them to FOCA's [ready-made
controllers](#endpoints-for-managing-roles):

```python
import logging

from connexion.exceptions import BadRequestProblem
from flask import request

from foca import get_collection
from foca.security.access_control import grant_role

logger = logging.getLogger(__name__)


def PutTaskUserRole(id, userId, body, **kwargs):
    resource = request.path.removesuffix(f"/permissions/users/{userId}")
    tasks = get_collection(db="myDb", collection="tasks")
    try:
        grant_role(
            resource,
            userId,
            body["role"],
            exists=lambda: tasks.count_documents({"id": id}) > 0,
        )
    except ValueError as exc:
        # messages may name configuration options, so they are only logged
        logger.info("Role not granted: %s", exc)
        raise BadRequestProblem(detail="Invalid role or user") from exc
    return {"user_id": userId, "role": body["role"]}
```

Such endpoints are enforced by policies such as `p, owner,
/api/tasks/:id/permissions/*, (GET)|(PUT)|(DELETE)`. `LastOwner` and
`NotFound` result in `409` and `404` responses. As for
`add_resource_role()`, which groups a caller may grant roles to is up to the
app; cloud-registry only lets owners grant roles to groups they are members
of (cf. `get_current_group_values()`), and admins to any group.

Keep in mind that:

* Checking for other owners and changing roles is not atomic, as policy
  rules are stored one by one. If concurrent requests remove each other's
  owner roles, each restores the owner role it removed when no owner is left,
  and raises `LastOwner`. Concurrent requests granting a subject different
  roles may leave it with both, and a request failing halfway (e.g., if the
  database becomes unavailable) may leave a subject with fewer roles, or a
  resource without owners; roles are revoked before others are granted, so
  that failures leave fewer rights, not more. Admins can fix such cases.
  Non-owner roles are not granted on resources without owners.
* A resource may be deleted while a role on it is granted, which would leave
  the role behind. Pass a function checking that the resource still exists
  as `exists` to `grant_role()` and `revoke_roles()`: if it returns `False`
  after the role was granted, the role is revoked again, and `NotFound`
  (`404`) is raised. Deleting a resource should revoke its roles both before
  and after deleting it (cf. `DeleteTask()` above), so that roles granted
  meanwhile are revoked, too. If deleting then fails, the resource is left
  without roles, i.e., only admins can access it; so only revoke them once
  the resource is going to be deleted.
* Listings consider roles of the caller itself and of its groups (with
  `keep_group_subjects`, if `group_mapping` is set). A service client acting
  for a user lists the resources of the _user_ instead, but only those the
  user has roles on directly, as service tokens carry no groups of the user.
  Admin rights stay the client's own (`client_mapping`): a client acting for
  an admin does not see everything, and an admin client does, also when
  acting for a user. Access to the listed resources is still enforced as the
  client.
* `resource_filter()` lists the identifiers of all resources the caller has
  roles on in the filter, so it suits apps where users have roles on up to a
  few thousand resources. Like enforcement, each helper call loads all rules
  (cf. [scaling](#scaling)); `grant_role()` and `revoke_roles()` do so
  several times per call.

#### Endpoints for managing roles

Instead of writing controllers like `PutTaskUserRole()` above, apps can route
operations of their API specifications to FOCA's ready-made controllers in
`foca.security.access_control.ownership_server`, under paths of their
choice:

| Controller | Example operation | Response |
| --- | --- | --- |
| `getResourceRoles` | `GET /tasks/{id}/permissions` | roles on the resource, as `subject_type`, `subject` and `role` (cf. `list_roles()`) |
| `putUserRole` | `PUT /tasks/{id}/permissions/users/{userId}` | the granted role, replacing others (cf. `grant_role()`) |
| `deleteUserRole` | `DELETE /tasks/{id}/permissions/users/{userId}` | the user identifier (a JSON string), after revoking their roles (cf. `revoke_roles()`) |
| `putGroupRole` | `PUT /tasks/{id}/permissions/groups/{group}` | as `putUserRole`, for a group |
| `deleteGroupRole` | `DELETE /tasks/{id}/permissions/groups/{group}` | as `deleteUserRole`, for a group |

The controllers take the resource and the subject from the route: the
subject is the last path parameter, and the resource is the request path up to
the path parameter before it (e.g., `/api/tasks/1` for
`/api/tasks/1/permissions/users/alice`), or up to the last path parameter for
`getResourceRoles`. Path parameters may have any names, and resources may be
identified by several (e.g., `/projects/{p}/files/{f}/permissions`). The `PUT`
operations take the role as `{"role": "maintainer"}`; an `enum` of the role in
the specification, as in the example below, rejects other roles before the
controller is called, and should list the roles of `resource_roles`. Keep
users and groups apart in the paths (e.g., `.../users/{userId}` and
`.../groups/{group}`): with users at `.../permissions/{userId}` next to
`.../permissions/groups/{group}`, `PUT .../permissions/groups` would grant a
role to a user called `groups`.

* Resources without owners are not found (`404`), so that roles cannot be
  granted on resources that do not exist.
* The controllers cannot know whether a resource still exists, so a role
  granted while the resource is deleted would be left behind. To revoke it
  again (`404`), route to a wrapper passing an existence check as `exists`
  (cf. `grant_role()`), which also serves as an alias; `exists` (like
  `body`, which holds request bodies) must therefore not be the name of a
  parameter of these operations:

    ```python
    from foca import get_collection
    from foca.security.access_control import ownership_server


    # `id` is the path parameter of the task in the route
    def putTaskUserRole(**kwargs):
        tasks = get_collection(db="myDb", collection="tasks")
        return ownership_server.putUserRole(
            exists=lambda: tasks.count_documents({"id": kwargs["id"]}) > 0,
            **kwargs,
        )
    ```

* Roles not in `resource_roles`, and roles that cannot be granted to the
  subject (cf. `grant_role()`), are rejected with `400`; downgrading or
  removing the last owner with `409`; revoking the roles of a subject
  without any with `404`.
* Callers may only grant roles to groups they are members of (`403`
  otherwise), as they could otherwise hand resources to any group; admins
  may grant roles to any group. Any caller allowed by the policies may revoke
  roles of any group.
* Who may call the operations is decided by your policies, as for any other
  operation; the controllers do not check the caller themselves. So the
  policies must be enforced on these operations: enable `enforce: True`,
  require authentication (security requirements without an empty
  alternative such as `{}`, no `disable_auth`) and do not set
  `x-foca-access-control: false`. Otherwise, any caller could grant
  themselves roles, so apps routing such operations to the controllers (or
  to aliases of them) refuse to start, naming the operations, and wrappers
  of the controllers (and routes added after the app was created) refuse such
  requests with `500`.
* Requests and responses are logged unless `log_traffic` is disabled, as for
  the [permissions API](#securing-the-permissions-api).
* Operation identifiers must be unique, so for several types of resources,
  route each to an alias or wrapper of the controller (e.g.,
  `putFileUserRole = ownership_server.putUserRole` in a module of your
  app).

For example, with the base path `/api`:

```yaml
servers:
  - url: /api
paths:
  /tasks/{id}/permissions:
    parameters:
      - {name: id, in: path, required: true, schema: {type: string}}
    get:
      operationId: foca.security.access_control.ownership_server.getResourceRoles
      responses:
        "200": {description: Roles on the task}
  /tasks/{id}/permissions/users/{userId}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: string}}
      - {name: userId, in: path, required: true, schema: {type: string}}
    put:
      operationId: foca.security.access_control.ownership_server.putUserRole
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [role]
              properties:
                role: {type: string, enum: [owner, maintainer]}
      responses:
        "200": {description: Granted role}
    delete:
      operationId: foca.security.access_control.ownership_server.deleteUserRole
      responses:
        "200": {description: Roles revoked}
  /tasks/{id}/permissions/groups/{group}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: string}}
      # groups may contain slashes
      - {name: group, in: path, required: true, schema: {type: string, format: path}}
    put:
      operationId: foca.security.access_control.ownership_server.putGroupRole
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [role]
              properties:
                role: {type: string, enum: [owner, maintainer]}
      responses:
        "200": {description: Granted role}
    delete:
      operationId: foca.security.access_control.ownership_server.deleteGroupRole
      responses:
        "200": {description: Roles revoked}
```

```yaml
security:
  access_control:
    policies:
      - [p, owner, "/api/tasks/:id/permissions", GET]
      - [p, owner, "/api/tasks/:id/permissions/users/:user", "(PUT)|(DELETE)"]
      # groups may contain slashes
      - [p, owner, "/api/tasks/:id/permissions/groups/*", "(PUT)|(DELETE)"]
```

Apps with further needs (e.g., cloud-registry, which also shows display names
of users) write their own controllers on top of the helpers.

### Securing the permissions API

The [permissions API](#exploring-the-api) requires a bearer token
(`bearerAuth`, validated by `foca.security.auth.validate_token`), and
policies are always enforced on it, irrespective of `enforce`. With the
[reference model][resource-role-model], only subjects with role `admin` can
manage rules. With other models, rules allowing access to the permissions API
routes (e.g., `/admin/access-control/permissions`) are required, naming
prefixed subjects (e.g., `user:alice`) or roles. Initial
rules can be added via [`policies`](#configuring-policies), via
`group_mapping` (e.g., mapping an entitlement to `admin`) or directly to the
access control database.

> If you provide custom `api_specs` for the permissions API, make sure that
> they require authentication; otherwise, all requests will be denied.

### Listing permissions

`GET /permissions` returns permissions page by page, as recommended by the
[GA4GH API pagination guide][res-ga4gh-pagination] (page-based pagination):

```console
GET /admin/access-control/permissions?page_size=2&page=0
```

```json
{
  "results": [
    {"id": "aB3dE9", "policy_type": "p", "rule": {"v0": "*", "v1": "/services", "v2": "POST"}},
    {"id": "Xy7Qr2", "policy_type": "g", "rule": {"v0": "user:bob", "v1": "admin"}}
  ],
  "pagination": {"page": 0, "page_size": 2, "total": 16, "total_pages": 8}
}
```

* `page_size` (default: 100, at most 1000) is the maximum number of
  permissions per page; `page` (default: 0) is the page to return.
* Permissions are returned in the order they were created.
* Requests for pages beyond the last page (except page 0 if there are no
  permissions) or with a `page_size` out of range are rejected with `400`.
* Pages are not guaranteed to be consistent with each other: if permissions
  are created or deleted while paging, permissions may be skipped or returned
  twice.

### Self-service

With `self_service: True`, any authenticated subject may list and get the
permissions that _apply to them_ via `GET /permissions` and
`GET /permissions/{id}`, i.e., rules whose subject (`v0`) is

* the subject's identifier, prefixed with `subject_prefix` (e.g.,
  `user:alice`), including its role assignments (e.g., `g` and `g2` rules),
* one of the subject's roles from group claims (cf. [identity and group
  claims](#identity-and-group-claims)), or
* `*` (any authenticated subject).

Rules granted to roles that are themselves assigned via rules (e.g., rules for
`owner`) are not included. Other permissions are not found (`404`), so that
their identifiers are not disclosed. Subjects allowed by the policies to
access the permissions API (e.g., admins) still get all permissions, and
creating, updating and deleting permissions still requires being allowed by
the policies.

Self-service only lets subjects _see_ their permissions. To let users manage
access to resources, e.g., resource owners granting roles on their resources
to other users, expose endpoints in your app that use the [resource role
helpers](#resource-scoped-roles), as these do not require access to the
permissions API.

Custom permissions API specifications can opt operations in to self-service
via `x-foca-self-service: true`; their controllers must then restrict access
themselves, e.g., via `foca.security.access_control.is_permitted()`.

### Rule values

Rules are validated whenever they are written, i.e., via the permissions API,
the resource role helpers or the Casbin adapter (against the configured
model):

* The policy type (`policy_type`) must be defined in the model (e.g., `p`,
  `g` or `g2`), and the rule must have exactly as many values as the policy
  type's definition has fields, given as `v0`, `v1`, ... in the permissions
  API. Other keys are rejected.
* Values must be non-empty strings of at most 1024 characters, must not
  contain commas or line breaks, and must not start with `#`.
* Values of `act` fields of policy definitions (e.g., `p = sub, obj, act`)
  must be valid regular expressions.

The permissions API responds with `400` to invalid rules. Requests to
enforced operations whose subject is not a valid value (e.g., an identity
claim containing a comma) are denied with `403`. Stored rules that are not
valid (e.g., added to the database directly) are skipped with a warning when
policies are loaded.

## Scaling

All stored rules are loaded on every enforced request (and for the
permissions API and self-service), so that changes apply immediately and
consistently across app instances and workers. Loading takes time linear in
the number of stored rules, so the cost of each request grows linearly with
it. As resource-scoped roles add rules per created resource (e.g., one `g2`
rule for its owner), apps that let users create many resources should
consider limiting this, e.g., via quotas or rate limits on the endpoints
creating them.

The [resource ownership](#resource-ownership) helpers load all rules on each
call too, and those changing roles load them again after the change, to
notice concurrent changes (e.g., owners removing each other, or the resource
being deleted meanwhile). So each request to the [endpoints for managing
roles](#endpoints-for-managing-roles) loads all rules several times,
including the enforcement of the request (more often for `PUT` and `DELETE`
than for `GET`), which matters more than for other operations once there are
many rules.

## Exploring the API

If `api_swagger_ui` is set, you can explore the access control API endpoints
via a [Swagger UI][res-swagger] in your browser once your app is up and
running, e.g.:

```bash
firefox http://<host>/admin/access-control/ui/  # or use your browser of choice
```

> The exact route at which endpoints are served depends on the `servers`
> (or `basePath`) of the permissions API specification (cf. `api_specs`).

[casbin-docs]: <https://casbin.org/docs/en/how-it-works>
[docs-models-access-control]: configuration/access-control.md
[default-specs]: <https://github.com/elixir-cloud-aai/foca/blob/dev/foca/security/access_control/api/access-control-specs.yaml>
[default-controllers]: <https://github.com/elixir-cloud-aai/foca/blob/dev/foca/security/access_control/access_control_server.py>
[default-model]: <https://github.com/elixir-cloud-aai/foca/blob/dev/foca/security/access_control/api/default_model.conf>
[resource-role-model]: <https://github.com/elixir-cloud-aai/foca/blob/dev/foca/security/access_control/api/resource_role_model.conf>
[img-casbin-flow]: images/casbin_model.jpeg
[res-cloud-registry]: <https://github.com/elixir-cloud-aai/cloud-registry>
[res-casbin-models]: <https://casbin.org/docs/en/supported-models>
[res-foca]: <https://pypi.org/project/foca/>
[res-ga4gh-pagination]: <https://github.com/ga4gh/TASC/blob/main/recommendations/API%20pagination%20guide.md>
[res-openapi]: <https://www.openapis.org/>
[res-swagger]: <https://swagger.io/tools/swagger-ui/>
