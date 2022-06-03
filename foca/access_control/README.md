# FOCA-Access Control

## Description

This document summarises the complete flow for understanding and
configuring access control using [FOCA][res-foca].

## Table of Contents

* [Description](#description)
* [Why do we need it?](#why-do-we-need-it)
* [How it works?](#how-it-works)
  * [PERM Model](#perm-model)
    * [Policy](#policy)
    * [Effect](#effect)
    * [Request](#request)
    * [Matchers](#matchers)
* [Configuration](#configuration)
  * [Configuring Database](#configuring-database)
  * [Configuring API Specs](#configuring-api-specifications)
  * [Configuring User Properties](#configuring-user-properties)
  * [Configuring Models](#configuring-models)
* [Contributing](#contributing)
* [Versioning](#versioning)
* [License](#license)
* [Contact](#contact)

## Why do we need it?

This feature has been added in order to create secure applications. One may
need to restrict the API usage on the basis of specific users or perhaps create
user groups with access to specific functionalities. For eg. Person A might
read, B can read/write while C cannot access the same resource. Hence to combat
the same, [FOCA][res-foca] provides configurable access control. This has been
implemented the support provided [Casbin][casbin-docs].

## How it works?

Access control model is abstracted into a CONF file based on the
**PERM metamodel (Policy, Effect, Request, Matchers)**. One can provide the
required model definition as per one's usecase. Further, policies can be
defined on the basis of the PERM model which are used to validate requests.
For storing these policies various adaptors can be used. FOCA uses mongo db
collection to store these user specific policies. 

![Casbin Flow][img-casbin-flow]

### PERM Model

The PERM model is composed of four foundations (Policy, Effect, Request,
Matchers) describing the relationship between resources and users.

#### Policy

Here we define the policies like *who can do what* and *who has what*
*permissions*. It can be understood as a model in which a logical combination judgment is performed again on the matching results of Matchers.

For example: e = some(where(p.eft == allow))

This sentence means that if the matching strategy result p.eft has the result of (some) allow, then the final result is true. Basic Syntax for Policy is:
```
p = sub, obj, act, eft
```

This syntax can be read as who(**sub**) can/cannot(**allow/deny**) do
what(**act**) on some resource(**obj**).

Here *eft* can be either allow or deny. If it is not included, the default
value is allow.

In the [above](#how-it-works) diagram as per policies defined:
1. John has permission to read RECORD1
2. John has no permission to write RECORD1
3. Harry has permission to read RECORD1
4. Harry has permission to write RECORD1

#### Effect

It can be understood as a model in which a logical combination judgment is
performed again on the matching results of Matchers.

For example:
```
e = some(where(p.eft == allow))
```

This sentence means that if the matching strategy result p.eft has the result of (some) allow, then the final result is true

#### Request

Define the request parameters. A basic request is a tuple object, requiring at
least a subject (accessed entity), object (accessed resource) and action (access
method).

For instance, a request definition may look like this: 
```
r = {sub , obj, act}
```

It actually defines the parameter name and order which we should provide for
access control matching function. In the above diagram as per the incoming
request:
1. John wants to read RECORD1
2. John wants to write on RECORD1

#### Matchers

Matching rules of Request (r) and Policy (p).

For example: 
```
m = r.sub == p.sub && r.act == p.act && r.obj == p.obj
```

This simple and common matching rule means that if the requested parameters
(entities, resources, and methods) are equal, that is, if they can be found in
the policy, then the policy result (p.eft) is returned. The result of the
strategy will be saved in p.eft.

## Configuration

FOCA allows you to restrict your application configuration using access
control. To modify defaults, include the top-level `access_control`
keyword section in your app configuration file:

```yaml
access_control:
  api_specs: 'path/to/your/access/control/specs'
  api_controllers: 'path/to/your/access/control/spec/controllers'
  db_name: access_control_db_name
  collection_name: access_control_collection_name
  model: access_control_model_definition
  owner_headers: admin_identification_properties
  user_headers: user_identification_properties
```

> This config would create an application with access control defined as per
> `model` provided. The corresponding permission models could be accessed and
> altered by the user with admin permissions on the swagger panel specified
> under api specs.
>  
> Cf. the [API model][docs-models-access-control] for further options and details.

### Configuring Database

You can set up a default database and collection by providing `db_name` and
`collection_name` as a part of `access_control`. This would create a separate
database and collection as a part of your application which would manage the
resource permissions. Defualts are set to `access_control_db` and
`permission_rules` respectively.

### Configuring API Specifications

FOCA enables requirement specific design for your application's access control
configuration. You can specify any [OpenAPI][res-openapi] specifications
consumed as part of your application which validates requests/responses. FOCA
supports multiple specification files (versions Swagger/OpenAPI 2.x, OpenAPI
3.x and mixed) and multiple fragments thereof, and it adds additional features
that allow easy modification of specifications on the fly. In particular, links
to routers and security definitions can be added to each specified endpoint.

Under the `access_control` section, you can specify, specifications and
corresponding controllers under `api_specs` and `api_controllers` respectively.
To give furter context, these controllers will provide support for user to
access control related data points via APIs.
If not provided, the defaults will be utilised for setting up the the access
control configuration. 
* [Default Specifications][default-specs]
* [Default Controller Definitions][default-controllers]

### Configuring User Properties

For access control to work, we must provide specific user properties for every
request. These can be used to validate against in order to provide resource
permissions and differentiate between owners and users. You can set up
`owner_headers` and `user_headers` to specify owner and user properties.

### Configuring Models

Access control works on the basis of model definitions. If not provided,
default [RBAC Model Definition][default-model] will be used. For information
on how to write policy definitions Cf. [Model Types][res-casbin-models].

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contributing
guidelines][res-elixir-cloud-contributing] if you are interested to contribute.
Please mind the [code of conduct][res-elixir-cloud-coc] for all interactions
with the community.

## Versioning

The project adopts [semantic versioning][res-semver]. Currently the service
is in beta stage, so the API may change without further notice.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][org-elixir-cloud]. Follow the link to get in touch with us via chat or
email. Please mention the name of this service for any inquiry, proposal,
question etc.

![Logo_banner][img-logo-banner]

[casbin-docs]: <https://casbin.org/docs/en/how-it-works>
[default-specs]: api/access-control-specs.yaml
[default-controllers]: ./access_control_server.py
[default-model]: api/default_model.conf
[img-casbin-flow]: ../../images/casbin_model.jpeg
[img-hint]: ../../images/hint.svg
[img-logo-banner]: ../../images/logo-banner.svg
[license]: ../../LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[org-elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[res-casbin-models]: <https://casbin.org/docs/en/supported-models>
[res-elixir-cloud-coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[res-elixir-cloud-contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[res-foca]: <https://pypi.org/project/foca/>
[res-openapi]: <https://www.openapis.org/>
[res-semver]: <https://semver.org/>
[res-swagger]: <https://swagger.io/tools/swagger-ui/>