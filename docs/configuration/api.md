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
and whether to serve the Swagger UI and the specification itself. Options not
listed below are passed through to Connexion's `add_api()` as they are.

Which operations require authentication is defined by the security
requirements of the operations in the specifications; FOCA's access control
can additionally [enforce policies](../access-control.md#enforcing-policies-with-oidc-tokens)
on them.

::: foca.models.config.APIConfig
    options:
      members: false

::: foca.models.config.SpecConfig
    options:
      members: false

::: foca.models.config.ConnexionOptions
    options:
      members: false
