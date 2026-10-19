"""FOCA config models."""

import importlib
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import (
    Any,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, collection, database
from typing_extensions import Self

from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_ACCESS_CONTROL_COLLECTION_NAME,
    DEFAULT_ACCESS_CONTROL_DB_NAME,
    DEFAULT_ID_CHARSET,
    DEFAULT_ID_LENGTH,
    DEFAULT_MODEL_FILE,
    GROUP_PREFIX,
    MIN_ID_SPACE,
)


def _resolve_path(path: str | Path, info: ValidationInfo) -> Path:
    """Resolve a file path of the configuration.

    Args:
        path: File path.
        info: Validation info; relative paths are resolved against the
            directory in its context's `base_dir` (e.g., the directory of the
            base config file), if set, otherwise against the current working
            directory.

    Returns:
        Absolute path.
    """
    path = Path(path)
    if path.is_absolute():
        return path
    base_dir = (info.context or {}).get("base_dir")
    if base_dir is not None:
        path = Path(base_dir) / path
    return path.resolve()


def _validate_log_level_choices(cls, level: int) -> int:
    """Ensure that a valid logging level is set.

    Args:
        cls: Model class.
        level: Log level choice to be validated.

    Returns:
        Unmodified `level` value if validation succeeds.

    Raises:
        ValueError: Raised if validation fails.
    """
    CHOICES = [0, 10, 20, 30, 40, 50]
    if level not in CHOICES:
        raise ValueError(f"illegal log level specified: {level}")

    return level


class ExceptionLoggingEnum(Enum):
    """Enumerator for exception logging config values.

    Attributes:
        minimal: Exception title and message are logged on a single line.
        none: Exception details are not logged.
        oneline: Exception, including traceback, is logged on a single line.
        regular: The exception is logged with the entire traceback stack,
            typically on multiple lines.
    """

    minimal = "minimal"
    none = "none"
    regular = "regular"
    oneline = "oneline"


class FOCABaseConfig(BaseModel):
    """Base configuration for FOCA models."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class ServerConfig(FOCABaseConfig):
    """Model for configuration parameters of a Flask or Connexion app.

    Args:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode. If ``True``, the
            application runs in debug mode and an interactive debugger will
            be shown for unhandled exceptions, which allows executing
            arbitrary code. Only use for local development; FOCA logs a
            warning if debug mode is enabled on a host other than a loopback
            address. See Flask documentation for more details.
        testing: Enable/disable testing mode. If ``True``, exceptions are
            propagated rather than handled by the app's error handlers.

    Attributes:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode.
        testing: Enable/disable testing mode. If ``True``, exceptions are
            propagated rather than handled by the app's error handlers.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> server = ServerConfig(host="127.0.0.1", port=8000)
        >>> server.port
        8000
        >>> server.debug
        False
    """

    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False
    testing: bool = False


class ExceptionConfig(FOCABaseConfig):
    """Model for mapping exceptions to JSON problem details.

    Error responses are problem details as per RFC 9457
    (`application/problem+json`). By default, exceptions with an HTTP error
    status code (e.g., werkzeug and Connexion exceptions) are mapped to
    problem details with that status and its standard reason phrase as
    title, and all other exceptions to `500 Internal Server Error`. Use
    `mapping` to define problem details for specific exceptions (cf.
    :py:func:`foca.errors.exceptions.build_problem`).

    Args:
        mapping: Path to a dictionary mapping exception classes to problem
            details, as a dot-separated path to the module, followed by the
            name of the dictionary, e.g., ``myapp.errors.problems`` for
            dictionary ``problems`` in module ``myapp.errors`` (must be
            importable). Problem details are dictionaries with members
            `status` (HTTP status code, 400 to 599) and `title`, and any
            other members (e.g., `detail` or `type`). Exceptions that are
            not listed are handled like their closest listed base class.
        public_members: Members of problem details that are returned to
            clients. If ``None``, all members are returned. Problem details
            are logged with all members.
        logging: Specifies if and how exceptions and their problem details
            are logged.

    Attributes:
        mapping: Path to a dictionary mapping exception classes to problem
            details.
        public_members: Members of problem details that are returned to
            clients.
        logging: Specifies if and how exceptions and their problem details
            are logged.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, or the mapping cannot be imported or is invalid.

    Examples:
        >>> exceptions = ExceptionConfig(public_members=["title", "status"])
        >>> exceptions.public_members
        ['title', 'status']
        >>> exceptions.logging
        <ExceptionLoggingEnum.oneline: 'oneline'>
    """

    mapping: str | None = None
    public_members: list[str] | None = None
    logging: ExceptionLoggingEnum = ExceptionLoggingEnum.oneline
    # Problem details by exception class, imported from `mapping`
    _problems: dict[type[BaseException], dict[str, Any]] = PrivateAttr(
        default_factory=dict
    )

    @property
    def problems(self) -> dict[type[BaseException], dict[str, Any]]:
        """Problem details by exception class, imported from `mapping`."""
        return self._problems

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        """Import and validate the mapping of exceptions to problem details.

        Returns:
            Model instance with problem details imported.

        Raises:
            ValueError: The mapping cannot be imported or is not a dictionary
                mapping exception classes to problem details with an integer
                `status` between 400 and 599 and a string `title`.
        """
        if self.mapping is None:
            self._problems = {}
            return self
        module_path, _, name = self.mapping.rpartition(".")
        try:
            problems = getattr(importlib.import_module(module_path), name)
        except (ImportError, ValueError, AttributeError) as exc:
            raise ValueError(
                f"Exception mapping '{self.mapping}' could not be imported."
            ) from exc
        if not isinstance(problems, dict):
            raise ValueError(
                f"Exception mapping '{self.mapping}' is not a dictionary."
            )
        for key, problem in problems.items():
            if not (isinstance(key, type) and issubclass(key, BaseException)):
                raise ValueError(
                    f"Key '{key}' of exception mapping is not an exception "
                    "class."
                )
            if not isinstance(problem, dict):
                raise ValueError(
                    f"Problem details of exception '{key.__name__}' are not a "
                    "dictionary."
                )
            status = problem.get("status")
            if (
                not isinstance(status, int)
                or isinstance(status, bool)
                or not 400 <= status <= 599
            ):
                raise ValueError(
                    f"Problem details of exception '{key.__name__}' do not "
                    "have an integer 'status' between 400 and 599."
                )
            if not isinstance(problem.get("title"), str):
                raise ValueError(
                    f"Problem details of exception '{key.__name__}' do not "
                    "have a string 'title'."
                )
        self._problems = problems
        return self


class ConnexionOptions(BaseModel):
    """Model for options for registering an API specification with Connexion.

    Options are translated to keyword arguments of Connexion's `add_api()` in
    one place (cf. `foca.api.register_openapi._connexion_add_api_kwargs`).
    Other keys are passed through to `add_api()` as they are.

    Args:
        strict_validation: Whether to reject requests with parameters not
            defined in the specification.
        validate_responses: Whether to validate responses against the
            specification.
        swagger_ui: Whether to serve the Swagger UI.
        serve_spec: Whether to serve the specification.
        swagger_ui_path: Path at which the Swagger UI is served.
        base_path: Base path overriding that of the specification.
        resolver: Resolver of operations to functions.
        options: Further Connexion options; `swagger_ui`, `serve_spec` and
            `swagger_ui_path` take precedence.

    Attributes:
        strict_validation: Whether to reject requests with parameters not
            defined in the specification.
        validate_responses: Whether to validate responses against the
            specification.
        swagger_ui: Whether to serve the Swagger UI.
        serve_spec: Whether to serve the specification.
        swagger_ui_path: Path at which the Swagger UI is served.
        base_path: Base path overriding that of the specification.
        resolver: Resolver of operations to functions.
        options: Further Connexion options.

    Examples:
        >>> options = ConnexionOptions(swagger_ui=False)
        >>> options.swagger_ui
        False
        >>> options.base_path is None
        True
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    strict_validation: bool | None = None
    validate_responses: bool | None = None
    swagger_ui: bool | None = None
    serve_spec: bool | None = None
    swagger_ui_path: str | None = None
    base_path: str | None = None
    resolver: Any = None
    options: dict[str, Any] | None = None


class SpecConfig(FOCABaseConfig):
    """Model for an OpenAPI 2.x or 3.x specification for a Connexion app.

    Args:
        path: A single path or list of paths to OpenAPI 2.x or 3.x
            specification in YAML format.
        append: Fields to be added/modified in the root of the specification
            file. For OpenAPI 2.x, see https://swagger.io/specification/v2/.
            For OpenAPI 3.x, see https://swagger.io/specification/.
        add_operation_fields: Fields to be added/modified to/in the Operation
            Objects of each Path Info Object. An example use case for this is
            the addition or replacement of the ``x-swagger-router-controller``
            field. For OpenAPI 2.x, see
            https://swagger.io/specification/v2/#operation-object. For OpenAPI
            3.x, see https://swagger.io/specification/#operation-object. Note
            that different operation fields for different Operation Objects are
            currently not supported.
        add_security_fields: Fields to be added/modified to/in each definition
            or scheme in the ``securityDefintions`` (OpenAPI 2.x) or
            ``securitySchemes`` (OpenAPI 3.x) objects. An example use case for
            this is the addition or replacement of the ``x-tokenInfoFunc`` or
            similar field. Cf.
            https://connexion.readthedocs.io/en/latest/security.html. For
            OpenAPI 2.x, see
            https://swagger.io/specification/v2/#securityDefinitionsObject. For
            OpenAPI 3.x, see
            https://swagger.io/docs/specification/authentication/. Note that
            different security fields for different security
            definitions/schemes are currently not supported.
        disable_auth: Disable JWT validation for endpoints configured to
            require authorization as per the OpenAPI specifications. Has no
            effect if relevant security definitions/schemes are not defined.
            Setting is global. Use the ``security`` property in the OpenAPI
            specification to define this behavior separately for each
            `Operation Object` and/or security definition/scheme. For OpenAPI
            2.x, see
            https://swagger.io/specification/v2/#securityDefinitionsObject. For
            OpenAPI 3.x, see
            https://swagger.io/docs/specification/authentication/.
        connexion: Options for registering the specification with
            Connexion (cf. :py:class:`ConnexionOptions`).

    Attributes:
        path: A single path or list of paths to OpenAPI 2.x or 3.x
            specification in YAML format.
        append: Fields to be added/modified in the root of the specification
            file. For OpenAPI 2.x, see https://swagger.io/specification/v2/.
            For OpenAPI 3.x, see https://swagger.io/specification/.
        add_operation_fields: Fields to be added/modified to/in the Operation
            Objects of each Path Info Object. An example use case for this is
            the addition or replacement of the ``x-swagger-router-controller``
            field. For OpenAPI 2.x, see
            https://swagger.io/specification/v2/#operation-object. For OpenAPI
            3.x, see https://swagger.io/specification/#operation-object. Note
            that different operation fields for different Operation Objects are
            currently not supported.
        add_security_fields: Fields to be added/modified to/in each definition
            or scheme in the ``securityDefintions`` (OpenAPI 2.x) or
            ``securitySchemes`` (OpenAPI 3.x) objects. An example use case for
            this is the addition or replacement of the ``x-tokenInfoFunc`` or
            similar field. Cf.
            https://connexion.readthedocs.io/en/latest/security.html. For
            OpenAPI 2.x, see
            https://swagger.io/specification/v2/#securityDefinitionsObject. For
            OpenAPI 3.x, see
            https://swagger.io/docs/specification/authentication/. Note that
            different security fields for different security
            definitions/schemes are currently not supported.
        disable_auth: Disable JWT validation for endpoints configured to
            require authorization as per the OpenAPI specifications. Has no
            effect if relevant security definitions/schemes are not defined.
            Setting is global. Use the ``security`` property in the OpenAPI
            specification to define this behavior separately for each
            `Operation Object` and/or security definition/scheme. For OpenAPI
            2.x, see
            https://swagger.io/specification/v2/#securityDefinitionsObject. For
            OpenAPI 3.x, see
            https://swagger.io/docs/specification/authentication/.
        connexion: Options for registering the specification with
            Connexion (cf. :py:class:`ConnexionOptions`).

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> spec = SpecConfig(
        ...     path=["/path/to/specs.yaml", "/path/to/more_specs.yaml"],
        ...     add_operation_fields={
        ...         "x-my-field": "some value",
        ...     },
        ... )
        >>> [path.name for path in spec.path]
        ['specs.yaml', 'more_specs.yaml']
        >>> spec.add_operation_fields
        {'x-my-field': 'some value'}
        >>> spec.disable_auth
        False
    """

    path: Path | list[Path]
    append: list[dict] | None = None
    add_operation_fields: dict | None = None
    add_security_fields: dict | None = None
    disable_auth: bool = False
    connexion: ConnexionOptions | None = None

    @model_validator(mode="after")
    def make_paths_absolute(self, info: ValidationInfo) -> Self:
        """Make paths absolute.

        Relative paths are resolved against the directory of the base config
        file, if available, otherwise against the current working directory.

        Args:
            info: Validation info.

        Returns:
            Model instance with absolute paths.
        """
        paths = self.path if isinstance(self.path, list) else [self.path]
        self.path = [_resolve_path(path, info) for path in paths]
        return self


class APIConfig(FOCABaseConfig):
    """Model for OpenAPI 2.x or 3.x specifications for a Connexion app.

    Args:
        specs: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app.

    Attributes:
        specs: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> api = APIConfig(specs=[SpecConfig(path="/path/to/specs.yaml")])
        >>> api.specs[0].path[0].name
        'specs.yaml'
    """

    specs: list[SpecConfig] = []


class AccessControlConfig(FOCABaseConfig):
    """Model for setting access control configuration.

    For exact behaviour cf. https://github.com/casbin/pycasbin.

    Args:
        enabled: Register access control, i.e., the policy collection, the
            Casbin adapter and the permissions API. Required for all other
            options to take effect; requires trusted token issuers
            (`AuthConfig.issuers`).
        api_specs: Path to a custom API specification of the permissions
            API. If None, the default specification is used. Controllers are
            resolved via the `operationId` of each operation.
        api_swagger_ui: Serve the Swagger UI of the permissions API.
        api_serve_spec: Serve the API specification of the permissions API.
        db_name: Name of the database of the policy collection. If it is the
            name of one of the app's databases, the policy collection is
            added to that database.
        collection_name: Name of the policy collection.
        model: Path to access control model configuration file. If None,
            the use default specs.
        enforce: Enforce policies on all operations of the app's API specs
            that require authentication, unless an operation sets
            `x-foca-access-control: false`. The permissions API is always
            enforced.
        claim_identity: Claim of the validated JSON Web Token (JWT) used as
            the subject for enforcing policies. If None, use
            `AuthConfig.claim_identity`.
        subject_prefix: Prefix added to the subject in policy rules (e.g.,
            `user:alice`), so that subjects cannot collide with role names.
            Must not be empty and must not overlap with `group:`, the prefix
            of group claim values.
        claim_groups: Claim of the validated JWT listing groups or
            entitlements of the subject (e.g., `eduperson_entitlement`). If
            set, its values are used as additional subjects (roles) when
            enforcing policies, prefixed with `group:` unless
            `group_mapping` is set.
        group_mapping: Mapping of values of `claim_groups` to roles. If set,
            only mapped values are used as roles.
        resource_roles_cover_subpaths: Whether resource-scoped roles (role
            definitions with three fields, e.g., `g2 = _, _, _`) granted on
            a resource path also apply to its sub-paths (e.g., a role on
            `/services/ABC` also applies to `/services/ABC/permissions`, but
            not to `/services/ABCD`). What a role may do on a sub-path is
            still decided by policy rules.
        id_charset: Characters of generated policy rule identifiers. Only
            letters, digits, `-`, `.`, `_` and `~` are allowed, as
            identifiers are used in URL paths.
        id_length: Length of generated policy rule identifiers (4 to 64).
            Together with `id_charset`, it must allow for at least one
            million different identifiers.
            Identifiers supplied via `PUT /permissions/{id}` must have the
            same length and characters.
        self_service: Allow any authenticated subject to list and get the
            permissions that apply to them via the permissions API (rules
            whose subject is the prefixed subject, one of its roles from
            group claims, or `*`). Subjects allowed to list and get all
            permissions (e.g., admins) still get all of them. Write
            operations are not affected.
        policies: Policy rules the app relies on, each given as its policy
            type followed by its values (e.g., `["p", "*", "/pets", "GET"]`).
            If set, they are validated against the model on startup, and the
            stored rules set via the configuration are made to match them:
            missing rules are added, and rules removed from the
            configuration are removed. Configured rules cannot be changed or
            deleted via the permissions API. Other stored rules are kept. If
            not set, stored rules are not changed on startup.

    Attributes:
        enabled: Register access control.
        api_specs: Path to a custom API specification of the permissions
            API.
        api_swagger_ui: Serve the Swagger UI of the permissions API.
        api_serve_spec: Serve the API specification of the permissions API.
        db_name: Name of the database of the policy collection.
        collection_name: Name of the policy collection.
        model: Path to access control model configuration file.
        enforce: Enforce policies on all operations of the app's API specs
            that require authentication.
        claim_identity: JWT claim used as the subject for enforcing policies.
        subject_prefix: Prefix added to the subject in policy rules.
        claim_groups: JWT claim listing groups or entitlements of the subject.
        group_mapping: Mapping of values of `claim_groups` to roles.
        resource_roles_cover_subpaths: Whether resource-scoped roles apply to
            sub-paths of the resource path they were granted on.
        id_charset: Characters of generated policy rule identifiers.
        id_length: Length of generated policy rule identifiers.
        self_service: Allow subjects to list and get the permissions that
            apply to them.
        policies: Policy rules the app relies on, managed on startup.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> access_control = AccessControlConfig(
        ...     enabled=True,
        ...     db_name="access_control_db",
        ...     collection_name="policy_rules",
        ... )
        >>> access_control.enabled
        True
        >>> access_control.collection_name
        'policy_rules'
    """

    enabled: bool = False
    api_specs: str | None = None
    api_swagger_ui: bool = False
    api_serve_spec: bool = False
    db_name: str = DEFAULT_ACCESS_CONTROL_DB_NAME
    collection_name: str = DEFAULT_ACCESS_CONTROL_COLLECTION_NAME
    model: str | None = Field(default=None, validate_default=True)
    enforce: bool = False
    claim_identity: str | None = None
    subject_prefix: str = "user:"
    claim_groups: str | None = None
    group_mapping: dict[str, str] | None = None
    resource_roles_cover_subpaths: bool = True
    id_charset: str = DEFAULT_ID_CHARSET
    id_length: int = Field(default=DEFAULT_ID_LENGTH, ge=4, le=64)
    self_service: bool = False
    policies: list[list[str]] | None = None
    # Database and collection names of the policy collection added by FOCA,
    # and whether FOCA also added the database; set on registration
    _registered_collection: tuple[str, str, bool] | None = PrivateAttr(
        default=None
    )

    @field_validator("subject_prefix")
    @classmethod
    def validate_subject_prefix(cls, v: str) -> str:
        """Validate the subject prefix.

        Args:
            v: Subject prefix.

        Returns:
            Subject prefix.

        Raises:
            ValueError: The prefix is empty, so that subjects may collide with
                role names, or it overlaps with the prefix of group claim
                values, so that subjects may collide with groups.
        """
        if not v:
            raise ValueError(
                "'subject_prefix' must not be empty, as subjects could "
                "otherwise collide with role names"
            )
        if GROUP_PREFIX.startswith(v) or v.startswith(GROUP_PREFIX):
            raise ValueError(
                f"'subject_prefix' must not overlap with '{GROUP_PREFIX}', "
                "as subjects could otherwise collide with groups"
            )
        return v

    @field_validator("id_charset")
    @classmethod
    def validate_id_charset(cls, v: str) -> str:
        """Validate the characters of policy rule identifiers.

        Args:
            v: Characters of policy rule identifiers.

        Returns:
            Characters of policy rule identifiers.

        Raises:
            ValueError: No characters are given, or characters other than
                letters, digits, `-`, `.`, `_` and `~` are given.
        """
        if not v:
            raise ValueError("'id_charset' must not be empty")
        if not set(v) <= set(f"{DEFAULT_ID_CHARSET}-._~"):
            raise ValueError(
                "'id_charset' may only contain letters, digits, '-', '.', "
                "'_' and '~'"
            )
        return v

    @model_validator(mode="after")
    def validate_id_space(self) -> Self:
        """Ensure that enough different identifiers can be generated.

        Returns:
            Validated access control configuration.

        Raises:
            ValueError: `id_charset` and `id_length` allow for fewer than
                `MIN_ID_SPACE` different identifiers, so that generating
                unique identifiers could fail.
        """
        if len(set(self.id_charset)) ** self.id_length < MIN_ID_SPACE:
            raise ValueError(
                "'id_charset' and 'id_length' must allow for at least "
                f"{MIN_ID_SPACE} different identifiers"
            )
        return self

    @field_validator("model", mode="before")
    @classmethod
    def validate_model_path(cls, v: str | None, info: ValidationInfo) -> str:
        """Validate the model path.

        Args:
            v: Model path.
            info: Validation info.

        Returns:
            Absolute model path as string; relative paths are resolved
            against the directory of the base config file, if available,
            otherwise against the current working directory. If no path is
            provided, return default model path.
        """
        if v is None:
            return str(
                files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_MODEL_FILE)
            )
        return str(_resolve_path(v, info))

    @field_validator("api_specs")
    @classmethod
    def validate_api_specs_path(
        cls, v: str | None, info: ValidationInfo
    ) -> str | None:
        """Validate the path of the permissions API specification.

        Args:
            v: Path of the permissions API specification.
            info: Validation info.

        Returns:
            Absolute path as string, resolved like the model path, or `None`
            if not provided.
        """
        return None if v is None else str(_resolve_path(v, info))


class AuthConfig(FOCABaseConfig):
    """Model for configuring JSON Web Token (JWT)-based authorization.

    Bearer tokens are only accepted if issued by one of the trusted
    `issuers`. They are verified locally with the issuer's public keys,
    obtained via OpenID Connect discovery, and, if `validate_userinfo` is
    set, additionally via the issuer's user info endpoint.

    Which operations require a bearer token is defined by the security
    requirements of the operations in the API specifications (e.g., via
    `x-bearerInfoFunc: foca.security.auth.validate_token`).

    Args:
        issuers: Trusted token issuers, i.e., values of the `iss` claim of
            accepted tokens. Tokens of other issuers are rejected without
            contacting them. If empty, all tokens are rejected.
        add_key_to_claims: Whether to add the identity provider's public key
            that the token was verified with, in PEM format, to the claims.
        allow_expired: Allow/disallow expired JWTs. If ``False``, a ``401``
            authorization error is raised in response to a request containing
            an expired JWT.
        audience: List of audiences that the app identifies itself with.
            JWTs whose `aud` claim does not contain any of them are rejected.
            Required if `issuers` are set, unless `verify_audience` is
            ``False``.
        verify_audience: Whether to validate the audience of JWTs. If
            ``False``, tokens that trusted issuers issued for other services
            are accepted, too; only disable it if the issuer does not include
            the app in the audience of its tokens.
        claim_identity: The JWT claim used to identify the sender. Tokens
            without this claim are rejected.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validate_userinfo: Whether to additionally validate tokens via the
            issuer's user info endpoint, which needs to report the token's
            subject.
        timeout: Timeout (in seconds) for requests to identity providers.
        cache_ttl: Time (in seconds) for which identity providers' discovery
            documents and public keys are cached.

    Attributes:
        issuers: Trusted token issuers.
        add_key_to_claims: Whether to add the identity provider's public key
            that the token was verified with, in PEM format, to the claims.
        allow_expired: Allow/disallow expired JWTs.
        audience: List of audiences that the app identifies itself with.
        verify_audience: Whether to validate the audience of JWTs.
        claim_identity: The JWT claim used to identify the sender.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validate_userinfo: Whether to additionally validate tokens via the
            issuer's user info endpoint.
        timeout: Timeout (in seconds) for requests to identity providers.
        cache_ttl: Time (in seconds) for which identity providers' discovery
            documents and public keys are cached.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, or `issuers` are set without `audience` while
            `verify_audience` is ``True``.

    Examples:
        >>> auth = AuthConfig(
        ...     issuers=["https://login.example.org/oidc/"],
        ...     audience=["my-app"],
        ... )
        >>> auth.audience
        ['my-app']
        >>> auth.algorithms
        ['RS256']
    """

    issuers: list[str] = []
    add_key_to_claims: bool = True
    allow_expired: bool = False
    audience: list[str] | None = None
    verify_audience: bool = True
    claim_identity: str = "sub"
    algorithms: list[str] = ["RS256"]
    validate_userinfo: bool = False
    timeout: float = Field(default=10, gt=0)
    cache_ttl: float = Field(default=300, ge=0)
    # Whether warnings about the settings were logged, i.e., on the first
    # token validation
    _warnings_logged: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def validate_audience(self) -> Self:
        """Require an audience for trusted issuers, unless opted out.

        Returns:
            Model instance.

        Raises:
            ValueError: Trusted issuers are set without audience (or with
                blank audiences), and audience validation is not disabled.
        """
        has_audience = bool(self.audience) and all(
            aud.strip() for aud in self.audience or []
        )
        if self.issuers and self.verify_audience and not has_audience:
            raise ValueError(
                "Trusted token issuers require an audience: set "
                "`security.auth.audience` to the (non-blank) audiences that "
                "the issuers put into the `aud` claim of tokens for the app "
                "(e.g., its client ID), or set "
                "`security.auth.verify_audience: false` to accept tokens for "
                "any audience (not recommended)."
            )
        return self


class CORSConfig(FOCABaseConfig):
    """Model for Cross Origin Resource Sharing (CORS) configuration.

    Field names follow Starlette's `CORSMiddleware`. By default, requests
    from any origin are allowed, without credentials.

    Args:
        enabled: Enable/disable CORS for the application.
        allow_origins: Origins allowed to make cross-origin requests (e.g.,
            ``https://example.org``), or ``*`` for any origin.
        allow_methods: HTTP methods allowed in cross-origin requests.
        allow_headers: Request headers allowed in cross-origin requests, or
            ``*`` for any header.
        expose_headers: Response headers exposed to the browser.
        allow_credentials: Whether cross-origin requests may include
            credentials (e.g., cookies). Requires explicit `allow_origins`.
        max_age: Time (in seconds) for which browsers may cache preflight
            responses. If ``None``, no `Access-Control-Max-Age` header is
            sent.

    Attributes:
        enabled: Enable/disable CORS for the application.
        allow_origins: Origins allowed to make cross-origin requests.
        allow_methods: HTTP methods allowed in cross-origin requests.
        allow_headers: Request headers allowed in cross-origin requests.
        expose_headers: Response headers exposed to the browser.
        allow_credentials: Whether cross-origin requests may include
            credentials.
        max_age: Time (in seconds) for which browsers may cache preflight
            responses.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, or credentials are allowed for any origin.

    Examples:
        >>> cors = CORSConfig(
        ...     allow_origins=["https://example.org"],
        ...     allow_credentials=True,
        ... )
        >>> cors.allow_origins
        ['https://example.org']
        >>> cors.allow_headers
        ['*']
    """

    enabled: bool = True
    allow_origins: list[str] = ["*"]
    allow_methods: list[str] = [
        "GET",
        "HEAD",
        "POST",
        "OPTIONS",
        "PUT",
        "PATCH",
        "DELETE",
    ]
    allow_headers: list[str] = ["*"]
    expose_headers: list[str] = []
    allow_credentials: bool = False
    max_age: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        """Ensure that credentials are not allowed for any origin.

        Returns:
            Validated CORS configuration.

        Raises:
            ValueError: Credentials are allowed for any origin (``*``), which
                would let any website make authenticated requests on behalf
                of users.
        """
        if self.allow_credentials and "*" in self.allow_origins:
            raise ValueError(
                "'allow_credentials' requires explicit 'allow_origins' "
                "instead of '*'"
            )
        return self


class SecurityConfig(FOCABaseConfig):
    """Model for list the Security configuration.

    Args:
        access_control: Config parameters for Access Control.
        auth: Config parameters for JSON Web Token (JWT) validation.
        cors: Config parameters for CORS.

    Attributes:
        access_control: Config parameters for Access Control.
        auth: Config parameters for JSON Web Token (JWT) validation.
        cors: Config parameters for CORS.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, access control options are set while access control
            is not enabled, or access control is enabled without trusted
            token issuers.

    Examples:
        >>> security = SecurityConfig(cors=CORSConfig(enabled=False))
        >>> security.cors.enabled
        False
        >>> security.access_control.enabled
        False
    """

    access_control: AccessControlConfig = AccessControlConfig()
    auth: AuthConfig = AuthConfig()
    cors: CORSConfig = CORSConfig()

    @model_validator(mode="after")
    def validate_access_control(self) -> Self:
        """Ensure that access control settings are consistent.

        Returns:
            Validated security configuration.

        Raises:
            ValueError: Access control options are set while access control
                is not enabled, in which case they would not take effect
                (e.g., policies would not be enforced), or access control is
                enabled without trusted token issuers, in which case all
                requests to the permissions API and to enforced operations
                would be rejected.
        """
        access_control = self.access_control
        if not access_control.enabled:
            options = [
                name
                for name, value in [
                    ("enforce", access_control.enforce),
                    ("api_specs", access_control.api_specs),
                    ("self_service", access_control.self_service),
                    ("policies", access_control.policies),
                ]
                if value
            ]
            if options:
                raise ValueError(
                    "Access control options require "
                    f"'access_control.enabled': {', '.join(options)}"
                )
        elif not self.auth.issuers:
            raise ValueError(
                "'access_control.enabled' requires trusted token issuers "
                "('auth.issuers')"
            )
        return self


class IndexConfig(FOCABaseConfig):
    """Model for configuring indexes for a MongoDB collection.

    Args:
        keys: A key-direction dictionary indicating the field to be indexed
            and the sort order of that index.
        options: A dictionary of any additional index creation options. cf.:
            https://pymongo.readthedocs.io/en/3.10.1/api/pymongo/collection.html

    Attributes:
        keys: A key-direction dictionary indicating the field to be indexed
            and the sort order of that index.
        options: A dictionary of any additional index creation options. cf.:
            https://pymongo.readthedocs.io/en/3.10.1/api/pymongo/collection.html

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> index = IndexConfig(keys={"name": -1}, options={"unique": True})
        >>> index.keys
        [('name', -1)]
        >>> index.options
        {'unique': True}
    """

    keys: dict | list[tuple] | None = None
    options: dict = {}

    @field_validator("keys", mode="after")
    @classmethod
    def keys_to_list(
        cls, v: dict | list[tuple] | None
    ) -> dict | list[tuple] | None:
        """Convert a dictionary of keys into a list of tuples."""
        if v is not None and isinstance(v, dict):
            v = [(key, val) for key, val in v.items()]
        return v


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

    Examples:
        >>> collection = CollectionConfig(
        ...     indexes=[IndexConfig(keys={"last_name": 1})],
        ... )
        >>> collection.indexes[0].keys
        [('last_name', 1)]
    """

    indexes: list[IndexConfig] | None = None
    client: collection.Collection | None = None


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

    Examples:
        >>> db = DBConfig(
        ...     collections={
        ...         "my_collection": CollectionConfig(
        ...             indexes=[IndexConfig(keys={"last_name": 1})],
        ...         ),
        ...     },
        ... )
        >>> list(db.collections)
        ['my_collection']
    """

    collections: dict[str, CollectionConfig] | None = None
    client: database.Database | None = None


class MongoConfig(FOCABaseConfig):
    """Model for a MongoDB instance attached to a Flask/Connexion app.

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

    Examples:
        >>> mongo = MongoConfig(host="mongodb", port=27017)
        >>> mongo.host
        'mongodb'
        >>> mongo.dbs is None
        True
    """

    host: str = "mongodb"
    port: int = 27017
    dbs: dict[str, DBConfig] | None = None
    # MongoDB client shared by all databases; set on registration
    _client: MongoClient | None = PrivateAttr(default=None)


class JobsConfig(FOCABaseConfig):
    """Model for configuring RabbitMQ broker and Celery client and tasks.

    The broker, client and tasks are attached to a Flask/Connexion app.

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

    Examples:
        >>> jobs = JobsConfig(host="rabbitmq", include=["myapp.tasks"])
        >>> jobs.include
        ['myapp.tasks']
        >>> jobs.backend
        'rpc://'
    """

    host: str = "rabbitmq"
    port: int = 5672
    backend: str = "rpc://"
    include: list[str] | None = None


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

    Examples:
        >>> formatter = LogFormatterConfig(format="{levelname}: {message}")
        >>> formatter.format
        '{levelname}: {message}'
        >>> formatter.class_formatter
        'logging.Formatter'
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

    Examples:
        >>> import logging
        >>> handler = LogHandlerConfig(level=logging.WARNING)
        >>> handler.level
        30
        >>> handler.stream
        'ext://sys.stderr'
    """

    class_handler: str = Field(
        "logging.StreamHandler",
        alias="class",
    )
    level: int = 20
    formatter: str = "standard"
    stream: str = "ext://sys.stderr"

    _validate_level = field_validator("level")(_validate_log_level_choices)


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

    Examples:
        >>> import logging
        >>> root = LogRootConfig(level=logging.INFO, handlers=["console"])
        >>> root.level
        20
        >>> root.handlers
        ['console']
    """

    level: int = 10
    handlers: list[str] | None = ["console"]

    _validate_level = field_validator("level")(_validate_log_level_choices)


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

    Examples:
        >>> import logging
        >>> log = LogConfig(root=LogRootConfig(level=logging.WARNING))
        >>> log.root.level
        30
        >>> list(log.handlers)
        ['console']
    """

    version: int = 1
    disable_existing_loggers: bool = False
    formatters: dict[str, LogFormatterConfig] | None = {
        # Validated from data, as the `class` alias is not a constructor
        # argument for type checkers
        "standard": LogFormatterConfig.model_validate({}),
    }
    handlers: dict[str, LogHandlerConfig] | None = {
        "console": LogHandlerConfig.model_validate({}),
    }
    root: LogRootConfig | None = LogRootConfig()


class Config(FOCABaseConfig):
    """Model for all app configuration parameters.

    Args:
        server: Server config parameters.
        exceptions: Exception handling parameters.
        api: OpenAPI specification config parameters.
        security: Security config parameters.
        db: Database config parameters.
        jobs: Background job config parameters.
        log: Logger config parameters.

    Attributes:
        server: Server config parameters.
        exceptions: Exception handling parameters.
        api: OpenAPI specification config parameters.
        security: Security config parameters.
        db: Database config parameters.
        jobs: Background job config parameters.
        log: Logger config parameters.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> config = Config(server=ServerConfig(port=8000))
        >>> config.server.port
        8000
        >>> config.security.auth.issuers
        []
        >>> config.db is None
        True
    """

    server: ServerConfig = ServerConfig()
    exceptions: ExceptionConfig = ExceptionConfig()
    api: APIConfig = APIConfig()
    security: SecurityConfig = SecurityConfig()
    db: MongoConfig | None = None
    jobs: JobsConfig | None = None
    log: LogConfig = LogConfig()
    model_config = ConfigDict(extra="allow")
