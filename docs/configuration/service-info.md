# `service_info`

Service info as per the
[GA4GH service-info specification](https://github.com/ga4gh-discovery/ga4gh-service-info)
(version 1.0.0), served at `GET /service-info` from the configuration, without
a database and without code of your own:

```yaml
service_info:
  package: my-app
  runtime_fields: my_app.service_info.runtime_fields
  service:
    id: org.example.my-service
    name: My service
    type:
      group: org.ga4gh
      artifact: wes
      version: 1.1.0
    description: Runs workflows.
    organization:
      name: Example
      url: https://example.org
    contactUrl: mailto:support@example.org
    documentationUrl: https://docs.example.org
    environment: prod
    # Optional; served only if set
    createdAt: "2026-10-20T00:00:00Z"
    updatedAt: "2026-10-20T00:00:00Z"
```

`service` is the service info itself: `id`, `name`, `type`, `organization`
and `version` are required, the other fields of the GA4GH `Service` schema
are optional and only served if set (including `createdAt` and `updatedAt`,
which must be [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) date-times),
and any other fields (e.g., `x-logo`) are served as they are.

## Serving the service info

Route the `GET /service-info` operation of your API specification to FOCA's
controller, `foca.api.service_info.getServiceInfo`:

```yaml title="api/openapi.yaml"
paths:
  /service-info:
    get:
      operationId: getServiceInfo
      x-openapi-router-controller: foca.api.service_info
      security: []
      responses:
        '200':
          description: Service info
          content:
            application/json:
              schema:
                # Or the GA4GH `Service` schema
                type: object
```

The controller set on the operation takes precedence over one added to all
operations via [`add_operation_fields`](api.md). Without such a field, a full
`operationId: foca.api.service_info.getServiceInfo` works as well. Most GA4GH
API specifications already define the operation (with `operationId:
getServiceInfo`), so it is enough to add `x-openapi-router-controller`, e.g.,
in a [merged file](api.md). `security: []` makes the service info public;
leave it out if your specification requires authentication for it.

## Version

`version` is the version of the deployed app. Set `package` to the name of
your app's distribution package (as installed, e.g., with `pip install`, and
as in the `name` of its `pyproject.toml`) to serve its version, unless
`service.version` is set. The package must then be installed; otherwise the
app refuses to start.

## Fields computed at runtime

For fields that are only known at runtime (e.g., from the environment, or
from other services), set `runtime_fields` to the dotted path of a function
that returns them as a dictionary:

```python title="my_app/service_info.py"
import os


def runtime_fields() -> dict:
    return {"environment": os.environ.get("DEPLOYMENT", "dev")}
```

The function is called without arguments, in an app context (e.g., with
access to [`get_config()`](index.md#accessing-the-configuration)), on startup
and for each request; keep it fast. Fields are taken, in order of
precedence, from `service`, the function and the package version. Fields and
list items set to `null` (e.g., in a [config
overlay](index.md#environment-specific-configuration)) are removed, also if
the function or the package set them.

## Validation

The service info is validated against the `Service` schema of the GA4GH
service-info specification, version 1.0.0, which is bundled with FOCA:

- When the configuration is validated, the configured fields (and the
  package version) raise a `pydantic.ValidationError` naming the first
  invalid field, e.g., ``Invalid service info in
  `service_info.service.organization.url`: 'example.org' is not a 'uri'``.
  Required fields may be missing if `runtime_fields` is set.
- When the app is created (`Foca.create_app()`), the complete service info,
  including the fields computed at runtime, raises a
  `foca.api.service_info.InvalidServiceInfo` (a `ValueError`) if invalid, so
  the app refuses to start.
- On each request, service info that has become invalid (via the function)
  is not served; the request fails with `500 Internal Server Error`, and the
  error is logged.

Use `foca.api.service_info.get_service_info()` to get the service info of the
current app in your own code.

## Migrating an app

Apps that implemented the service info themselves, e.g., with
`POST /service-info` storing it in a database, can serve it from the
configuration instead:

1. Move the service info into `service_info.service` of the configuration
   (e.g., from a `custom` section or the database). Set `package` instead of
   `service.version` to serve the app's version, and move code computing
   fields into a `runtime_fields` function.
2. Route `GET /service-info` to `foca.api.service_info.getServiceInfo` (cf.
   [above](#serving-the-service-info)), and remove the `POST /service-info`
   operation from your API specification.
3. Remove your controllers for `GET` and `POST /service-info`, any code
   checking or storing the service info, and its database collection from
   `db` (drop the collection from existing databases if you like; stored
   service info is no longer served).
4. Start the app: invalid service info now fails startup, naming the invalid
   field. To change the service info, change the configuration and restart
   the app.

::: foca.models.config.ServiceInfoConfig
    options:
      members: false
