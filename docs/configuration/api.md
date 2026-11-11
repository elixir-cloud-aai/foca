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
files update earlier ones, see [merging specification
files](#merging-specification-files)). Root fields are added via `append`
(here: a global security requirement), the controller module is added to every
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

## Merging specification files

If `path` lists several files, each file is deep-merged into the result of
the files before it: mappings are merged, while lists and other values
replace earlier ones. Apps use this to add their own operations, schemas or
extensions to a vendored specification (e.g., of a GA4GH API) without
changing the vendored file.

By default, `null` is a value like any other: `default: null` in a later file
sets the default to `null`. With `merge_null_removes: true`, `null` in the
second and later files removes the key instead, at any depth, as in
[patches](#patching-specifications) (JSON Merge Patch,
[RFC 7396](https://www.rfc-editor.org/rfc/rfc7396)). Apps need this to
change a vendored specification where a change has to remove a key, e.g., to
turn an API key scheme into an HTTP bearer scheme (Connexion rejects the
`name` and `in` that the merged scheme would otherwise keep) or to replace a
reference to another file or URL by a path item of their own (Connexion
fetches references to URLs on every start):

```yaml title="config.yaml"
api:
  specs:
    my-api:
      path:
        - api/vendored.openapi.yaml
        - api/additions.openapi.yaml
      merge_null_removes: true
```

```yaml title="api/additions.openapi.yaml"
components:
  securitySchemes:
    BEARER:
      type: http
      scheme: bearer
      name: null
      in: null
paths:
  /service-info:
    $ref: null
    get:
      operationId: getServiceInfo
      security: []
      responses:
        "200":
          description: Service info
```

With the option set, later files can't set values to `null`: a `null` in
them, also in mappings they add (e.g., `default: null` in a new schema),
removes the key. Values that must be `null` go into the first file, or the
option stays off. `null` in the first file is kept either way.

## Patching specifications

!!! danger "With great power comes great responsibility"
    Patches can change everything in a specification, including its security
    requirements: a single line can make an operation public or exempt it
    from access control. Review patches like code, keep them as small as
    possible, and check the startup logs for the warnings described below.

`patches` lists patches that are deep-merged into a specification, in order,
after the files of `path` and the root fields of `append`, and before
`add_operation_fields` and `add_security_fields`, as a JSON Merge Patch
([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396)): mappings are merged,
lists and other values replace earlier ones, and `null` removes a key, at any
depth (keys that are not set are ignored). Patches can't set values to
`null`; a `null` in a mapping that a patch adds is dropped, too. Each patch
is a YAML or JSON file, or a mapping given inline.
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

FOCA fails to start if a patch file cannot be read or is not a mapping, if
the patched specification is invalid, or if patches set or remove (`null`)
fields of a path item defined via `$ref` (Connexion replaces them with the
fields of the referenced object, so they would silently not take effect;
patch the referenced object instead, or remove the reference with
`$ref: null` and define the path item in the patch, see [merging
specification files](#merging-specification-files) for an example). Other
errors registering a patched specification are logged with a hint to the
patches. FOCA logs the patches applied to each specification, and warns of

- operations that required authentication (non-empty `security`
  requirements, of the operation or the specification, without an empty
  alternative) or were subject to [access
  control](../access-control.md#enforcing-policies-with-oidc-tokens) before
  the patches, but not after (e.g., because a patch sets `security: []` or
  `x-foca-access-control: false`, or removes `security` with `null`),
- operations added by patches (also by renaming paths, via local references
  or by replacing a reference with a path item) that do not require
  authentication or opt out of access control, and path items that patches refer to other files (which FOCA
  cannot check), and
- patches that change (also remove) security definitions or schemes.

Protections are compared after `add_operation_fields` (applied before and
after the patches for the comparison), and with path items given via local
references resolved, as Connexion does. The warnings are a safety net, not a
review: check what a patch does before deploying it.

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

## Stricter request checks

Beyond the checks of Connexion against the specification, FOCA refuses these with
`400` (see `foca.api.validation`):

- **Unpaired surrogates** in JSON bodies, from escapes such as `\ud800`, also in keys
  and nested strings: they can't be encoded for storage. As with Connexion's checks of
  bodies, only for operations with a schema for their body and only JSON media types.
  A custom `body` validator in `validator_map` takes this check over.
- **Invalid values for the JSON Schema formats** `date-time` (RFC 3339), `date`
  (`YYYY-MM-DD`) and `uri` (lenient: any scheme, without whitespace), in bodies and
  parameters, as well as in responses with `validate_responses`.
- **Query parameters of operations without parameters**, with `strict_validation`,
  except those holding API keys. Operations with parameters refuse others already.

Error responses don't name the field that failed yet.
