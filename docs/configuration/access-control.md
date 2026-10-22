# `security.access_control`

Policy-based access control with [Casbin](https://casbin.org/), including a
permissions API for managing policy rules.

```yaml
security:
  auth:
    issuers:
      - https://login.elixir-czech.org/oidc/
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
      "urn:geant:elixir-europe.org:group:elixir:my-app:admins": admin
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
to roles; other clients cannot be mapped. `resource_roles` are the roles
that the [resource ownership](../access-control.md#resource-ownership) helpers
grant and revoke. `policies` can also be given as named rules, so that
config overlays can change single rules (see [configuring
policies](../access-control.md#configuring-policies)). See [Access
control](../access-control.md) for how policies, roles and the permissions API
work.

::: foca.models.config.AccessControlConfig
    options:
      members: false
