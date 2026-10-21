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
        - get_collection

## Authentication

::: foca.security.auth
    options:
      members:
        - validate_token

## Access control

::: foca.security.access_control.enforcement
    options:
      members:
        - enforce_access_control
        - is_permitted
        - get_current_subject
        - get_current_groups
        - get_current_group_values
        - get_enforcer
        - add_resource_role
        - remove_resource_role
        - list_resource_roles
        - remove_resource
        - SubjectType

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

## Errors

::: foca.errors.exceptions
    options:
      members:
        - build_problem
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

::: foca.utils.logging
    options:
      members:
        - log_traffic
        - log_traffic_if
        - escape_log_value
