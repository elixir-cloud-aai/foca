# Migrating to 0.15.0

FOCA 0.15.0 serves GA4GH service info from the configuration, lets apps grant
resource-scoped roles to groups, and makes the media type of error responses
configurable. Most apps need no changes; apps with a `service_info` section
and apps using the resource role helpers should check the breaking changes
below. Upgrading from 0.13 or earlier? Start with [Migrating to
0.14.0](migration-0.14.md).

## Breaking changes

### Service info

`service_info` is a FOCA config section for serving GA4GH service info
([details](configuration/service-info.md)), no longer a free-form section
left to apps. Move service info fields kept directly under `service_info` to
`service_info.service`, or rename the section if your app uses it for
anything else; otherwise, the configuration fails validation on startup:

=== "0.14"

    ```yaml
    service_info:
      id: org.example.my-service
      name: My service
      type:
        group: org.ga4gh
        artifact: wes
        version: 1.1.0
      organization:
        name: Example
        url: https://example.org
      version: 1.0.0
    ```

=== "0.15"

    ```yaml
    service_info:
      service:
        id: org.example.my-service
        name: My service
        type:
          group: org.ga4gh
          artifact: wes
          version: 1.1.0
        organization:
          name: Example
          url: https://example.org
        version: 1.0.0
    ```

- `get_config().service_info` is a `ServiceInfoConfig` object, not a
  dictionary; controllers returning it directly fail with "not JSON
  serializable". Route `GET /service-info` to FOCA's controller
  `foca.api.service_info.getServiceInfo` instead, and remove your own
  ([details](configuration/service-info.md#serving-the-service-info)), or
  call `foca.api.service_info.get_service_info()` for the complete service
  info. `get_config().service_info.service` holds only the configured fields.
- The service info is validated against the GA4GH `Service` schema (version
  1.0.0): `id`, `name`, `type`, `organization` and `version` are required,
  and URLs and date-times must be valid. The app fails to start if the
  service info is invalid, if `package` is needed for the version but not
  installed, if the `runtime_fields` function cannot be imported, or if an
  operation is routed to `getServiceInfo` without `service_info`
  ([details](configuration/service-info.md#validation)).

### Resource role helpers

- `add_resource_role()` raises a `ValueError` for empty subjects and for
  roles starting with `subject_prefix` (default: `user:`) or `group:`, as
  such roles would give the subject all rights of the subject they name.
  `remove_resource_role()` still accepts them, so that existing rules can be
  revoked. When an app instance first loads policies, FOCA logs a warning for
  each stored rule whose role is a subject
  ([details](access-control.md#granting-roles-to-groups)).
- `remove_resource()` returns a `bool` instead of pycasbin's list of
  removed rules: `True` only if it revoked a role.
- Rules set via [`access_control.policies`](access-control.md#configuring-policies)
  are no longer removed by `remove_resource_role()`, `remove_resource()` or
  single-rule removals via FOCA's Casbin adapter (e.g.,
  `enforcer.remove_grouping_policy()`; filtered removals still remove them),
  as they would be added again on the next start; a warning is logged instead, and
  `remove_resource_role()` returns `False`. Remove such rules from the
  configuration ([details](access-control.md#resource-scoped-roles)).

### Policy collection

With environment variable `MONGO_DBNAME` set, the app fails to start if an app
database has a collection named like the access control policy collection
(`security.access_control.collection_name`), as all databases then map to
the same database. Rename one of them.

## Behaviour changes

- `list_resource_roles()` returns each subject and role pair once, and
  `remove_resource_role()` removes all stored copies of a rule (e.g., if it
  was stored twice by concurrent requests).
- Access control policies load in linear time in the number of stored rules
  (previously quadratic), so enforced requests slow down much less as rules
  accumulate ([details](access-control.md#scaling)).
- Exceptions logged in the formats `oneline` and `minimal`, and request
  paths, subjects and rule identifiers in access control and permissions API
  logs, have non-printable characters escaped (e.g., a line break is logged
  as `\n`). Adjust log parsers that rely on the raw values.
- New startup warnings: access control enabled with several trusted issuers
  (subjects and group claim values are not scoped by issuer; see
  [authentication](authentication.md)), `disable_auth` together with
  `access_control.enforce` (operations of such specifications are public),
  and `keep_group_subjects` without `group_mapping`.
- The example apps' Compose files publish MongoDB on `127.0.0.1` only.

## New options

- [`service_info`](configuration/service-info.md): serves GA4GH service info
  at `GET /service-info` from the configuration, optionally with the version
  of an installed package and fields computed at runtime. Requests and
  responses are logged unless `service_info.log_traffic` is `false`.
- [`exceptions.media_type`](configuration/exceptions.md#media-type): media
  type of all error responses (default: `application/problem+json`, as
  before), e.g., `application/json` for GA4GH APIs declaring it. FOCA's
  default permissions API specification declares the configured media type;
  custom specifications must declare it themselves.
- Roles on resources can be granted to groups
  ([details](access-control.md#granting-roles-to-groups)):
  `subject_type="group"` for `add_resource_role()`, `remove_resource_role()`
  and `list_resource_roles()`, `security.access_control.keep_group_subjects`
  to use group claim values as subjects also with `group_mapping`, and
  `get_current_group_values()` for the group claim values of the current
  request.
- [`security.access_control.log_traffic`](access-control.md#configuring-the-api):
  whether requests to FOCA's permissions API controllers and their responses
  are logged (default: `true`, as in 0.14), like the new
  `service_info.log_traffic` for the service info.
- [`server.max_content_length`](configuration/server.md): rejects request
  bodies larger than this many bytes with `413` (default: no limit). See also
  [running in production](deployment.md#running-in-production).

## Dependencies

- New: `jsonschema>=4.17.3,<5`.
- Raised lower bounds: `PyJWT>=2.11` (with PyJWT 2.10, `foca.security.auth`
  fails to import, also in FOCA 0.14), `requests>=2.32.4`, `PyYAML>=6.0.1`
  and `typing_extensions>=4.13`.
- `pycasbin` is capped at `<2.9`, as FOCA's adapter relies on Casbin
  internals.
