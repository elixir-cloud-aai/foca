# API reference

Reference of FOCA's public Python API, generated from its docstrings. For
configuration models, see [Configuration](configuration/index.md).

## App factory

::: foca.Foca

## Configuration access

::: foca.config.access
    options:
      members:
        - get_config
        - get_custom_config
        - get_collection

## Authentication

::: foca.security.auth
    options:
      members:
        - validate_token
        - get_token_client
        - get_service_client
        - require_no_service_clients

## Access control

::: foca.security.access_control.enforcement
    options:
      members:
        - enforce_access_control
        - is_permitted
        - get_current_subject
        - get_current_groups
        - get_current_claims
        - get_current_group_values
        - get_current_client
        - get_on_behalf_of_user
        - locked_enforcer
        - add_resource_role
        - remove_resource_role
        - list_resource_roles
        - remove_resource
        - SubjectType

::: foca.security.access_control.ownership
    options:
      members:
        - add_owner
        - grant_role
        - revoke_roles
        - list_roles
        - list_subject_roles
        - resource_filter
        - is_admin
        - set_owner_field
        - owner_filter
        - LastOwner
        - ResourceRole
        - SubjectResourceRole

::: foca.security.access_control.ownership_server
    options:
      members:
        - getResourceRoles
        - putUserRole
        - deleteUserRole
        - putGroupRole
        - deleteGroupRole

::: foca.security.access_control.foca_casbin_adapter.adapter
    options:
      members:
        - Adapter

::: foca.security.access_control.foca_casbin_adapter.casbin_rule
    options:
      members:
        - CasbinRule
        - validate_rule
        - validate_rule_value

## Gateway plugins

::: foca.plugins.base
    options:
      members:
        - Plugin
        - NotApplicable
        - Rejected
        - PluginError
        - API_VERSION
        - PHASES

::: foca.plugins.model

::: foca.plugins.context
    options:
      members:
        - Context

::: foca.plugins.engine
    options:
      members:
        - PluginHost
        - PipelineRun
        - Attempt

::: foca.plugins.outcomes
    options:
      members:
        - NotAccepted
        - OutcomeUnknown
        - sanitize_error

::: foca.plugins.records
    options:
      members:
        - ResolvedPipeline
        - ResolvedStep
        - ResolvedAlternative
        - Execution
        - AttemptRecord

::: foca.plugins.checks
    options:
      members:
        - default_outbound

::: foca.plugins.errors

::: foca.plugins.selection
    options:
      members:
        - parse_client_tags
        - parse_client_pipeline
        - apply_backend_selection
        - BackendSelection
        - ClientSelection

::: foca.plugins.loader
    options:
      members:
        - load_plugins
        - check_legacy_keys
        - LoadedPlugin

::: foca.plugins.provenance

::: foca.plugins.backends
    options:
      members:
        - BackendList
        - BackendListConfig
        - ServiceEntry
        - RegistryEntry
        - BasicCredentialsConfig
        - LocationConfig
        - RegistryCache
        - select_services
        - clear_caches
        - API_TYPES

::: foca.plugins.routing
    options:
      members:
        - OrderedRouter
        - RouterConfig
        - RouterParams

::: foca.plugins.tags

::: foca.plugins.api

::: foca.plugins.runtime
    options:
      members:
        - GatewayApp
        - get_host
        - create_host
        - configure
        - get_backend_list
        - reset

::: foca.plugins.adapter

## Errors

::: foca.errors.exceptions
    options:
      members:
        - build_problem
        - problem_response
        - shorten_detail
        - register_exception_handler

## Service info

::: foca.api.service_info
    options:
      members:
        - getServiceInfo
        - get_service_info
        - build_service_info
        - check_service_info

::: foca.utils.service_info
    options:
      members:
        - InvalidServiceInfo
        - merge_service_info
        - validate_service_info
        - without_nulls
        - lookup_package_version
        - import_runtime_function
        - call_runtime_function

## Databases

::: foca.database.register_mongodb
    options:
      members:
        - create_mongo_client
        - get_mongo_client
        - get_mongo_db_name
        - register_mongodb

::: foca.utils.db

## Utilities

::: foca.utils.misc

::: foca.utils.urls
    options:
      members:
        - check_url
        - check_url_fields
        - normalize_url
        - get_public_url
        - InvalidURL
        - URLScheme
        - HTTP_SCHEMES
        - SUPPORTED_SCHEMES

::: foca.utils.http

::: foca.utils.logging
    options:
      members:
        - log_traffic
        - log_traffic_if
        - escape_log_value
