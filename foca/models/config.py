"""FOCA config models."""

import os
from typing import (Dict, List, Optional)

from pydantic import (BaseModel, Field, validator)  # pylint: disable=E0611


def validate_log_level_choices(level: int) -> int:
    """Check if valid level value has been assigned"""
    choices = [0, 10, 20, 30, 40, 50, 60]
    if level not in choices:
        raise ValueError("illegal log level specified")
    return level


class FOCABaseConfig(BaseModel):
    """FOCA Base Settings for Config"""
    # raise error if additional arg is passed
    class Config:
        extra = 'forbid'
        arbitrary_types_allowed = True


class ServerConfig(FOCABaseConfig):
    """Model for configuration parameters for Server specifications
    to be attached to a Connexion app.

    Args:
        host: Host address for the application.
        port: Port address for the application.
        debug: Flag to run application in debug mode. If True, then
            application runs in debug mode and an interactive debugger
            will be shown for unhandled exceptions.
        environment: Variable to specify the application environment
            variable.
        testing: Enable/Disable testing mode.If True, thenexceptions
            are propagated rather than handled by the the app’s error
            handlers.
        use_reloader: Enable/Disable the application reloader. If debug=True,
            enabling this will allow the server to reload automatically on
            code changes.

    Attributes:
        host: Host address for the application.
        port: Port address for the application.
        debug: Flag to run application in debug mode. If True, then
            application runs in debug mode and an interactive debugger
            will be shown for unhandled exceptions.
        environment: Variable to specify the application environment
            variable.
        testing: Enable/Disable testing mode.If True, thenexceptions
            are propagated rather than handled by the the app’s error
            handlers.
        use_reloader: Enable/Disable the application reloader. If debug=True,
            enabling this will allow the server to reload automatically on
            code changes.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ServerConfig(
        ...     host="0.0.0.0",
        ...     port=8080,
        ...     debug=True,
        ...     environment="development",
        ...     testing=False,
        ...     use_reloader=True
        ... )
        ServerConfig(host="0.0.0.0",port=8080,debug=True,environment="develop\
ment",testing=False,use_reloader=True)
    """
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
        SpecConfig(path='/path/to/my/specs.yaml', path_out='/path/to/modified/\
specs.yaml', append=<list_iterator object at 0x7f12f0f56f10>, add_operation_fi\
elds={'x-swagger-router-controller': 'controllers.my_specs', 'x-some-other-cus\
tom-field': 'some_value'})
    """
    path: str
    path_out: Optional[str] = None
    append: Optional[List[Dict]] = None
    add_operation_fields: Optional[Dict] = None
    connexion: Optional[Dict] = None

    # set default if no output file path provided
    @validator('path_out', always=True, allow_reuse=True)
    def set_default_out_path(cls, v, *, values):  # pylint: disable=E0213
        """Set default output path for spec file if not supplied by user.
        """
        if 'path' in values and values['path'] is not None:
            return v or '.'.join([
                os.path.splitext(values['path'])[0],
                "modified.yaml"
            ])
        return v


class APIConfig(FOCABaseConfig):
    """Model for list the OpenAPI specifications for the Connexion app.

    Args:
        spec: List object enclosing OpenAPI 2.x or 3.x specifications
            for the Connexion app.

    Attributes:
        spec: List object enclosing OpenAPI 2.x or 3.x specifications
            for the Connexion app.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> SpecConfig(
        ...     spec=[SpecConfig]
        ... )
        APIConfig(specs=[SpecConfig])
    """
    specs: List[SpecConfig] = []


class AuthConfig(FOCABaseConfig):
    """Model for configuration parameters for JWTAuth token specifications
    to be attached to a Connexion app.

    Args:
        required: Enable/Disable authorization setting.
        add_key_to_claims: Permission to add the pem key to the claims dict.
        allow_expired: Allow/Disallow user to use expired passkey.
        audience: List of recipients that the JWT is intended for.
        claim_identity: JWT claim identifier.
        claim_issuer: Issuer Claim for JWT.
        claim_key_id: Key for validating the JWT signature.
        header_name: Header name to which the JWT will be added to when
            making a request.
        token_prefix: JWT token prefix.
        algorithms: List of JWT algorithms to sign the key.
        validation_methods: List of validation methods to be used.
        validation_checks: Specify the validation checks to be made.

    Attributes:
        required: Enable/Disable authorization setting.
        add_key_to_claims: Permission to add the pem key to the claims dict.
        allow_expired: Allow/Disallow user to use expired passkey.
        audience: List of recipients that the JWT is intended for.
        claim_identity: JWT claim identifier.
        claim_issuer: Issuer Claim for JWT.
        claim_key_id: Key for validating the JWT signature.
        header_name: Header name to which the JWT will be added to when
            making a request.
        token_prefix: JWT token prefix.
        algorithms: List of JWT algorithms to sign the key.
        validation_methods: List of validation methods to be used.
        validation_checks: Specify the validation checks to be made.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> AuthConfig(
        ...     required=False,
        ...     add_key_to_claims=True,
        ...     allow_expired=False,
        ...     audience=None,
        ...     claim_identity="sub",
        ...     claim_issuer="iss",
        ...     claim_key_id="kid",
        ...     header_name="Authorization",
        ...     token_prefix="Bearer",
        ...     algorithms=["RS256"],
        ...     validation_methods=["userinfo", "public_key"],
        ...     validation_checks="all"
        ... )
        AuthConfig(required=False,add_key_to_claims=True,allow_expired=False,\
audience=None,claim_identity="sub",claim_issuer="iss",claim_key_id="kid",heade\
r_name="Authorization",token_prefix="Bearer",algorithms=["RS256"],validation_m\
ethods=["userinfo", "public_key"],validation_checks="all")
    """

    required: bool = False
    add_key_to_claims: bool = True
    allow_expired: bool = False
    audience: Optional[List[str]] = None
    claim_identity: str = "sub"
    claim_issuer: str = "iss"
    claim_key_id: str = "kid"
    header_name: str = "Authorization"
    token_prefix: str = "Bearer"
    algorithms: List[str] = ["RS256"]
    validation_methods: List[str] = ["userinfo", "public_key"]
    validation_checks: str = "all"


class SecurityConfig(FOCABaseConfig):
    """Model for list the Security configuration.

    Args:
        auth: Class object containing security specifications.

    Attributes:
        auth: Class object containing security specifications.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> SecurityConfig(
        ...     auth=AuthConfig(**kwargs)
        ... )
        SecurityConfig(auth=AuthConfig(**kwargs))
    """
    auth: AuthConfig = AuthConfig()


class DBConfig(FOCABaseConfig):
    """Model for configuration parameters for Database specifications
    to be attached to a Connexion app.

    Args:
        host: Databse host name.
        port: Database port number.
        name: Databse name.

    Attributes:
        host: Databse host name.
        port: Database port number.
        name: Databse name.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> DBConfig(
        ...     host="mongodb",
        ...     port=27017,
        ...     name="database"
        ... )
        DBConfig(host="mongodb",port=27017,name="database")
    """
    host: str = "mongodb"
    port: int = 27017
    name: str = "database"


class JobsConfig(FOCABaseConfig):
    """Model for configuration parameters for Jobs specifications
    to be attached to a Connexion app.

    Args:
        host: Host address for a job server.
        port: Port number for a job server.
        result_backend: Backend mapping used to store task results.
        include: List of modules to import when worker starts.

    Attributes:
        host: Host address for a job server.
        port: Port number for a job server.
        result_backend: Backend mapping used to store task results.
        include: List of modules to import when worker starts.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> JobsConfig(
        ...     host="rabbitmq",
        ...     port=5672,
        ...     result_backend='rpc://,
        ...     include=[]
        ... )
        JobsConfig(host="rabbitmq",port=5672,result_backend='rpc://,include=[])
    """
    host: str = "rabbitmq"
    port: int = 5672
    result_backend: str = 'rpc://'
    include: Optional[List[str]] = None


class LogFormatterConfig(FOCABaseConfig):
    """Model for formatter for LogConfig.

    Args:
        class_formatter: Formatter class instance.
        style: Determines how the format string will be merged with its data.
        format: Default string format.

    Attributes:
        class_formatter: Formatter class instance.
        style: Determines how the format string will be merged with its data.
        format: Default string format.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogFormatterConfig(
        ...     class_formatter=Field("logging.Formatter",alias="class"),
        ...     style="{",
        ...     format="[{asctime}: {levelname:<8}] {message} [{name}]"
        ... )
        LogFormatterConfig(class_formatter=Field("logging.Formatter",alias="cla\
ss"),style="{",format="[{asctime}: {levelname:<8}] {message} [{name}]")
    """
    class_formatter: str = Field(
        "logging.Formatter",
        alias="class",
    )
    style: str = "{"
    format: str = "[{asctime}: {levelname:<8}] {message} [{name}]"


class LogHandlerConfig(FOCABaseConfig):
    """Model for handlers for LogConfig.

    Args:
        class_handler: Handler class instance.
        level: Numeric level of the logging event.
        formatter: Logging formatter.
        stream: Specified stream for initializing StreamHandler.

    Attributes:
        class_handler: Handler class instance.
        level: Numeric level of the logging event.
        formatter: Logging formatter.
        stream: Specified stream for initializing StreamHandler.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogHandlerConfig(
        ...     class_handler=Field("logging.StreamHandler",alias="class"),
        ...     level=20,
        ...     formatter="standard",
        ...     stream="ext://sys.stderr"
        ... )
        LogHandlerConfig(class_handler=Field("logging.StreamHandler",alias="clas\
s"),level=20,formatter="standard",stream="ext://sys.stderr")
    """
    class_handler: str = Field(
        "logging.StreamHandler",
        alias="class",
    )
    level: int = 20
    formatter: str = "standard"
    stream: str = "ext://sys.stderr"

    _validate_level = validator('level', allow_reuse=True)(
        validate_log_level_choices
    )


class LogRootConfig(FOCABaseConfig):
    """Model for root log configuration.

    Args:
        level: Numeric level of logging event.
        handlers: List of LogHandler instances.

    Attributes:
        level: Numeric level of logging event.
        handlers: List of LogHandler instances.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogRootConfig(
        ...     level=10,
        ...     handlers=["console"]
        ... )
        LogRootConfig(level=10,handlers=["console"])
    """
    level: int = 10
    handlers: Optional[List[str]] = ["console"]

    _validate_level = validator('level', allow_reuse=True)(
        validate_log_level_choices
    )


class LogConfig(FOCABaseConfig):
    """Model for configuration parameters for logger specifications
    to be attached to a Connexion app.

    Args:
        version: Represents current schema version.
        disable_existing_loggers: Specifies whether any existing non-root
            loggers are to be disabled.
        formatters: A dict in which each key is a formatter id and each
            value is a dict describing how to configure the corresponding
            Formatter instance.
        handlers: A dict in which each key is a handler id and each value
            is a dict describing how to configure the corresponding Handler
            instance.
        root: Configuration of the root logger.

    Attributes:
        version: Represents current schema version.
        disable_existing_loggers: Specifies whether any existing non-root
            loggers are to be disabled.
        formatters: A dict in which each key is a formatter id and each
            value is a dict describing how to configure the corresponding
            Formatter instance.
        handlers: A dict in which each key is a handler id and each value
            is a dict describing how to configure the corresponding Handler
            instance.
        root: Configuration of the root logger.


    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogConfig(
        ...     version=1,
        ...     disable_existing_loggers=False,
        ...     formatters={
        ...         "standard": LogFormatterConfig()
        ...     },
        ...     handlers={
        ...         "console": LogHandlerConfig()
        ...     },
        ...     root=LogRootConfig()
        ... )
        LogConfig(version=1,disable_existing_loggers=False,formatters={"stand\
ard": LogFormatterConfig()},handlers={"console": LogHandlerConfig()},root=Log\
RootConfig())
    """
    version: int = 1
    disable_existing_loggers: bool = False
    formatters: Optional[Dict[str, LogFormatterConfig]] = {
        "standard": LogFormatterConfig(),
    }
    handlers: Optional[Dict[str, LogHandlerConfig]] = {
        "console": LogHandlerConfig(),
    }
    root: Optional[LogRootConfig] = LogRootConfig()


class Config(FOCABaseConfig):
    """Main Config class.

    Args:
        server: Server config parameters.
        api: Api config parameters and specifications.
        security: Security config parameters.
        db: Databse config parameters.
        jobs: Job config parameters.
        log: Logger config parameters.

    Attributes:
        server: Server config parameters.
        api: Api config parameters and specifications.
        security: Security config parameters.
        db: Databse config parameters.
        jobs: Job config parameters.
        log: Logger config parameters.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> DBConfig(
        ...     server=ServerConfig(**kwargs),
        ...     api=APIConfig(**kwargs),
        ...     security=SecurityConfig(**kwargs),
        ...     db=DBConfig(**kwargs),
        ...     jobs=JobsConfig(**kwargs),
        ...     log=LogConfig(**kwargs),
        ... )
        DBConfig(host="mongodb",port=27017,name="database")
    """
    server: ServerConfig = ServerConfig()
    api: APIConfig = APIConfig()
    security: SecurityConfig = SecurityConfig()
    db: Optional[DBConfig] = None
    jobs: Optional[JobsConfig] = None
    log: LogConfig = LogConfig()

    class Config:
        extra = 'allow'
