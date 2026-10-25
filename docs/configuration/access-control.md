# `security.access_control`

Policy-based access control with [Casbin](https://casbin.org/), including a
permissions API for managing policy rules.

```yaml
security:
  auth:
    issuers:
      - https://login.example.org/oidc/
    audience:
      - my-app
    service_clients:
      - client_id: conquest-client-id
  access_control:
    enabled: true
    enforce: true
    db_name: access_control_db
    collection_name: policy_rules
    model: policies.conf
    claim_groups: eduperson_entitlement
    group_mapping:
      "urn:example:group:my-app-admins#login.example.org": admin
    keep_group_subjects: true
    require_admin: true
    client_mapping:
      conquest-client-id: submitter
    resource_roles: [owner, maintainer]
    self_service: true
    policies:
      - [p, "*", /pets, "(GET)|(POST)"]
    api_swagger_ui: true
    api_serve_spec: true
    log_traffic: true
```

Access control requires `enabled: true` and trusted token
[`issuers`](auth.md); the other options are rejected otherwise. With
`enforce: true`, policies are enforced on all operations that require
authentication. `client_mapping` maps [service
clients](../authentication.md#service-tokens) (`security.auth.service_clients`)
to roles; other clients cannot be mapped. `opt_in_roles` lists roles of
`group_mapping` that apply only to requests asking for them in the header
`opt_in_header` (default: `X-Request-Roles`), e.g., for an admin mode (see
[opt-in roles](../access-control.md#opt-in-roles)); by default, there are
none. `resource_roles` are the roles
that the [resource ownership](../access-control.md#resource-ownership) helpers
grant and revoke. `policies` can also be given as named rules, so that
config overlays can change single rules (see [configuring
policies](../access-control.md#configuring-policies)). See [Access
control](../access-control.md) for how policies, roles and the permissions API
work.

## Startup checks

Some settings keep the app from working as intended. When access control
is enabled, `Foca.create_app()` checks them, and refuses to start
(`ValueError`) or logs a warning, naming what to change:

- **Admins:** the groups (via `group_mapping`) and service clients (via
  `client_mapping`) mapped to the role `admin` are logged (`INFO`). If none
  is mapped to it and no rule of `policies` grants it (e.g., `[g,
  "user:alice", admin]`), a warning is logged, as nobody is then an admin
  via mappings or configured rules. Rules for groups (`group:<value>`)
  count only if `claim_groups` is set and group claim values are used as
  group subjects (see below). Only direct
  grants are checked: a rule granting `admin` to a role (e.g., `[g, ops,
  admin]`) or to a service client (`client:<client ID>`) counts, whether
  or not any subject can get that role or is that client. Roles
  granted via stored policy rules (e.g., via the permissions API) are not
  checked, and still apply. With `require_admin: true`, the app refuses to
  start (`ValueError`) instead, before anything is stored; set it if the
  app needs admins, e.g., to manage resources of others. It defaults to
  `false` and requires `enabled: true`.
- **Group claim:** the app refuses to start (`ValueError`) if
  `group_mapping` maps groups, but `claim_groups` is not set, as no mapping
  would then apply, and if `claim_groups` has surrounding whitespace, as the
  claim is looked up as written. An empty `group_mapping` without
  `claim_groups` is allowed, with a warning, as users then have no groups.
- **Group subjects:** a warning is logged if `group_mapping` is set, but
  `keep_group_subjects` is not, as group claim values are then not used as
  group subjects (`group:<value>`): rules and [roles granted to
  groups](../access-control.md#granting-roles-to-groups) don't apply, and
  the resource ownership helpers refuse to grant them. Set
  `keep_group_subjects: true` if the app grants roles to groups.
- **Opt-in roles:** the app refuses to start (`ValueError`) if a role of
  `opt_in_roles` is not a role of `group_mapping`, or a rule of `policies`
  grants an opt-in role (e.g., `[g, "user:alice", admin]`), as roles
  granted by rules always apply. The opt-in roles and their header are
  logged (`INFO`), and a warning is logged if CORS is enabled but doesn't
  allow the header. These checks also run without access control, as
  gateway plugins use the roles, too.
- `keep_group_subjects` without `group_mapping` (no effect) and several
  trusted `issuers` (subjects are not scoped by issuer) are warned about,
  too, and so is the removal of stored configured rules if `policies` is
  not set (see [configuring
  policies](../access-control.md#configuring-policies)).

Messages name the settings to change, and contain no claim values other
than configured group names. Without access control, none of these checks
run. See [startup checks](../deployment.md#startup-checks) for all of
FOCA's checks.

::: foca.models.config.AccessControlConfig
    options:
      members: false
