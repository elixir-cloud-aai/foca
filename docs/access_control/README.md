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

You can tweak the default access control behavior by setting a number of
configuration parameters:

```yaml
security:
  access_control:
    model: access_control_model_definition
    api_specs: 'path/to/your/access/control/specs'
    api_controllers: 'path/to/your/access/control/spec/controllers'
    api_route: '/path/to/access_control_api'
    db_name: access_control_db_name
    collection_name: access_control_collection_name
    owner_headers: admin_identification_properties
    user_headers: user_identification_properties
```

> The application created with this config would provide an access control
> model `model`. Corresponding permissions could be accessed and altered
> by a user with admin permissions via the dedicated endpoints defined in the
> `api_specs`, operationalized by the controllers in `api_controllers` and
> hosted at `api_route`. Permissions will be stored in collection
> `collection_name` of a dedicated MongoDB database `db_name`. The headers
> `owner_headers` and `user_headers` would be set for admins and regular users,
> respectively.
>  
> Cf. the [API model][docs-models-access-control] for further options and details.

### Configuring the database

FOCA sets up a MongoDB database and collection to manage permission resources.
Default names are set to `access_control_db` and `permission_rules`,
respectively. To manually set the names, provide values for the `db_name` and
`collection_name` parameters, respectively.

### Configuring the API

FOCA comes with a default API for configuring permission resources. However,
you can provide a custom [Swagger 2.x or OpenAPI 3.x][res-openapi] definition
for `/permissions` endpoints via `api_specs`, provide a module containing
custom controllers for the defined endpoints via the `api_controllers`
parameter and tell FOCA where to host the endpoints via `api_route`.

The specs and controllers used by default can be accessed via the links below:

* [Default Specifications][default-specs]
* [Default Controller Definitions][default-controllers]

### Configuring the access control model

Access control works on the basis of model definitions. If a custom model
definition is not provided via parameter `model`, a [role-based access control
(RBAC) model definition][default-model] will be used by default. To learn more
on writing your own custom models, refer to the [Casbin
documentation][res-casbin-models].

### Configuring header properties

For access control to work, we must provide specific properties for every
request. These are used to validate the incoming request and to differentiate
users and owners/admins. You can set up `owner_headers` and `user_headers` to
specify custom owner and user properties, respectively.

## Exploring the API

Once your app is up and running, you can explore the access control API
endpoints via a [Swagger UI][res-swagger] in your browser, e.g.:

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
[img-casbin-flow]: ../../images/casbin_model.jpeg
[img-logo-banner]: ../../images/logo-banner.svg
[res-casbin-models]: <https://casbin.org/docs/en/supported-models>
[res-foca]: <https://pypi.org/project/foca/>
[res-openapi]: <https://www.openapis.org/>
[res-swagger]: <https://swagger.io/tools/swagger-ui/>
