# `api`

OpenAPI specifications of the app. FOCA registers them with
[Connexion](https://github.com/spec-first/connexion), which routes requests to
controllers and validates requests and responses. FOCA supports multiple
specifications (Swagger/OpenAPI 2.x and OpenAPI 3.x), specifications merged
from several files, and modifications on the fly.

```yaml
api:
  specs:
    - path:
        - specs/openapi.yaml
        - specs/additions.yaml
      append:
        - security:
            - bearerAuth: []
      add_operation_fields:
        x-openapi-router-controller: my_app.controllers
      add_security_fields:
        x-bearerInfoFunc: foca.security.auth.validate_token
      disable_auth: false
      connexion:
        strict_validation: true
        validate_responses: true
        swagger_ui: true
        serve_spec: true
    - path: specs/other_openapi.yaml
```

In this example, the first specification is merged from two files (later
files update earlier ones). Root fields are added via `append` (here: a
global security requirement), the controller module is added to every
operation via `add_operation_fields` (operations that set the field
themselves keep their value, e.g., to use another controller module), and
FOCA's
[token validation](../authentication.md) is added to every security scheme via
`add_security_fields`. `disable_auth: true` would remove all security
requirements and schemes instead, e.g., for local development.

Options for registering a specification with Connexion are set via
`connexion`, e.g., whether to validate requests strictly and responses at all,
and whether to serve the Swagger UI and the specification itself (both are
served by default). The Swagger UI needs the specification, so with
`serve_spec: false`, the UI is not served either, and `swagger_ui: true`
together with `serve_spec: false` is rejected. Options not listed below are
passed through to Connexion's `add_api()` as they are.

Which operations require authentication is defined by the security
requirements of the operations in the specifications; FOCA's access control
can additionally [enforce policies](../access-control.md#enforcing-policies-with-oidc-tokens)
on them.

## Named specifications

`specs` can also be given as a mapping of names to specifications. Config
overlays can then change a single specification by its name, without
restating the others or the options they do not change (see [named
entries](index.md#named-entries)):

```yaml
api:
  specs:
    my-api:
      path: specs/openapi.yaml
      add_operation_fields:
        x-openapi-router-controller: my_app.controllers
    other-api:
      path: specs/other_openapi.yaml
```

Names appear in the logs and must be unique; in the list form, specifications
may set `name` themselves. Apps that expect operators to adapt their
specifications should use named specifications.

## Patching specifications

!!! danger "With great power comes great responsibility"
    Patches can change everything in a specification, including its security
    requirements: a single line can make an operation public or exempt it
    from access control. Review patches like code, keep them as small as
    possible, and check the startup logs for the warnings described below.

`patches` lists patches that are deep-merged into a specification, in order,
after the files of `path` and the root fields of `append`, and before
`add_operation_fields` and `add_security_fields`, with the same rules as the
files of `path`: mappings are merged, while lists and other values replace
earlier ones. Each patch is a YAML or JSON file, or a mapping given inline.
Apps leave `patches` unset; it is meant for operators, who set it in a config
overlay for a [named specification](#named-specifications):

```yaml title="overlay.yaml"
api:
  specs:
    my-api:
      patches:
        - /etc/my-app/patches/openapi.yaml
        - info:
            description: Deployment of My App at Example Org
```

Relative paths of patch files are resolved like those of `path` (against the
directory of the base configuration, see [relative
paths](index.md#relative-paths)), so overlays usually refer to mounted files
with absolute paths, or give patches inline.

FOCA fails to start if a patch file cannot be read or is not a mapping, or if
the patched specification is invalid. It logs the patches applied to each
specification, and a warning listing the operations that required
authentication (non-empty `security` requirements, of the operation or the
specification, without an empty alternative) or were subject to [access
control](../access-control.md#enforcing-policies-with-oidc-tokens) before the
patches, but not after (e.g., because a patch sets `security: []` or
`x-foca-access-control: false`).

See [requiring login for reads](../access-control.md#requiring-login-for-reads)
for a recipe that combines a patch with named policy rules.

::: foca.models.config.APIConfig
    options:
      members: false

::: foca.models.config.SpecConfig
    options:
      members: false

::: foca.models.config.ConnexionOptions
    options:
      members: false
