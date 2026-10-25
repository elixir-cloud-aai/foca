"""FOCA config models."""

import importlib
import re
from collections.abc import Callable, Mapping
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import (
    Any,
    Self,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    PrivateAttr,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, collection, database

from foca.plugins.config import PluginsConfig
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    ADMIN_ROLE,
    DEFAULT_ACCESS_CONTROL_COLLECTION_NAME,
    DEFAULT_ACCESS_CONTROL_DB_NAME,
    DEFAULT_ID_CHARSET,
    DEFAULT_ID_LENGTH,
    DEFAULT_MODEL_FILE,
    MAINTAINER_ROLE,
    MIN_ID_SPACE,
    OWNER_ROLE,
    SUBJECT_TYPE_PREFIXES,
)
from foca.utils.service_info import (
    import_runtime_function,
    lookup_package_version,
    merge_service_info,
    validate_service_info,
)
from foca.utils.urls import HTTP_SCHEMES, normalize_url

# Default media type of error responses
DEFAULT_ERROR_MEDIA_TYPE = "application/problem+json"
# JSON media types of error responses: `type/json` or `type/subtype+json`,
# with characters as per RFC 6838, section 4.2; without parameters, as JSON
# defines none (RFC 8259, section 11)
_JSON_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]*/(?:[a-z0-9][a-z0-9!#$&^_.+-]*\+)?json"
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


def _validate_log_level_choices(cls: type[BaseModel], level: int) -> int:
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


HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
"""Pattern of HTTP header names (tokens, RFC 9110)."""


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
        max_content_length: Maximum size of request bodies in bytes, set as
            Flask's `MAX_CONTENT_LENGTH`; larger bodies are rejected with
            `413` before requests are handled, also if their size is not
            declared (e.g., chunked bodies, which are read up to one byte
            more than the limit). If ``None``, bodies are not limited by the
            app. As bodies may be read before requests are authenticated,
            limit them here or in a reverse proxy or ingress.
        public_url: Absolute `http` or `https` URL of the app as clients see
            it, including any path prefix of a reverse proxy or ingress
            (e.g., ``https://api.example.org/my-app/``), but not the root
            path of the app (`SCRIPT_NAME`), which
            :py:func:`foca.utils.urls.get_public_url` appends. Checked
            like by :py:func:`foca.utils.urls.check_url` (no user info) and
            normalized (cf. :py:func:`foca.utils.urls.normalize_url`; e.g.,
            no trailing slash); must not have a query or fragment. If
            ``None``, the app has no public URL, and
            :py:func:`foca.utils.urls.get_public_url` raises an error.

    Attributes:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode.
        testing: Enable/disable testing mode. If ``True``, exceptions are
            propagated rather than handled by the app's error handlers.
        max_content_length: Maximum size of request bodies in bytes.
        public_url: Public URL of the app, normalized.

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

    host: str = "0.0.0.0"  # noqa: S104 (all interfaces, as in containers)
    port: int = 8080
    debug: bool = False
    testing: bool = False
    max_content_length: PositiveInt | None = None
    public_url: str | None = None

    @field_validator("public_url")
    @classmethod
    def _normalize_public_url(cls, url: str | None) -> str | None:
        """Check and normalize the public URL of the app.

        Args:
            url: Public URL, or ``None``.

        Returns:
            Normalized public URL, or ``None``.

        Raises:
            ValueError: The URL is invalid (cf.
                :py:func:`foca.utils.urls.check_url`), not an `http` or
                `https` URL, or has a query or fragment (also an empty one).
        """
        if url is None:
            return None
        name = "'server.public_url'"
        # Checked before the query and fragment, which are split off at the
        # first `?` and `#` only once the URL is known to be valid
        normalized = normalize_url(url, schemes=HTTP_SCHEMES, name=name)
        if "?" in url or "#" in url:
            raise ValueError(f"{name} must not have a query or fragment.")
        return normalized


class ExceptionConfig(FOCABaseConfig):
    """Model for mapping exceptions to JSON problem details.

    Error responses are problem details as per RFC 9457, by default with
    media type `application/problem+json` (cf. `media_type`). By default,
    exceptions with an HTTP error status code (e.g., werkzeug and Connexion
    exceptions) are mapped to problem details with that status and its
    standard reason phrase as title (as per RFC 9110 for `413`, `414`, `416`
    and `422` on all Python versions), and all other exceptions to `500
    Internal Server Error`. Use `mapping` to define
    problem details for specific exceptions (cf.
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
        media_type: Media type (`Content-Type`) of all error responses, as
            `type/subtype`, with subtype `json` or suffix `+json`, e.g.,
            ``application/json`` for APIs whose specifications declare it for
            error responses (as many GA4GH API specifications do). The body
            is the same for all media types.
        validation_details: Whether responses to requests rejected by
            Connexion's validation (`BadRequestProblem` and
            `ExtraParameterProblem`) get the reason of the rejection (e.g.,
            the validator's message) as `detail`, instead of the problem
            details mapped to the exception class. Defaults to true: the
            reason is also logged (at `INFO`, shortened and escaped) instead
            of the exception, and the request validation messages of
            Connexion's validation logger (`connexion.decorators.validation`)
            are filtered out, as they have the validator's message in full
            and unescaped. If false, responses get the mapped problem
            details, and the exception is logged.
        max_detail_length: Maximum length of request-specific details in
            responses (cf. `validation_details` and
            :py:func:`foca.errors.exceptions.problem_response`) and log
            messages (cf. :py:func:`foca.errors.exceptions.shorten_detail`);
            longer details are shortened in the middle.

    Attributes:
        mapping: Path to a dictionary mapping exception classes to problem
            details.
        public_members: Members of problem details that are returned to
            clients.
        logging: Specifies if and how exceptions and their problem details
            are logged.
        media_type: Media type of all error responses, in lower case.
        validation_details: Whether responses to requests rejected by
            Connexion's validation get the reason as `detail`.
        max_detail_length: Maximum length of request-specific details in
            responses and log messages.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, the mapping cannot be imported or is invalid, or the
            media type is not a JSON media type.

    Examples:
        >>> exceptions = ExceptionConfig(public_members=["title", "status"])
        >>> exceptions.public_members
        ['title', 'status']
        >>> exceptions.logging
        <ExceptionLoggingEnum.oneline: 'oneline'>
        >>> exceptions.media_type
        'application/problem+json'
        >>> exceptions.validation_details
        True
        >>> exceptions.max_detail_length
        500
    """

    mapping: str | None = None
    public_members: list[str] | None = None
    logging: ExceptionLoggingEnum = ExceptionLoggingEnum.oneline
    media_type: str = DEFAULT_ERROR_MEDIA_TYPE
    validation_details: bool = True
    max_detail_length: PositiveInt = 500
    # Problem details by exception class, imported from `mapping`
    _problems: dict[type[BaseException], dict[str, Any]] = PrivateAttr(
        default_factory=dict
    )

    @property
    def problems(self) -> dict[type[BaseException], dict[str, Any]]:
        """Problem details by exception class, imported from `mapping`."""
        return self._problems

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, v: str) -> str:
        """Validate the media type of error responses.

        Args:
            v: Media type.

        Returns:
            Media type, in lower case (media types are case-insensitive).

        Raises:
            ValueError: The media type is not of the form `type/subtype`,
                with subtype `json` or suffix `+json`, or has parameters.
        """
        v = v.lower()
        if not _JSON_MEDIA_TYPE.fullmatch(v):
            raise ValueError(
                "'media_type' must be a JSON media type without parameters, "
                "i.e., 'type/json' or 'type/subtype+json' (e.g., "
                "'application/problem+json' or 'application/json'), not "
                f"'{v}'"
            )
        return v

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
            raise ValueError(f"Exception mapping '{self.mapping}' is not a dictionary.")
        for key, problem in problems.items():
            if not (isinstance(key, type) and issubclass(key, BaseException)):
                raise ValueError(
                    f"Key '{key}' of exception mapping is not an exception class."
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
        swagger_ui: Whether to serve the Swagger UI (Connexion's default:
            `True`). The UI needs the specification, so it is not served if
            `serve_spec` is disabled, and enabling the UI while disabling
            `serve_spec` is rejected.
        serve_spec: Whether to serve the specification (Connexion's default:
            `True`).
        swagger_ui_path: Path of the Swagger UI, relative to the base path
            (Connexion's default: `/ui`); must not be empty or `/`.
        base_path: Base path overriding that of the specification.
        resolver: Resolver of operations to functions.
        options: Further Connexion options; `swagger_ui`, `serve_spec` and
            `swagger_ui_path` (Connexion's `swagger_url`) take precedence.

    Attributes:
        strict_validation: Whether to reject requests with parameters not
            defined in the specification.
        validate_responses: Whether to validate responses against the
            specification.
        swagger_ui: Whether to serve the Swagger UI.
        serve_spec: Whether to serve the specification.
        swagger_ui_path: Path of the Swagger UI, relative to the base path
            (Connexion's default: `/ui`); must not be empty or `/`.
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
    # Not empty or `/` only, as Connexion would serve the UI's static files
    # for all paths under the base path
    swagger_ui_path: str | None = Field(default=None, pattern=r"[^/]")
    base_path: str | None = None
    resolver: Any = None
    options: dict[str, Any] | None = None

    def _set_options(self) -> dict[str, Any]:
        """Get the further Connexion options that are set.

        Returns:
            `options` without `None` values, which Connexion ignores.
        """
        return {
            key: value
            for key, value in (self.options or {}).items()
            if value is not None
        }

    def _spec_disabled(self) -> bool:
        """Check whether serving the specification is disabled.

        As Connexion does, `options` may disable it via `serve_spec` or its
        deprecated name `swagger_json`; the field takes precedence.

        Returns:
            Whether serving the specification is set and disabled.
        """
        options = self._set_options()
        serve_spec = self.serve_spec
        if serve_spec is None:
            serve_spec = options.get("serve_spec", options.get("swagger_json"))
        return serve_spec is not None and not serve_spec

    @model_validator(mode="after")
    def validate_swagger_ui(self) -> Self:
        """Ensure that the specification is served if the Swagger UI is.

        Returns:
            Validated options.

        Raises:
            ValueError: The Swagger UI is enabled, but serving the
                specification is disabled.
        """
        swagger_ui = self.swagger_ui
        if swagger_ui is None:
            swagger_ui = self._set_options().get("swagger_ui")
        if swagger_ui and self._spec_disabled():
            raise ValueError(
                "The Swagger UI ('swagger_ui') needs the specification, so "
                "'serve_spec' must not be disabled"
            )
        return self


class SpecConfig(FOCABaseConfig):
    """Model for an OpenAPI 2.x or 3.x specification for a Connexion app.

    Args:
        path: A single path or list of paths to OpenAPI 2.x or 3.x
            specification in YAML format.
        append: Fields to be added/modified in the root of the specification
            file. For OpenAPI 2.x, see https://swagger.io/specification/v2/.
            For OpenAPI 3.x, see https://swagger.io/specification/.
        add_operation_fields: Fields to be added to the Operation Objects of
            each Path Info Object, unless an operation sets them itself. An
            example use case for this is the addition of the
            ``x-openapi-router-controller`` field, so that operations can
            still be routed to other controllers individually. For OpenAPI
            2.x, see https://swagger.io/specification/v2/#operation-object.
            For OpenAPI 3.x, see
            https://swagger.io/specification/#operation-object.
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
        name: Name of the specification, used in logs. Set from the key if
            `APIConfig.specs` is given as a mapping of named specifications,
            so that config overlays can change single specifications.
            Must be unique.
        patches: Paths to YAML or JSON files (resolved like those of `path`)
            or mappings, deep-merged into the specification in order after
            `path` and `append`, with the same rules as `path`. Meant for
            operators; patches can weaken or remove authentication and
            access control. Cf. the documentation on patching
            specifications.

    Attributes:
        path: A single path or list of paths to OpenAPI 2.x or 3.x
            specification in YAML format.
        append: Fields to be added/modified in the root of the specification
            file. For OpenAPI 2.x, see https://swagger.io/specification/v2/.
            For OpenAPI 3.x, see https://swagger.io/specification/.
        add_operation_fields: Fields to be added to the Operation Objects of
            each Path Info Object, unless an operation sets them itself. An
            example use case for this is the addition of the
            ``x-openapi-router-controller`` field, so that operations can
            still be routed to other controllers individually. For OpenAPI
            2.x, see https://swagger.io/specification/v2/#operation-object.
            For OpenAPI 3.x, see
            https://swagger.io/specification/#operation-object.
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
        name: Name of the specification.
        patches: Patches deep-merged into the specification, given as
            absolute paths or mappings.

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
    append: list[dict[str, Any]] | None = None
    add_operation_fields: dict[str, Any] | None = None
    add_security_fields: dict[str, Any] | None = None
    disable_auth: bool = False
    connexion: ConnexionOptions | None = None
    name: str | None = Field(default=None, min_length=1)
    patches: list[Path | dict[str, Any]] | None = None

    @model_validator(mode="after")
    def make_paths_absolute(self, info: ValidationInfo) -> Self:
        """Make paths absolute.

        Relative paths of `path` and `patches` are resolved against the
        directory of the base config file, if available, otherwise against
        the current working directory.

        Args:
            info: Validation info.

        Returns:
            Model instance with absolute paths.
        """
        paths = self.path if isinstance(self.path, list) else [self.path]
        self.path = [_resolve_path(path, info) for path in paths]
        if self.patches is not None:
            self.patches = [
                _resolve_path(patch, info) if isinstance(patch, Path) else patch
                for patch in self.patches
            ]
        return self


class APIConfig(FOCABaseConfig):
    """Model for OpenAPI 2.x or 3.x specifications for a Connexion app.

    Args:
        specs: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app, or a mapping
            of names to such parameters (named specifications), so that
            config overlays can change single specifications (cf.
            :py:class:`foca.config.config_parser.ConfigParser`); `None`
            values remove specifications. Names are set as
            `SpecConfig.name`.

    Attributes:
        specs: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app, in order.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Examples:
        >>> api = APIConfig(specs=[SpecConfig(path="/path/to/specs.yaml")])
        >>> api.specs[0].path[0].name
        'specs.yaml'
        >>> api = APIConfig.model_validate(
        ...     {"specs": {"pets": {"path": "/path/to/specs.yaml"}}}
        ... )
        >>> api.specs[0].name
        'pets'
    """

    specs: list[SpecConfig] = []

    @field_validator("specs", mode="before")
    @classmethod
    def named_specs_to_list(cls, v: Any) -> Any:
        """Convert named specifications to a list.

        Args:
            v: Specifications, as a list or a mapping of names to
                specifications.

        Returns:
            Specifications, with names set from the keys of a mapping, in
            order; specifications set to `None` are dropped. Other values
            are returned as they are, for validation.

        Raises:
            ValueError: A named specification has no `path`, e.g., because an
                overlay changes a specification under a misspelled name, or
                its `name` differs from its key.
        """
        if not isinstance(v, dict):
            return v
        specs: list[Any] = []
        for name, spec in v.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"Names of API specs must be non-empty strings: {name!r}"
                )
            if spec is None:
                continue
            if isinstance(spec, dict) and "path" not in spec:
                raise ValueError(
                    f"API spec '{name}' has no 'path'; to change a spec of "
                    "the app, use its name in the app's configuration"
                )
            if not isinstance(spec, (dict, SpecConfig)):
                # invalid, rejected on validation
                specs.append(spec)
                continue
            given = spec.get("name") if isinstance(spec, dict) else spec.name
            if given not in (None, name):
                raise ValueError(f"API spec '{name}' must not set a different 'name'")
            specs.append(
                {**spec, "name": name}
                if isinstance(spec, dict)
                else spec.model_copy(update={"name": name})
            )
        return specs

    @field_validator("specs")
    @classmethod
    def validate_unique_names(cls, v: list[SpecConfig]) -> list[SpecConfig]:
        """Ensure that names of specifications are unique.

        Args:
            v: Specifications.

        Returns:
            Specifications.

        Raises:
            ValueError: Several specifications have the same name.
        """
        names = [spec.name for spec in v if spec.name is not None]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"Names of API specs must be unique: {', '.join(duplicates)}"
            )
        return v


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
        api_swagger_ui: Serve the Swagger UI of the permissions API, and
            its specification, which the UI needs (`api_serve_spec` must
            then not be set to `False`).
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
            `group_mapping` is set. Must not have surrounding whitespace, as
            the claim is looked up as written (the app refuses to start,
            `ValueError`).
        group_mapping: Mapping of values of `claim_groups` to roles. If set,
            only mapped values are used as roles, unless
            `keep_group_subjects` is set. If it maps any value,
            `claim_groups` must be set (the app refuses to start,
            `ValueError`). Roles must not be roles of
            `resource_roles` (mapped roles apply to every resource) and must
            not start with `subject_prefix`, `group:` or `client:`.
        keep_group_subjects: Whether to also use all values of
            `claim_groups`, prefixed with `group:`, as subjects if
            `group_mapping` is set, i.e., in addition to the roles they are
            mapped to (e.g., to grant resource-scoped roles to groups). Has
            no effect without `group_mapping`, as values are then always
            used as such.
        opt_in_roles: Roles of `group_mapping` that apply only to requests
            that ask for them in the header `opt_in_header` (e.g., `admin`,
            so that admins act as regular users unless they opt in). Asking
            for a role grants nothing that the token's groups do not
            entitle to. Roles of service clients (`client_mapping`) always
            apply. Rules must not grant opt-in roles (e.g., `[g,
            "user:alice", admin]`), except to other opt-in roles, as roles
            granted by rules always apply: configured rules of `policies`
            are refused, as are such rules via the permissions API, and
            stored ones are warned about. If empty (default), all mapped
            roles always apply.
        opt_in_header: Request header in which requests ask for opt-in
            roles, as a comma-separated list (e.g., `X-Request-Roles:
            admin`). Must be a valid header name without `_` (which servers
            such as Gunicorn drop), other than `auth.on_behalf_of_header`.
        require_admin: Refuse to start (`ValueError`) if no group (via
            `group_mapping`, with `claim_groups` set) or service client (via
            `client_mapping`) is mapped to the role `admin` and no rule of
            `policies` that applies grants it, instead of logging a warning.
            Roles granted via stored policy rules (e.g., via the permissions
            API) are not checked.
        client_mapping: Mapping of service clients (client IDs of
            `AuthConfig.service_clients`) to roles. Requests with service
            tokens are enforced with the subject `client:<client ID>` and
            the role of the client, if mapped. Clients that are not service
            clients cannot be mapped. Roles are restricted as for
            `group_mapping`.
        resource_roles_cover_subpaths: Whether resource-scoped roles (role
            definitions with three fields, e.g., `g2 = _, _, _`) granted on
            a resource path also apply to its sub-paths (e.g., a role on
            `/services/ABC` also applies to `/services/ABC/permissions`, but
            not to `/services/ABCD`). What a role may do on a sub-path is
            still decided by policy rules.
        resource_roles: Roles on resources that the resource ownership
            helpers (cf. :py:mod:`foca.security.access_control.ownership`)
            grant and revoke. Must contain `owner`, the role of the creator
            of a resource. Roles must be valid values of policy rules, must
            not be `admin` and must not start with `subject_prefix`,
            `group:` or `client:`.
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
            not set, stored configured rules are removed on startup (with a
            warning); the app then refuses to start without the database, as
            with policies set. Given as a list, or as a mapping of names
            to rules (named rules), so that config overlays can add, change
            or remove single rules (cf.
            :py:class:`foca.config.config_parser.ConfigParser`); `None`
            values remove rules.
        log_traffic: Whether requests to the permissions API and their
            responses are logged (cf.
            :py:func:`foca.utils.logging.log_traffic`).

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
        keep_group_subjects: Whether to also use values of `claim_groups`
            as subjects if `group_mapping` is set.
        opt_in_roles: Roles of `group_mapping` that apply only to requests
            that ask for them.
        opt_in_header: Request header in which requests ask for opt-in
            roles.
        require_admin: Refuse to start if no group, service client or
            configured policy rule is granted the role `admin`.
        client_mapping: Mapping of service clients to roles.
        resource_roles_cover_subpaths: Whether resource-scoped roles apply to
            sub-paths of the resource path they were granted on.
        resource_roles: Roles on resources managed by the resource ownership
            helpers.
        id_charset: Characters of generated policy rule identifiers.
        id_length: Length of generated policy rule identifiers.
        self_service: Allow subjects to list and get the permissions that
            apply to them.
        policies: Policy rules the app relies on, managed on startup, as a
            list (names of named rules are kept in `named_policies`). If not
            set, stored configured rules are removed on startup.
        log_traffic: Whether requests to the permissions API and their
            responses are logged.

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
    keep_group_subjects: bool = False
    opt_in_roles: list[str] = []
    opt_in_header: str = "X-Request-Roles"
    require_admin: bool = False
    client_mapping: dict[str, str] | None = None
    resource_roles_cover_subpaths: bool = True
    resource_roles: list[str] = Field(
        default_factory=lambda: [OWNER_ROLE, MAINTAINER_ROLE]
    )
    id_charset: str = DEFAULT_ID_CHARSET
    id_length: int = Field(default=DEFAULT_ID_LENGTH, ge=4, le=64)
    self_service: bool = False
    policies: list[list[str]] | None = None
    log_traffic: bool = True
    # Database and collection names of the policy collection added by FOCA,
    # and whether FOCA also added the database; set on registration
    _registered_collection: tuple[str, str, bool] | None = PrivateAttr(default=None)
    # Named rules, if `policies` was given as named rules
    _named_policies: dict[str, list[str]] | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def named_policies_to_list(
        cls, data: Any, handler: ValidatorFunctionWrapHandler
    ) -> Any:
        """Convert named policy rules to a list, keeping their names.

        Args:
            data: Input data.
            handler: Validation handler.

        Returns:
            Validated access control configuration; named rules set to
            `None` are dropped.

        Raises:
            ValueError: Names of rules are not non-empty strings, or rules
                are not lists of strings.
        """
        named_policies: dict[str, list[str]] | None = None
        if isinstance(data, dict) and isinstance(data.get("policies"), dict):
            named: dict[str, list[str]] = {}
            for name, rule in data["policies"].items():
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"Names of policy rules must be non-empty strings: {name!r}"
                    )
                if rule is None:
                    continue
                if not isinstance(rule, list) or not all(
                    isinstance(value, str) for value in rule
                ):
                    raise ValueError(f"Policy rule '{name}' must be a list of strings")
                named[name] = rule
            data = {**data, "policies": list(named.values())}
            named_policies = named
        model = handler(data)
        if named_policies is not None:
            model._named_policies = {
                name: list(rule) for name, rule in named_policies.items()
            }
        return model

    @property
    def named_policies(self) -> dict[str, list[str]] | None:
        """Named policy rules, if `policies` was given as named rules.

        `None` if the rules were given as a list, or if they were changed
        after validation, so that the names no longer apply.
        """
        named = self._named_policies
        if named is None or list(named.values()) != self.policies:
            return None
        return {name: list(rule) for name, rule in named.items()}

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
                values (`group:`) or service clients (`client:`), so that
                subjects may collide with groups or clients.
        """
        if not v:
            raise ValueError(
                "'subject_prefix' must not be empty, as subjects could "
                "otherwise collide with role names"
            )
        for subject_type, prefix in SUBJECT_TYPE_PREFIXES.items():
            if prefix.startswith(v) or v.startswith(prefix):
                raise ValueError(
                    f"'subject_prefix' must not overlap with '{prefix}', as "
                    f"subjects could otherwise collide with {subject_type}s"
                )
        return v

    @model_validator(mode="after")
    def validate_swagger_ui(self) -> Self:
        """Ensure that the specification is served if the Swagger UI is.

        Returns:
            Validated access control configuration.

        Raises:
            ValueError: `api_swagger_ui` is enabled, but `api_serve_spec` is
                set to `False` (the specification is served with the UI
                unless it is set).
        """
        if (
            self.api_swagger_ui
            and not self.api_serve_spec
            and "api_serve_spec" in self.model_fields_set
        ):
            raise ValueError(
                "The Swagger UI of the permissions API ('api_swagger_ui') "
                "needs the specification, so 'api_serve_spec' must not be "
                "disabled"
            )
        return self

    @model_validator(mode="after")
    def validate_mapped_roles(self) -> Self:
        """Ensure that roles mapped from group claims or clients are global roles.

        Returns:
            Validated access control configuration.

        Raises:
            ValueError: A role of `group_mapping` or `client_mapping` is one
                of `resource_roles`, so that the mapped groups or clients
                would have that role on every resource (a mapped role is a
                subject of its own, which matches the policy rules of the
                role on all resources), or it starts with `subject_prefix`,
                `group:` or `client:`, so that the mapped groups or clients
                would get the rights of that subject.
        """
        prefixes = [self.subject_prefix, *SUBJECT_TYPE_PREFIXES.values()]
        for name, mapping in (
            ("group_mapping", self.group_mapping),
            ("client_mapping", self.client_mapping),
        ):
            for role in (mapping or {}).values():
                if role in self.resource_roles:
                    raise ValueError(
                        f"'{name}' must not map to '{role}', a role of "
                        "'resource_roles', as mapped subjects would otherwise "
                        "have that role on every resource"
                    )
                for prefix in prefixes:
                    if role.startswith(prefix):
                        raise ValueError(
                            f"Roles of '{name}' must not start with '{prefix}', "
                            "the prefix of subjects, as mapped subjects would "
                            "otherwise get the rights of that subject"
                        )
        return self

    @field_validator("opt_in_header")
    @classmethod
    def validate_opt_in_header(cls, v: str) -> str:
        """Validate the header of opt-in roles.

        Args:
            v: Header name.

        Returns:
            Header name.

        Raises:
            ValueError: The name is no valid header name (an HTTP token), or
                it contains `_`, as servers such as Gunicorn drop such
                headers.
        """
        if not HEADER_NAME.fullmatch(v) or "_" in v:
            raise ValueError(
                f"'opt_in_header' is no valid header name (without '_'): {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_opt_in_roles(self) -> Self:
        """Ensure that opt-in roles apply only to requests that ask for them.

        Returns:
            Validated access control configuration.

        Raises:
            ValueError: An opt-in role is not a role of `group_mapping`, so
                that nobody could opt in to it, or a configured role rule of
                `policies` (`g`, also resource-scoped, e.g., `g2`) grants an
                opt-in role to a subject that is not an opt-in role itself
                (e.g., `[g, "user:alice", admin]`), as roles granted by rules
                always apply. Rules between opt-in roles (e.g., `[g,
                superadmin, admin]`, both opt-in) are allowed.
        """
        mapped = set((self.group_mapping or {}).values())
        unmapped = [role for role in self.opt_in_roles if role not in mapped]
        if unmapped:
            raise ValueError(
                "'opt_in_roles' must be roles of 'group_mapping', as only "
                f"roles from group claims can be opted in to: {', '.join(unmapped)}"
            )
        opt_in = set(self.opt_in_roles)
        for rule in self.policies or []:
            if (
                len(rule) >= 3
                and rule[0].startswith("g")
                and rule[2] in opt_in
                and rule[1] not in opt_in
            ):
                raise ValueError(
                    f"Configured rules must not grant the opt-in role "
                    f"'{rule[2]}' ('opt_in_roles') to '{rule[1]}', as roles "
                    "granted by rules always apply: map groups to it "
                    "('group_mapping') instead"
                )
        return self

    @model_validator(mode="after")
    def validate_resource_roles(self) -> Self:
        """Validate the roles managed by the resource ownership helpers.

        Returns:
            Validated access control configuration.

        Raises:
            ValueError: `resource_roles` does not contain `owner`, contains
                duplicates, `admin` (so that owners cannot grant admin
                rights on their resources), a role that is not a valid value
                of policy rules, or a role starting with `subject_prefix`,
                `group:` or `client:`, so that subjects granted the role
                would get the rights of that subject.
        """
        # Imported here, as the access control package imports this module
        from foca.security.access_control.foca_casbin_adapter.casbin_rule import (  # noqa: PLC0415
            validate_rule_value,
        )

        if OWNER_ROLE not in self.resource_roles:
            raise ValueError(f"'resource_roles' must contain '{OWNER_ROLE}'")
        if len(set(self.resource_roles)) != len(self.resource_roles):
            raise ValueError("'resource_roles' must not contain duplicates")
        if ADMIN_ROLE in self.resource_roles:
            raise ValueError(f"'resource_roles' must not contain '{ADMIN_ROLE}'")
        prefixes = [self.subject_prefix, *SUBJECT_TYPE_PREFIXES.values()]
        for role in self.resource_roles:
            try:
                validate_rule_value(role)
            except ValueError as exc:
                raise ValueError(f"Invalid role in 'resource_roles': {exc}") from exc
            for prefix in prefixes:
                if role.startswith(prefix):
                    raise ValueError(
                        f"Roles of 'resource_roles' must not start with "
                        f"'{prefix}', the prefix of subjects"
                    )
        return self

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
                "'id_charset' may only contain letters, digits, '-', '.', '_' and '~'"
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
            return str(files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_MODEL_FILE))
        return str(_resolve_path(v, info))

    @field_validator("api_specs")
    @classmethod
    def validate_api_specs_path(cls, v: str | None, info: ValidationInfo) -> str | None:
        """Validate the path of the permissions API specification.

        Args:
            v: Path of the permissions API specification.
            info: Validation info.

        Returns:
            Absolute path as string, resolved like the model path, or `None`
            if not provided.
        """
        return None if v is None else str(_resolve_path(v, info))


class ServiceClientConfig(FOCABaseConfig):
    """Model for a service client, i.e., a client using its own tokens.

    Services call each other with OAuth 2.0 client-credentials tokens (e.g.,
    a workflow engine calling a task service), which represent the client,
    not a user, and have no user info. A token is a service token of a
    service client if it was issued by the client's `issuer` (its `iss`
    claim), to the client (its `client_id` claim, or `azp` without
    `client_id`, is `client_id`), and its subject (the claim configured as
    `AuthConfig.claim_identity`) is `subject`. Other tokens issued to the
    client (e.g., for users, with their subjects) are user tokens. Check
    which claims your identity provider puts into client-credentials
    tokens, and set `subject` accordingly.

    Args:
        client_id: Client ID of the service client.
        issuer: Issuer of the client's service tokens (`iss` claim), one of
            the trusted `AuthConfig.issuers`, as client IDs are only unique
            per issuer. Required if several issuers are trusted; if `None`,
            the only trusted issuer.
        subject: Subject of the client's service tokens. If `None`, the
            client ID.
        on_behalf_of: Whether the client may name the user it acts for (cf.
            `AuthConfig.on_behalf_of_header` and
            :py:func:`foca.security.access_control.get_on_behalf_of_user`),
            e.g., so that the app makes that user the owner of a resource
            the client creates. Such clients can also list the resources
            of any user they name (cf.
            :py:func:`foca.security.access_control.resource_filter`), so
            only allow fully trusted clients.

    Attributes:
        client_id: Client ID of the service client.
        issuer: Issuer of the client's service tokens.
        subject: Subject of the client's service tokens.
        on_behalf_of: Whether the client may name the user it acts for.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, or `client_id`, `issuer` or `subject` is empty.

    Examples:
        >>> client = ServiceClientConfig(client_id="conquest")
        >>> client.issuer is None and client.subject is None
        True
        >>> client.on_behalf_of
        False
    """

    client_id: str = Field(min_length=1)
    issuer: str | None = Field(default=None, min_length=1)
    subject: str | None = Field(default=None, min_length=1)
    on_behalf_of: bool = False


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
            the app in the audience of its tokens, or if `clients` are
            restricted instead.
        token_types: Accepted values of the `typ` header of JWTs, compared
            case-insensitively. By default, only OAuth 2.0 access tokens as
            per RFC 9068 (`at+jwt` or `application/at+jwt`) are accepted, so
            that other JWTs of trusted issuers, such as ID tokens, cannot be
            used as access tokens. Required unless `verify_token_type` is
            ``False``.
        verify_token_type: Whether to validate the type of JWTs. If
            ``False``, JWTs with any or no `typ` header are accepted; only
            disable it if the issuer does not set the `typ` header of its
            access tokens.
        clients: Accepted clients, i.e., IDs of the clients that obtained
            the tokens, as per the `client_id` claim (RFC 9068) of JWTs or,
            if they have none, the `azp` claim (OpenID Connect), compared
            exactly. If set, JWTs of other clients, and JWTs with neither
            claim, are rejected, e.g., so that a service behind a gateway
            accepts only tokens obtained by the gateway. Can be used in
            addition to `audience`, or instead of it with `verify_audience`
            set to ``False``. Client IDs are matched for all trusted
            `issuers`, so with several issuers, make sure that the listed
            IDs cannot be claimed by clients of another issuer. If ``None``,
            clients are not restricted.
        claim_identity: The JWT claim used to identify the sender. Tokens
            without this claim are rejected.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validate_userinfo: Whether to additionally validate tokens via the
            issuer's user info endpoint, which needs to report the token's
            subject.
        userinfo_claims: Claims to add from the issuer's user info endpoint
            if the token lacks them, e.g., `eduperson_entitlement`, for
            access control based on group claims (cf.
            `access_control.claim_groups`) with identity providers that
            only provide them via user info. Claims of the token are never
            replaced. The user info needs to report the token's subject; it
            is cached per token for `cache_ttl` seconds (unless
            `validate_userinfo` is set), but not beyond the token's expiry.
            The claims `client_id` and `azp` cannot be added, as they
            identify the client that obtained the token (cf. `clients` and
            `service_clients`).
        timeout: Timeout (in seconds) for requests to identity providers.
        cache_ttl: Time (in seconds) for which identity providers' discovery
            documents and public keys, and user info, are cached.
        service_clients: Service clients, whose client-credentials tokens
            (service tokens) represent the client, not a user (cf.
            :py:class:`ServiceClientConfig`). No user info is requested for
            service tokens (neither for `userinfo_claims` nor for
            `validate_userinfo`), and access control enforces policies on
            them with the subject `client:<client ID>` (cf.
            `AccessControlConfig.client_mapping`). Client IDs must be unique.
        on_behalf_of_header: Request header in which service clients allowed
            to (cf. `ServiceClientConfig.on_behalf_of`) name the user they
            act for (e.g., `X-On-Behalf-Of`). If `None`, only the app can
            pass a named user (e.g., from a request field) to
            :py:func:`foca.security.access_control.get_on_behalf_of_user`.

    Attributes:
        issuers: Trusted token issuers.
        add_key_to_claims: Whether to add the identity provider's public key
            that the token was verified with, in PEM format, to the claims.
        allow_expired: Allow/disallow expired JWTs.
        audience: List of audiences that the app identifies itself with.
        verify_audience: Whether to validate the audience of JWTs.
        token_types: Accepted values of the `typ` header of JWTs.
        verify_token_type: Whether to validate the type of JWTs.
        clients: Accepted clients, i.e., IDs of the clients that obtained
            the tokens.
        claim_identity: The JWT claim used to identify the sender.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validate_userinfo: Whether to additionally validate tokens via the
            issuer's user info endpoint.
        userinfo_claims: Claims to add from the issuer's user info endpoint
            if the token lacks them.
        timeout: Timeout (in seconds) for requests to identity providers.
        cache_ttl: Time (in seconds) for which identity providers' discovery
            documents and public keys, and user info, are cached.
        service_clients: Service clients.
        on_behalf_of_header: Request header in which service clients name
            the user they act for.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, `issuers` are set without `audience` while
            `verify_audience` is ``True``, `token_types` are empty while
            `verify_token_type` is ``True``, `clients` are set but empty
            (or blank), client IDs of `service_clients` are not unique,
            service clients are not among the accepted `clients`, the
            issuer of a service client is not trusted or, with several
            trusted `issuers`, not set, or `userinfo_claims` contain
            `client_id` or `azp`.

    Examples:
        >>> auth = AuthConfig(
        ...     issuers=["https://login.example.org/oidc/"],
        ...     audience=["my-app"],
        ... )
        >>> auth.audience
        ['my-app']
        >>> auth.algorithms
        ['RS256']
        >>> auth.token_types
        ['at+jwt', 'application/at+jwt']
    """

    issuers: list[str] = []
    add_key_to_claims: bool = True
    allow_expired: bool = False
    audience: list[str] | None = None
    verify_audience: bool = True
    token_types: list[str] = ["at+jwt", "application/at+jwt"]
    verify_token_type: bool = True
    clients: list[str] | None = None
    claim_identity: str = "sub"
    algorithms: list[str] = ["RS256"]
    validate_userinfo: bool = False
    userinfo_claims: list[str] = []
    timeout: float = Field(default=10, gt=0)
    cache_ttl: float = Field(default=300, ge=0)
    service_clients: list[ServiceClientConfig] = []
    on_behalf_of_header: str | None = Field(default=None, min_length=1)
    # Whether warnings about the settings were logged, i.e., on the first
    # token validation
    _warnings_logged: bool = PrivateAttr(default=False)

    @field_validator("userinfo_claims")
    @classmethod
    def validate_userinfo_claims(cls, v: list[str]) -> list[str]:
        """Ensure that claims identifying clients are not added from user info.

        Args:
            v: Claims to add from user info.

        Returns:
            Claims to add from user info.

        Raises:
            ValueError: `client_id` or `azp` is to be added, so that user
                info could make tokens appear to be obtained by other
                clients (e.g., by service clients).
        """
        # Client claims as per `foca.security.auth.CLIENT_CLAIMS` (import cycle)
        client_claims = sorted({"client_id", "azp"} & set(v))
        if client_claims:
            raise ValueError(
                "Claims identifying the client that obtained a token cannot be "
                f"added from user info: {', '.join(client_claims)}"
            )
        return v

    @field_validator("service_clients")
    @classmethod
    def validate_service_clients(
        cls, v: list[ServiceClientConfig]
    ) -> list[ServiceClientConfig]:
        """Ensure that service clients are unique.

        Args:
            v: Service clients.

        Returns:
            Service clients.

        Raises:
            ValueError: Several service clients have the same client ID.
        """
        client_ids = [client.client_id for client in v]
        duplicates = sorted({c for c in client_ids if client_ids.count(c) > 1})
        if duplicates:
            raise ValueError(f"Duplicate service clients: {', '.join(duplicates)}")
        return v

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
                "any audience, preferably restricting the clients that "
                "obtained them via `security.auth.clients` instead (accepting "
                "tokens of any client is not recommended)."
            )
        return self

    @model_validator(mode="after")
    def validate_clients(self) -> Self:
        """Require non-blank client IDs if clients are restricted.

        Returns:
            Model instance.

        Raises:
            ValueError: Clients are set but empty, or a client ID is blank.
        """
        if self.clients is not None and not (
            self.clients and all(client.strip() for client in self.clients)
        ):
            raise ValueError(
                "Restricting clients requires client IDs: set "
                "`security.auth.clients` to the (non-blank) IDs of the clients "
                "whose tokens the app accepts (`client_id` claim, or `azp` claim "
                "if there is none), or omit it to accept tokens of any client."
            )
        return self

    @model_validator(mode="after")
    def validate_service_client_issuers(self) -> Self:
        """Require trusted issuers of service clients.

        Returns:
            Model instance.

        Raises:
            ValueError: The issuer of a service client is not a trusted
                issuer, or several issuers are trusted and the issuer of a
                service client is not set.
        """
        several_issuers = len(set(self.issuers)) > 1
        for client in self.service_clients:
            if client.issuer is None:
                if several_issuers:
                    raise ValueError(
                        f"Service client '{client.client_id}' needs an issuer: "
                        "with several trusted issuers, set "
                        "`security.auth.service_clients[].issuer` to the issuer "
                        "of its service tokens, as client IDs are only unique "
                        "per issuer."
                    )
            elif client.issuer not in self.issuers:
                raise ValueError(
                    f"Issuer of service client '{client.client_id}' is not "
                    "trusted: set `security.auth.service_clients[].issuer` to "
                    "one of `security.auth.issuers`."
                )
        return self

    @model_validator(mode="after")
    def validate_service_clients_accepted(self) -> Self:
        """Require service clients to be accepted if clients are restricted.

        Returns:
            Model instance.

        Raises:
            ValueError: Clients are restricted, but service clients are not
                among the accepted clients.
        """
        if self.clients is None:
            return self
        missing = [
            client.client_id
            for client in self.service_clients
            if client.client_id not in self.clients
        ]
        if missing:
            raise ValueError(
                "Service clients must be accepted clients: add "
                f"{', '.join(missing)} to `security.auth.clients`, so that "
                "their tokens are accepted."
            )
        return self

    @model_validator(mode="after")
    def validate_token_types(self) -> Self:
        """Require token types for token type validation, unless opted out.

        Returns:
            Model instance.

        Raises:
            ValueError: Token types are empty (or blank) while token type
                validation is not disabled.
        """
        has_token_types = bool(self.token_types) and all(
            typ.strip() for typ in self.token_types
        )
        if self.verify_token_type and not has_token_types:
            raise ValueError(
                "Token type validation requires token types: set "
                "`security.auth.token_types` to the (non-blank) values of the "
                "`typ` header of accepted tokens (default: `at+jwt` and "
                "`application/at+jwt`, i.e., OAuth 2.0 access tokens as per "
                "RFC 9068), or set `security.auth.verify_token_type: false` to "
                "accept tokens of any type (not recommended)."
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
                "'allow_credentials' requires explicit 'allow_origins' instead of '*'"
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
                (e.g., policies would not be enforced), access control is
                enabled without trusted token issuers, in which case all
                requests to the permissions API and to enforced operations
                would be rejected, clients that are not service clients
                are mapped to roles, or the header of opt-in roles is that
                of service clients acting for users.
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
                    ("client_mapping", access_control.client_mapping),
                    ("require_admin", access_control.require_admin),
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
        on_behalf_of = self.auth.on_behalf_of_header
        if (
            access_control.opt_in_roles
            and on_behalf_of is not None
            and on_behalf_of.lower() == access_control.opt_in_header.lower()
        ):
            raise ValueError(
                "'access_control.opt_in_header' must differ from "
                "'auth.on_behalf_of_header'"
            )
        service_clients = {client.client_id for client in self.auth.service_clients}
        unknown = sorted(set(access_control.client_mapping or {}) - service_clients)
        if unknown:
            raise ValueError(
                "'access_control.client_mapping' maps clients that are not "
                f"service clients ('auth.service_clients'): {', '.join(unknown)}"
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

    keys: dict[str, Any] | list[tuple[Any, ...]] | None = None
    options: dict[str, Any] = {}

    @field_validator("keys", mode="after")
    @classmethod
    def keys_to_list(
        cls, v: dict[str, Any] | list[tuple[Any, ...]] | None
    ) -> dict[str, Any] | list[tuple[Any, ...]] | None:
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
    client: collection.Collection[dict[str, Any]] | None = None


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
    client: database.Database[dict[str, Any]] | None = None


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
    _client: MongoClient[dict[str, Any]] | None = PrivateAttr(default=None)


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


class ServiceInfoConfig(FOCABaseConfig):
    """Model for the GA4GH service info of an app.

    The service info is served by
    :py:func:`foca.api.service_info.getServiceInfo` (`GET /service-info`),
    built from, in order of precedence, `service`,
    the fields returned by `runtime_fields`, and the version of `package`.
    Fields and list items set to ``None`` (e.g., in config overlays) are
    removed, also if set at a lower precedence, except for `version`, which
    is then taken from the next source. The service info must be
    valid as per the `Service` schema of the GA4GH service-info
    specification, version 1.0.0.

    Args:
        service: Service info, as per the GA4GH `Service` schema, e.g., with
            `id`, `name`, `type`, `organization` and `version` (required) and
            `description`, `contactUrl`, `documentationUrl`, `environment`,
            `createdAt` and `updatedAt` (optional, served only if set). Other
            fields are served as they are.
        package: Name of the app's distribution package (as installed, e.g.,
            with `pip install`), whose version is served as `version`,
            unless `service` sets `version` (to a value other than ``None``).
            The package must then be installed.
        runtime_fields: Path to a function returning a dictionary of fields
            that are computed at runtime, as a dot-separated path to the
            module, followed by the name of the function, e.g.,
            ``my_app.service_info.runtime_fields`` (must be importable). It
            is called without arguments, in an app context, on startup and
            for each request. Required fields that it returns may be missing
            from `service`.
        log_traffic: Whether requests for the service info and their
            responses are logged (cf.
            :py:func:`foca.utils.logging.log_traffic`).

    Attributes:
        service: Service info, as per the GA4GH `Service` schema.
        package: Name of the app's distribution package.
        runtime_fields: Path to a function returning fields that are computed
            at runtime.
        log_traffic: Whether requests for the service info and their
            responses are logged.
        package_version: Version of `package`, if used for `version` (set on
            validation).
        runtime_function: Function imported from `runtime_fields`, if set
            (set on validation).

    Raises:
        pydantic.ValidationError: The class was instantiated with an illegal
            data type, `package` is not installed although needed, the
            function cannot be imported, or the service info is invalid (when
            `runtime_fields` is set, required fields are only checked on
            startup).

    Examples:
        >>> service_info = ServiceInfoConfig(
        ...     service={
        ...         "id": "org.example.my-service",
        ...         "name": "My service",
        ...         "type": {
        ...             "group": "org.example",
        ...             "artifact": "my-api",
        ...             "version": "1.0.0",
        ...         },
        ...         "organization": {
        ...             "name": "Example",
        ...             "url": "https://example.org",
        ...         },
        ...         "version": "1.0.0",
        ...     },
        ... )
        >>> service_info.service["version"]
        '1.0.0'
        >>> service_info.package is None
        True
    """

    service: dict[str, Any]
    package: str | None = None
    runtime_fields: str | None = None
    log_traffic: bool = True
    # Version of `package`, if used; set on validation
    _package_version: str | None = PrivateAttr(default=None)
    # Function imported from `runtime_fields`; set on validation
    _runtime_function: Callable[[], Mapping[str, Any]] | None = PrivateAttr(
        default=None
    )

    @property
    def package_version(self) -> str | None:
        """Version of `package`, if used for `version`."""
        return self._package_version

    @property
    def runtime_function(self) -> Callable[[], Mapping[str, Any]] | None:
        """Function imported from `runtime_fields`, if set."""
        return self._runtime_function

    @model_validator(mode="after")
    def validate_service_info(self) -> Self:
        """Validate the service info.

        Returns:
            Model instance with the package version looked up and the runtime
            fields function imported.

        Raises:
            ValueError: `package` is needed but not installed, the runtime
                fields function cannot be imported or is not callable, or the
                service info is invalid, naming the first invalid field.
                Missing required fields are ignored if `runtime_fields` is
                set.
        """
        self._runtime_function = (
            None
            if self.runtime_fields is None
            else import_runtime_function(self.runtime_fields)
        )
        # `version: null` (e.g., in a config overlay) is the same as unset
        self._package_version = (
            lookup_package_version(self.package)
            if self.package is not None and self.service.get("version") is None
            else None
        )
        validate_service_info(
            merge_service_info(self.service, self._package_version),
            prefix="service_info.service",
            ignore_missing=self.runtime_fields is not None,
        )
        return self


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
        service_info: GA4GH service info parameters.
        plugins: Gateway plugin parameters (cf. :py:mod:`foca.plugins`).

    Attributes:
        server: Server config parameters.
        exceptions: Exception handling parameters.
        api: OpenAPI specification config parameters.
        security: Security config parameters.
        db: Database config parameters.
        jobs: Background job config parameters.
        log: Logger config parameters.
        service_info: GA4GH service info parameters.
        plugins: Gateway plugin parameters, if configured.

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
    service_info: ServiceInfoConfig | None = None
    plugins: PluginsConfig | None = None
    model_config = ConfigDict(extra="allow")
