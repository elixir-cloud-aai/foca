"""FOCA config models."""

import os
from typing import (Dict, List, Optional)

from pydantic import (BaseSettings, validator)


class FOCABaseConfig(BaseSettings):
    # raise error if additional arg is passed
    class Config:
        extra = 'forbid'
        arbitrary_types_allowed = True


class ServerConfig(FOCABaseConfig):
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = True
    environment: str = "development"
    testing: bool = False
    use_reloader: bool = True


class SpecConfig(FOCABaseConfig):
    """Model for configuration parameters for OpenAPI 2.x or 3.x specifications
    to be attached to a Connexion app.
    Args:
        path: Path to an OpenAPI 2.x or 3.x specification in YAML format.
        path_out: Output path for modified specification file. Ignored if specs
            are not modified. If not specified, the original file path is
            stripped of the file extension and the suffix '.modified.yaml' is
            appended.
        append: Fields to be added/modified in the root of the specification
            file. For OpenAPI 2, see https://swagger.io/specification/v2/. For
            OpenAPI 3, see https://swagger.io/specification/.
        add_operation_fields: Fields to be added/modified in the Operation
            Objects of each Path Info Object. An example use case for this is
            the addition or replacement of the `x-swagger-router-controller`
            field. For OpenAPI 2, see
            https://swagger.io/specification/v2/#operation-object. For OpenAPI
            3, see https://swagger.io/specification/#operation-object.
        connexion: Keyword arguments passed through to the `add_api()` method
            in Connexion's `connexion.apps.flask_app` module.
    Attributes:
        path: Path to an OpenAPI 2.x or 3.x specification in YAML format.
        path_out: Output path for modified specification file. Ignored if specs
            are not modified. If not specified, the original file path is
            stripped of the file extension and the suffix '.modified.yaml' is
            appended.
        append: Fields to be added/modified in the root of the specification
            file. For OpenAPI 2, see https://swagger.io/specification/v2/. For
            OpenAPI 3, see https://swagger.io/specification/.
        add_operation_fields: Fields to be added/modified in the Operation
            Objects of each Path Info Object. An example use case for this is
            the addition or replacement of the `x-swagger-router-controller`
            field. For OpenAPI 2, see
            https://swagger.io/specification/v2/#operation-object. For OpenAPI
            3, see https://swagger.io/specification/#operation-object.
        connexion: Keyword arguments passed through to the `add_api()` method
            in Connexion's `connexion.apps.flask_app` module.
    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.
    Example:
        >>> SpecConfig(
        ...     path="/path/to/my/specs.yaml",
        ...     path_out="/path/to/modified/specs.yaml",
        ...     append=[
        ...         {
        ...             "security": {
        ...                 "jwt": {
        ...                     "type": "apiKey",
        ...                     "name": "Authorization",
        ...                     "in": "header",
        ...                 }
        ...             }
        ...         },
        ...         {
        ...             "my_other_root_field": "some_value",
        ...         },
        ...     ],
        ...     add_operation_fields = {
        ...         "x-swagger-router-controller": "controllers.my_specs",
        ...         "x-some-other-custom-field": "some_value",
        ...     },
        ... )
        OpenAPIConfig(path='/path/to/my/specs.yaml', path_out='/path/to/modifi\
ed/specs.yaml', append=<list_iterator object at 0x7f12f0f56f10>, add_operation\
_fields={'x-swagger-router-controller': 'controllers.my_specs', 'x-some-other-\
custom-field': 'some_value'})
    """
    path: str
    path_out: Optional[str] = None
    append: Optional[List[Dict]] = None
    add_operation_fields: Optional[Dict] = None
    connexion: Optional[Dict] = None

    # set default if no output file path provided
    @validator('path_out', always=True)
    def modify_default_out_path(cls, v, *, values):
        """Set default output path for spec file if not supplied by user.
        """
        try:
            return v or '.'.join([
                os.path.splitext(values['path'])[0],
                "modified.yaml"
            ])
        except KeyError:
            return v


class APIConfig(FOCABaseConfig):
    specs: Optional[List[SpecConfig]] = None
    # handle list defaults


class AuthConfig(FOCABaseConfig):
    required: bool = False
    add_key_to_claims: bool = True
    allow_expired: bool = False
    audience: Optional[List[str]] = None
    claim_identity: str = "sub"
    claim_issuer: str = "iss"
    claim_key_id: str = "kid"
    header_name: str = "Authorization"
    token_prefix: str = "Bearer"
    algorithms: Optional[List[str]] = None
    validation_methods: Optional[List[str]] = None
    validation_checks: str = "all"
    # handle list defaults


class SecurityConfig(FOCABaseConfig):
    auth: AuthConfig = AuthConfig()


class DBConfig(FOCABaseConfig):
    host: str = "mongodb"
    port: int = 27017
    name: str = "database"
    # additional params present, not sure if needed for app setup


class JobsConfig(FOCABaseConfig):
    host: str = "rabbitmq"
    port: int = 5672
    result_backend: str = 'rpc://'
    include: Optional[List[str]] = None
    # additional params present, not sure if needed for app setup


class Config(FOCABaseConfig):
    server: ServerConfig = ServerConfig()
    api: APIConfig = APIConfig()
    security: SecurityConfig = SecurityConfig()
    db: DBConfig = DBConfig()
    jobs: JobsConfig = JobsConfig()

    class Config:
        extra = 'allow'
