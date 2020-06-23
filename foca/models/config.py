"""FOCA config models."""

from enum import Enum
import os
from typing import (Dict, List, Optional, Tuple)

from pydantic import (BaseModel, Field, validator)  # pylint: disable=E0611
import pymongo


def validate_log_level_choices(level: int) -> int:
    """Custom validation function for `pydantic` to ensure that a valid
    logging level is configured.

    Args:
        level: Log level choice to be validated.

    Returns:
        Unmodified `level` value if validation succeeds.

    Raises:
        ValueError: Raised if validation fails.
    """
    choices = [0, 10, 20, 30, 40, 50]
    if level not in choices:
        raise ValueError("illegal log level specified")
    return level


class ValidationMethodsEnum(Enum):
    """Supported JWT validation methods."""
    public_key = "public_key"
    userinfo = "userinfo"


class ValidationChecksEnum(Enum):
    """Values indicating how many JWT validation methods are required to
    pass."""
    all = "all"
    any = "any"


class PymongoDirectionEnum(Enum):
    """Supported directions for Pymongo indexes."""
    ASCENDING = 1
    DESCENDING = -1
    GEO2D = "2d"
    GEOHAYSTACK = "geoHaystack"
    GEOSPHERE = "2dsphere"
    HASHED = "hashed"
    TEXT = "text"


class FOCABaseConfig(BaseModel):
    """FOCA Base Settings for Config"""

    class Config:
        """Configuration for `pydantic` model class."""
        extra = 'forbid'
        arbitrary_types_allowed = True


class ServerConfig(FOCABaseConfig):
    """Model for configuration parameters to set up a Flask or Connexion
    app instance.

    Args:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode. If `True`, the
            application runs in debug mode and an interactive debugger will
            be shown for unhandled exceptions. See Flask documentation for more
            details.
        environment: Variable to specify the application environment variable.
            See Flask documentation for more details.
        testing: Enable/disable testing mode. If `True`, exceptions are
            propagated rather than handled by the the app’s error handlers.
        use_reloader: Enable/disable the application reloader. If `debug=True`,
            enabling this will allow the server to reload automatically on
            code changes. See Flask documentation for more details.

    Attributes:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode. If `True`, the
            application runs in debug mode and an interactive debugger will
            be shown for unhandled exceptions. See Flask documentation for more
            details.
        environment: Variable to specify the application environment variable.
            See Flask documentation for more details.
        testing: Enable/disable testing mode. If `True`, exceptions are
            propagated rather than handled by the the app’s error handlers.
        use_reloader: Enable/disable the application reloader. If `debug=True`,
            enabling this will allow the server to reload automatically on
            code changes. See Flask documentation for more details.

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
        ...     use_reloader=True,
        ... )
        ServerConfig(host='0.0.0.0', port=8080, debug=True, environment='devel\
opment', testing=False, use_reloader=True)
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
        ...     path="/path/to/specs.yaml",
        ...     path_out="/path/to/specs.modified.yaml",
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
        SpecConfig(path='/path/to/specs.yaml', path_out='/path/to/specs.modifi\
ed.yaml', append=[{'security': {'jwt': {'type': 'apiKey', 'name': 'Authorizati\
on', 'in': 'header'}}}, {'my_other_root_field': 'some_value'}], add_operation_\
fields={'x-swagger-router-controller': 'controllers.my_specs', 'x-some-other-c\
ustom-field': 'some_value'}, connexion=None)
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
    """Model for a list of configuration parameters for OpenAPI 2.x or 3.x
    specifications to be attached to a Connexion app.

    Args:
        spec: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app.

    Attributes:
        spec: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> APIConfig(
        ...     specs=[SpecConfig(path='/path/to/specs.yaml')],
        ... )
        APIConfig(specs=[SpecConfig(path='/path/to/specs.yaml', path_out='/pat\
h/to/specs.modified.yaml', append=None, add_operation_fields=None, connexion=N\
one)])
    """
    specs: List[SpecConfig] = []


class AuthConfig(FOCABaseConfig):
    """Model for parameters used to configure JSON Web Token (JWT)-based
    authorization for the app.

    Args:
        required: Enable/disable JWT validation for endpoints decorated with
            the `@jwt_validation` decorator defined in `foca.security.auth`.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims when handling requests to
            `@jwt_validation`-decorated endpoints.
        allow_expired: Allow/disallow expired JWTs. If `False`, a `401`
            authorization error is raised in response to a request containing
            an expired JWT.
        audience: List of audiences that the app identifies itself with. If
            specified, JWTs that do not contain any of the specified audiences
            are rejected. If `None`, audience validation is disabled.
        claim_identity: The JWT claim used to identify the sender.
        claim_issuer: The JWT claim used to identify the issuer.
        claim_key_id: The JWT claim used to identify the JWK used when the JWT
            was issued.
        header_name: Name of the request header field at which the app is
            expecting the JWT. Cf. `--token-prefix`.
        token_prefix: Prefix that the app expects to precede the JWT, separated
            by whitespace. Together, prefix and JWT constitute the value of
            the request header field specified by `--header-name`.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validation_methods: Lists the methods to be used to validate a JWT.
            Valid choices are `userinfo` and `public_key`. In the former case,
            validation happens via an OpenID Connect-compliant identify
            provider's `/userinfo` endpoint, in the latter via the identity
            provider's JSON Web Key.
        validation_checks: Specify how many of the `validation_methods` need
            to pass before accepting a JWT. One of `any` and `all`. In the
            former case, JWT validation succeeds after the first successful
            validation check, in the latter case, JWT validation fails after
            the first unsuccessful validation check.

    Attributes:
        required: Enable/disable JWT validation for endpoints decorated with
            the `@jwt_validation` decorator defined in `foca.security.auth`.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims when handling requests to
            `@jwt_validation`-decorated endpoints.
        allow_expired: Allow/disallow expired JWTs. If `False`, a `401`
            authorization error is raised in response to a request containing
            an expired JWT.
        audience: List of audiences that the app identifies itself with. If
            specified, JWTs that do not contain any of the specified audiences
            are rejected. If `None`, audience validation is disabled.
        claim_identity: The JWT claim used to identify the sender.
        claim_issuer: The JWT claim used to identify the issuer.
        claim_key_id: The JWT claim used to identify the JWK used when the JWT
            was issued.
        header_name: Name of the request header field at which the app is
            expecting the JWT. Cf. `--token-prefix`.
        token_prefix: Prefix that the app expects to precede the JWT, separated
            by whitespace. Together, prefix and JWT constitute the value of
            the request header field specified by `--header-name`.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validation_methods: Lists the methods to be used to validate a JWT.
            Valid choices are `userinfo` and `public_key`. In the former case,
            validation happens via an OpenID Connect-compliant identify
            provider's `/userinfo` endpoint, in the latter via the identity
            provider's JSON Web Key.
        validation_checks: Specify how many of the `validation_methods` need
            to pass before accepting a JWT. One of `any` and `all`. In the
            former case, JWT validation succeeds after the first successful
            validation check, in the latter case, JWT validation fails after
            the first unsuccessful validation check.

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
        ...     validation_checks="all",
        ... )
        AuthConfig(required=False, add_key_to_claims=True, allow_expired=False\
, audience=None, claim_identity='sub', claim_issuer='iss', claim_key_id='kid',\
 header_name='Authorization', token_prefix='Bearer', algorithms=['RS256'], val\
idation_methods=[<ValidationMethodsEnum.userinfo: 'userinfo'>, <ValidationMeth\
odsEnum.public_key: 'public_key'>], validation_checks=<ValidationChecksEnum.al\
l: 'all'>)
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
    validation_methods: List[ValidationMethodsEnum] = [
        ValidationMethodsEnum.userinfo,
        ValidationMethodsEnum.public_key,
    ]
    validation_checks: ValidationChecksEnum = ValidationChecksEnum.all


class SecurityConfig(FOCABaseConfig):
    """Model for list the Security configuration.

    Args:
        auth: Config parameters for JSON Web Token (JWT) validation.

    Attributes:
        auth: Config parameters for JSON Web Token (JWT) validation.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> SecurityConfig(
        ...     auth=AuthConfig(),
        ... )
        SecurityConfig(auth=AuthConfig(required=False, add_key_to_claims=True,\
 allow_expired=False, audience=None, claim_identity='sub', claim_issuer='iss',\
 claim_key_id='kid', header_name='Authorization', token_prefix='Bearer', algor\
ithms=['RS256'], validation_methods=[<ValidationMethodsEnum.userinfo: 'userinf\
o'>, <ValidationMethodsEnum.public_key: 'public_key'>], validation_checks=<Val\
idationChecksEnum.all: 'all'>))
    """
    auth: AuthConfig = AuthConfig()


class IndexConfig(FOCABaseConfig):
    """Model for configuring indexes for a MongoDB collection.

    Args:
        keys: A list of key-direction tuples indicating the field to be indexed
            and the sort order of that index. The sort order must be a valid
            MongoDB index specifier, one of `pymongo.ASCENDING`,
            `pymongo.DESCENDING`, `pymongo.GEO2D` etc. or their corresponding
            values `1`, `-1`, `'2d'`, respectively; cf.:
            https://api.mongodb.com/python/current/api/pymongo/collection.html
        name: Custom name to use for the index. If `None` is provided, a name
            will be generated.
        unique: Whether a uniqueness constraint shall be created on the index.
        background: Whether the index shall be created in the background.
        sparse: Whether documents that lack the indexed field shall be omitted
            from the index.
        key: Key denoting a field to be indexed.
        direction: Sort order of field. 

    Attributes:
        keys: A list of key-direction tuples indicating the field to be indexed
            and the sort order of that index. The sort order must be a valid
            MongoDB index specifier, one of `pymongo.ASCENDING`,
            `pymongo.DESCENDING`, `pymongo.GEO2D` etc. or their corresponding
            values `1`, `-1`, `'2d'`, respectively; cf.:
            https://api.mongodb.com/python/current/api/pymongo/collection.html
        name: Custom name to use for the index. If `None` is provided, a name
            will be generated.
        unique: Whether a uniqueness constraint shall be created on the index.
        background: Whether the index shall be created in the background.
        sparse: Whether documents that lack the indexed field shall be omitted
            from the index.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> IndexConfig(
        ...     keys=[('last_name', pymongo.DESCENDING)],
        ...     unique=True,
        ...     sparse=False,
        ... )
        IndexConfig(keys=[('last_name', -1)], name=None, unique=True, backgrou\
nd=False, sparse=False)
    """
    keys: Optional[List[Tuple[str, PymongoDirectionEnum]]] = None
    name: Optional[str] = None
    unique: Optional[bool] = False
    background: Optional[bool] = False
    sparse: Optional[bool] = False

    @validator('keys', always=True, allow_reuse=True)
    def store_enum_value(cls, v):  # pylint: disable=E0213
        """Store value of enumerator, rather than enumerator object."""
        if not v:
            return v
        else:
            new_v = []
            for item in v:
                tmp_list = list(item)
                tmp_list[1] = tmp_list[1].value
                new_v.append(tuple(tmp_list))
            return new_v


class CollectionConfig(FOCABaseConfig):
    """Model for configuring a MongoDB collection.

    Args:
        indexes: An index configuration object.
        client: Client connected to collection. Most likely populated through
            the code, not during setup.

    Attributes:
        indexes: An index configuration object.
        client: Client connected to collection. Most likely populated through
            the code, not during setup.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> CollectionConfig(
        ...     indexes=[IndexConfig(keys=[('last_name', 1)])],
        ... )
        CollectionConfig(indexes=[IndexConfig(keys=[('last_name', 1)], name=No\
ne, unique=False, background=False, sparse=False)], client=None)
    """
    indexes: Optional[List[IndexConfig]] = None
    client: Optional[pymongo.collection.Collection] = None


class DBConfig(FOCABaseConfig):
    """Model for configuring a MongoDB database.

    Args:
        collections: Mapping of collection names (keys) and configuration
            objects (values).
        client: Client connected to database. Most likely populated through the
            code, not during setup.

    Attributes:
        collections: Mapping of collection names (keys) and configuration
            objects (values).
        client: Client connected to database. Most likely populated through the
            code, not during setup.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> DBConfig(
        ...     collections={
        ...         'my_collection': CollectionConfig(
        ...             indexes=[IndexConfig(keys=[('last_name', 1)])],
        ...         ),
        ...     },
        ... )
        DBConfig(collections={'my_collection': CollectionConfig(indexes=[Index\
Config(keys=[('last_name', 1)], name=None, unique=False, background=False, spa\
rse=False)], client=None)}, client=None)
    """
    collections: Optional[Dict[str, CollectionConfig]] = None
    client: Optional[pymongo.database.Database] = None


class MongoConfig(FOCABaseConfig):
    """Model for configuring a MongoDB instance attached to a Flask or
    Connexion app.

    Args:
        host: Host at which the database is exposed.
        port: Port at which the database is exposed.
        dbs: Mapping of database names (keys) and configuration objects
            (values).

    Attributes:
        host: Host at which the database is exposed.
        port: Port at which the database is exposed.
        dbs: Mapping of database names (keys) and configuration objects
            (values).

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> MongoConfig(
        ...     host="mongodb",
        ...     port=27017,
        ... )
        MongoConfig(host='mongodb', port=27017, dbs=None)
    """
    host: str = "mongodb"
    port: int = 27017
    dbs: Optional[Dict[str, DBConfig]] = None


class JobsConfig(FOCABaseConfig):
    """Model for configuring a RabbitMQ broker instance and Celery client
    and tasks to be attached to a Flask/Connexion app.

    Args:
        host: Host at which the broker is exposed.
        port: Port at which the broker is exposed.
        backend: Backend used to store background task results.
        include: List of modules to import when workers start.

    Attributes:
        host: Host at which the broker is exposed.
        port: Port at which the broker is exposed.
        backend: Backend used to store background task results.
        include: List of modules to import when workers start.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> JobsConfig(
        ...     host="rabbitmq",
        ...     port=5672,
        ...     backend='rpc://',
        ...     include=[],
        ... )
        JobsConfig(host='rabbitmq', port=5672, backend='rpc://', include=[])
    """
    host: str = "rabbitmq"
    port: int = 5672
    backend: str = 'rpc://'
    include: Optional[List[str]] = None


class LogFormatterConfig(FOCABaseConfig):
    """Model for formatter for LogConfig.

    Args:
        class: Name of logging formatter class.
        style: Determines how the format string will be merged with its data.
        format: Format string any log messages.

    Attributes:
        class: Name of logging formatter class.
        style: Determines how the format string will be merged with its data.
        format: Format string any log messages.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogFormatterConfig(
        ...     style="{",
        ...     format="[{asctime}: {levelname:<8}] {message} [{name}]",
        ... )
        LogFormatterConfig(class_formatter='logging.Formatter', style='{', for\
mat='[{asctime}: {levelname:<8}] {message} [{name}]')
    """
    class_formatter: str = Field(
        "logging.Formatter",
        alias="class",
    )
    style: str = "{"
    format: str = "[{asctime}: {levelname:<8}] {message} [{name}]"


class LogHandlerConfig(FOCABaseConfig):
    """Model for passing logging handler parameters to configuring app logging.

    Args:
        class: Name of logging handler class.
        level: Numeric value of logging level.
        formatter: Name of logging formatter.
        stream: Devide to which log is streamed.

    Attributes:
        class: Name of logging handler class.
        level: Numeric value of logging level.
        formatter: Name of logging formatter.
        stream: Devide to which log is streamed.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogHandlerConfig(
        ...     level=20,
        ...     formatter="standard",
        ...     stream="ext://sys.stderr",
        ... )
        LogHandlerConfig(class_handler='logging.StreamHandler', level=20, form\
atter='standard', stream='ext://sys.stderr')
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
        level: Numeric value of logging level.
        handlers: List of logging handlers by name.

    Attributes:
        level: Numeric value of logging level.
        handlers: List of logging handlers by name.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> LogRootConfig(
        ...     level=logging.INFO,
        ...     handlers=["console"],
        ... )
        LogRootConfig(level=20, handlers=['console'])
    """
    level: int = 10
    handlers: Optional[List[str]] = ["console"]

    _validate_level = validator('level', allow_reuse=True)(
        validate_log_level_choices
    )


class LogConfig(FOCABaseConfig):
    """Model for passing parameters for configuring app logging.

    Args:
        version: Schema version.
        disable_existing_loggers: Whether any existing non-root loggers are to
            be disabled.
        formatters: A dictionary of logging formatters, where keys represent
            formatter names and values represent the corresponding
            configuration parameters.
        handlers: A dictionary of logging handlers, where keys represent
            handler names and values represent the corresponding configuration
            parameters.
        root: Configuration of the root logger.

    Attributes:
        version: Represents current schema version.
        disable_existing_loggers: Whether any existing non-root loggers are to
            be disabled.
        formatters: A dictionary of logging formatters, where keys represent
            formatter names and values represent the corresponding
            configuration parameters.
        handlers: A dictionary of logging handlers, where keys represent
            handler names and values represent the corresponding configuration
            parameters.
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
        LogConfig(version=1, disable_existing_loggers=False, formatters={'stan\
dard': LogFormatterConfig(class_formatter='logging.Formatter', style='{', form\
at='[{asctime}: {levelname:<8}] {message} [{name}]')}, handlers={'console': Lo\
gHandlerConfig(class_handler='logging.StreamHandler', level=20, formatter='sta\
ndard', stream='ext://sys.stderr')}, root=LogRootConfig(level=10, handlers=['c\
onsole']))
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
    """Model for all app configuration parameters.

    Args:
        server: Server config parameters.
        api: OpenAPI specification config parameters.
        security: Security config parameters.
        db: Database config parameters.
        jobs: Background job config parameters.
        log: Logger config parameters.

    Attributes:
        server: Server config parameters.
        api: OpenAPI specification config parameters.
        security: Security config parameters.
        db: Database config parameters.
        jobs: Background job config parameters.
        log: Logger config parameters.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> Config()
        Config(server=ServerConfig(host='0.0.0.0', port=8080, debug=True, envi\
ronment='development', testing=False, use_reloader=True), api=APIConfig(specs=\
[]), security=SecurityConfig(auth=AuthConfig(required=False, add_key_to_claims\
=True, allow_expired=False, audience=None, claim_identity='sub', claim_issuer=\
'iss', claim_key_id='kid', header_name='Authorization', token_prefix='Bearer',\
 algorithms=['RS256'], validation_methods=[<ValidationMethodsEnum.userinfo: 'u\
serinfo'>, <ValidationMethodsEnum.public_key: 'public_key'>], validation_check\
s=<ValidationChecksEnum.all: 'all'>)), db=None, jobs=None, log=LogConfig(versi\
on=1, disable_existing_loggers=False, formatters={'standard': LogFormatterConf\
ig(class_formatter='logging.Formatter', style='{', format='[{asctime}: {leveln\
ame:<8}] {message} [{name}]')}, handlers={'console': LogHandlerConfig(class_ha\
ndler='logging.StreamHandler', level=20, formatter='standard', stream='ext://s\
ys.stderr')}, root=LogRootConfig(level=10, handlers=['console'])))
    """
    server: ServerConfig = ServerConfig()
    api: APIConfig = APIConfig()
    security: SecurityConfig = SecurityConfig()
    db: Optional[MongoConfig] = None
    jobs: Optional[JobsConfig] = None
    log: LogConfig = LogConfig()

    class Config:
        """Configuration for `pydantic` model class."""
        extra = 'allow'
