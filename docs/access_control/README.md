# Access Control in FOCA

## Description

This document summarizes how access control works in [FOCA][res-foca], and how
it can be configured for your needs.

## Table of Contents

* [Description](#description)
* [Why do we need it?](#why-do-we-need-it)
* [How does it work?](#how-does-it-work)
  * [PERM Model](#perm-model)
    * [Policy](#policy)
    * [Effect](#effect)
    * [Request](#request)
    * [Matchers](#matchers)
* [Configuration](#configuration)
  * [Configuring the database](#configuring-the-database)
  * [Configuring the API](#configuring-the-api)
  * [Configuring header properties](#configuring-header-properties)
  * [Configuring the access control
    model](#configuring-the-access-control-model)
* [Enforcing policies with OIDC tokens](#enforcing-policies-with-oidc-tokens)
  * [Identity and group claims](#identity-and-group-claims)
  * [Resource-scoped roles](#resource-scoped-roles)
  * [Securing the permissions API](#securing-the-permissions-api)
  * [Listing permissions](#listing-permissions)
  * [Self-service](#self-service)
  * [Rule values](#rule-values)
* [Exploring the API](#exploring-the-api)

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
  access_control:
    enabled: True
    model: access_control_model_definition
    api_specs: 'path/to/your/access/control/specs'
    api_controllers: 'path/to/your/access/control/spec/controllers'
    api_route: '/path/to/access_control_api'
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
```

> The application created with this config would provide an access control
> model `model`. Corresponding permissions could be accessed and altered
> by a user with admin permissions via the dedicated endpoints defined in the
> `api_specs`, operationalized by the controllers in `api_controllers` and
> hosted at `api_route`. Permissions will be stored in collection
> `collection_name` of a dedicated MongoDB database `db_name`.
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

FOCA comes with a default API for configuring permission resources. However,
you can provide a custom [Swagger 2.x or OpenAPI 3.x][res-openapi] definition
for `/permissions` endpoints via `api_specs`, provide a module containing
custom controllers for the defined endpoints via the `api_controllers`
parameter and tell FOCA where to host the endpoints via `api_route`.

The Swagger UI and the specification of the permissions API are not served
by default. Set `api_swagger_ui: True` and/or `api_serve_spec: True` to serve
them.

Rules created via `POST /permissions` get a random identifier of `id_length`
characters (4 to 64, default: 6) drawn from `id_charset` (letters, digits,
`-`, `.`, `_` and `~`; default: letters and digits). Identifiers are unique:
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
`group_mapping` is set, claim values are mapped to roles, and values without
mapping are ignored. Access is granted if any of the subjects is allowed.
This way, no rules are needed per user for group-based access, e.g., for
administrators.

To keep users, groups and roles apart, subjects are namespaced in rules:

| Subject | In rules | Example |
| --- | --- | --- |
| User identifier | prefixed with `subject_prefix` (default: `user:`) | `user:alice` |
| Group claim value without `group_mapping` | prefixed with `group:` | `group:vo-members` |
| Role (from `group_mapping` or `g` rules) | as is | `admin` |

This way, a user whose identifier equals a role name (e.g., `admin`), or an
identity provider group of that name, is not granted that role. For the same
reason, `subject_prefix` must not be empty and must not overlap with
`group:`. All rules, including those for the permissions API, must use these
forms (e.g., `p, user:alice, /admin/access-control/permissions, GET`); rules
naming unprefixed user identifiers do not match.

In your controllers, use `get_current_subject()` and `get_current_groups()`
to get the subject (without prefix) and roles of the current request:

```python
from foca.security.access_control import (
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
add `subject_prefix` to the rules themselves.

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
rules can be added via `group_mapping` (e.g., mapping an entitlement to
`admin`) or directly to the access control database.

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

## Exploring the API

If `api_swagger_ui` is set, you can explore the access control API endpoints
via a [Swagger UI][res-swagger] in your browser once your app is up and
running, e.g.:

```bash
firefox http://<host>/admin/access-control/ui/  # or use your browser of choice
```

> The exact route at which endpoints are served depends on the value of
> `AccessControlConfig.api_route`.

![Logo_banner][img-logo-banner]

[casbin-docs]: <https://casbin.org/docs/en/how-it-works>
[docs-models-access-control]: <https://foca.readthedocs.io/en/latest/modules/foca.models.html#foca.models.config.AccessControlConfig>
[default-specs]: ../../foca/security/access_control/api/access-control-specs.yaml
[default-controllers]: ../../foca/security/access_control/access_control_server.py
[default-model]: ../../foca/security/access_control/api/default_model.conf
[resource-role-model]: ../../foca/security/access_control/api/resource_role_model.conf
[img-casbin-flow]: ../../images/casbin_model.jpeg
[img-logo-banner]: ../../images/logo-banner.svg
[res-casbin-models]: <https://casbin.org/docs/en/supported-models>
[res-foca]: <https://pypi.org/project/foca/>
[res-ga4gh-pagination]: <https://github.com/ga4gh/TASC/blob/main/recommendations/API%20pagination%20guide.md>
[res-openapi]: <https://www.openapis.org/>
[res-swagger]: <https://swagger.io/tools/swagger-ui/>
