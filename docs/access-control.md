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
by default. Set `api_swagger_ui: True` and/or `api_serve_spec: True` to serve
them.

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
makes the stored configured rules match the configuration: missing rules are
added (or, if stored already, e.g., via the permissions API, marked as
configured), and configured rules that were removed from the configuration,
as well as duplicates, are removed. The configuration is the only place to
change configured rules: the permissions API responds with `409` to requests
to change or delete them, and the [resource role
helpers](#resource-scoped-roles) keep them, with a warning. Other stored
rules are kept. Without `policies`, stored rules are not changed on startup;
with `policies: []`, all configured rules are removed.

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
administrators.

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
| Role (from `group_mapping` or `g` rules) | as is | `admin` |

This way, a user whose identifier equals a role name (e.g., `admin`), or an
identity provider group of that name, is not granted that role. For the same
reason, `subject_prefix` must not be empty and must not overlap with
`group:`. All rules, including those for the permissions API, must use these
forms (e.g., `p, user:alice, /admin/access-control/permissions, GET`); rules
naming unprefixed user identifiers do not match.

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
`subject_prefix` or `group:`: as Casbin resolves roles transitively,
granting, e.g., the "role" `group:auditors` to a user would otherwise give
the user all rights of that group. `remove_resource_role()` accepts both, so
that existing rules with them can be revoked; it removes all stored copies of
a rule, and `list_resource_roles()` returns each subject and role pair
once, also if a rule was stored more than once (e.g., by concurrent
requests).

`remove_resource(resource)` revokes the roles granted on exactly `resource`,
not those granted on its sub-paths (e.g., `/services/123/files` for
`/services/123`) or on the path with a trailing slash; revoke these
separately, e.g., via `list_resource_roles()` and `remove_resource_role()`.
Neither helper removes rules set via [`policies`](#configuring-policies), as
they would be added again on the next start: `remove_resource_role()` then
returns `False`, and both helpers log a warning. Remove such rules from the
configuration instead. `remove_resource()` returns `True` if it revoked any
role, i.e., if it removed a rule that is not also configured; removing only
stored copies of configured rules returns `False`, as their roles still
apply.

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
* Only map group claim values via `group_mapping` to global roles such as
  `admin`, not to names of resource-scoped roles such as `owner` or
  `maintainer`: a mapped role is a subject of its own, which matches the `p`
  rules of that role on _every_ resource (e.g., all members of the group
  would own all services).
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
[res-casbin-models]: <https://casbin.org/docs/en/supported-models>
[res-foca]: <https://pypi.org/project/foca/>
[res-ga4gh-pagination]: <https://github.com/ga4gh/TASC/blob/main/recommendations/API%20pagination%20guide.md>
[res-openapi]: <https://www.openapis.org/>
[res-swagger]: <https://swagger.io/tools/swagger-ui/>
