"""FOCA config models."""

from copy import deepcopy
from enum import Enum
from functools import reduce
import importlib
from importlib.resources import as_file, files
import operator
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pymongo import collection, database
from typing_extensions import Self

from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE
)


def _validate_log_level_choices(cls, level: int) -> int:
    """Ensure that a valid logging level is set.

    Args:
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


def _get_by_path(
    obj: Dict,
    key_sequence: List[str]
) -> Any:
    """Access a nested dictionary by sequence of keys.

    Args:
        obj: (Nested) dictionary.
        key_sequence: Sequence of keys, to be applied from outside to inside,
            pointing to the key (and descendants) to retrieve.

    Returns:
        Value of innermost key.
    """
    return reduce(operator.getitem, key_sequence, obj)  # type: ignore


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


class ValidationMethodsEnum(Enum):
    """Enumerator for JSON Web Token (JWT) validation methods.

    Attributes:
        public_key: JWT validation via the identity provider's JSON Web Key.
        userinfo: JWT validation via OpenID Connect-compliant identity
            provider's ``/userinfo`` endpoint.
    """
    public_key = "public_key"
    userinfo = "userinfo"


class ValidationChecksEnum(Enum):
    """Enumerator for values indicating how many JSON Web Token (JWT)
    validation methods are requested.

    Attributes:
        all: All JWT validation methods need to pass; validation fails after
            first unsuccessful check.
        any: Any method is sufficient to validate the JWT; validation succeeds
            after the first successful check.
    """
    all = "all"
    any = "any"


class PymongoDirectionEnum(Enum):
    """Enumerator for supported Pymongo index directions.

    Cf.
    https://pymongo.readthedocs.io/en/3.10.1/api/pymongo/collection.html#pymongo.collection.Collection.create_index

    Attributes:
        ASCENDING: Ascending sort order.
        DESCENDING: Descending sort order.
        GEO2D: Index specifier for a 2-dimensional geospatial index.
        GEOHAYSTACK: Index specifier for a 2-dimensional haystack index.
        GEOSPHERE: Index specifier for a spherical geospatial index.
        HASHED: Index specifier for a hashed index.
        TEXT: Index specifier for a text index.
    """
    ASCENDING = 1
    DESCENDING = -1
    GEO2D = "2d"
    GEOHAYSTACK = "geoHaystack"
    GEOSPHERE = "2dsphere"
    HASHED = "hashed"
    TEXT = "text"


class FOCABaseConfig(BaseModel):
    """Base configuration for FOCA models."""
    model_config = ConfigDict(extra='forbid', arbitrary_types_allowed=True)


class ServerConfig(FOCABaseConfig):
    """Model for configuration parameters to set up a Flask or Connexion
    app instance.

    Args:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode. If ``True``, the
            application runs in debug mode and an interactive debugger will
            be shown for unhandled exceptions. See Flask documentation for more
            details.
        environment: Variable to specify the application environment variable.
            See Flask documentation for more details.
        testing: Enable/disable testing mode. If ``True``, exceptions are
            propagated rather than handled by the the app’s error handlers.
        use_reloader: Enable/disable the application reloader. If
            ``debug=True``, enabling this will allow the server to reload
            automatically on code changes. See Flask documentation for more
            details.

    Attributes:
        host: Host at which the application is exposed.
        port: Port at which the application is exposed.
        debug: Flag to run application in debug mode. If ``True``, the
            application runs in debug mode and an interactive debugger will
            be shown for unhandled exceptions. See Flask documentation for more
            details.
        environment: Variable to specify the application environment variable.
            See Flask documentation for more details.
        testing: Enable/disable testing mode. If ``True``, exceptions are
            propagated rather than handled by the the app’s error handlers.
        use_reloader: Enable/disable the application reloader. If
            ``debug=True``, enabling this will allow the server to reload
            automatically on code changes. See Flask documentation for more
            details.

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


class ExceptionConfig(FOCABaseConfig):
    """Model for app context JSON exceptions to be registered with a Connexion
    app.

    Args:
        required_members: List of dictionary keys indicating which JSON members
            are required for all exceptions. *Must* contain a member that
            represents the HTTP response code (cf. `status_member`).
        extension_members: Either a list of additionally allowed, optional
            extension members, or a Boolean expression indicating whether
            any (``True``) or no (``False``) additional members are allowed.
        status_member: Sequence of dictionary keys indicating the member that
            represents the HTTP response code (e.g, ``500``).
        public_members: Filter to restrict which exception members are to be
            included in the error response. Only members listed here, with each
            one specified as a sequence of keys, are included. Specify an empty
            list to prevent any members from being returned to the user. Set to
            ``None`` to disable filtering. Note that only one of
            `public_members` and `private_members` filters can be active.
        private_members: Filter to restrict which exception members are to be
            included in the error response. Members listed here, with each one
            specified as a sequence of keys, are excluded. Set to ``None`` to
            disable filtering. Note that only one of `public_members` and
            `private_members` filters can be active.
        exceptions: Path to dictionary containing the actual exception classes
            as keys and a dictionary of JSON members (as per `required_members`
            and `extension_members`) as values. Path should be a dot-separated
            path to the module containing the dictionary (which needs to also
            contain imports for all listed exceptions), followed by the name of
            the dictionary itself. For example, for ``myapp.errors.exc_dict``,
            the dictionary ``exc_dict`` would be attempted to be imported from
            module ``myapp.errors`` (must be available in the Pythonpath). To
            ensure that all exceptions arising during the app context are
            handled, it is strongly advised to add the catch-all exception
            ``Exception``. If missing, any exceptions not listed will only
            provoke an empty JSON response.
        logging: Specifies if and how exception details should be logged. Note
            that unless :py:attr:`foca.models.config.ExceptionLoggingEnum.none`
            is specified, a JSON representation of the error, as defined in
            `exceptions`, and including _all_ members, unaffected by
            `public_members` and `private_members` filters, will be logged on
            an additional line.
        mapping: The actual referenced dictionary from `exceptions`, populated
            by FOCA.

    Attributes:
        required_members: List of dictionary keys indicating which JSON members
            are required for all exceptions. *Must* contain a member that
            represents the HTTP response code (cf. `status_member`).
        extension_members: Either a list of additionally allowed, optional
            extension members, or a Boolean expression indicating whether
            any (``True``) or no (``False``) additional members are allowed.
        status_member: Sequence of dictionary keys indicating the member that
            represents the HTTP response code (e.g, ``500``).
        public_members: Filter to restrict which exception members are to be
            included in the error response. Only members listed here, with each
            one specified as a sequence of keys, are included. Specify an empty
            list to prevent any members from being returned to the user. Set to
            ``None`` to disable filtering. Note that only one of
            `public_members` and `private_members` filters can be active.
        private_members: Filter to restrict which exception members are to be
            included in the error response. Members listed here, with each one
            specified as a sequence of keys, are excluded. Set to ``None`` to
            disable filtering. Note that only one of `public_members` and
            `private_members` filters can be active.
        exceptions: Path to dictionary containing the actual exception classes
            as keys and a dictionary of JSON members (as per `required_members`
            and `extension_members`) as values. Path should be a dot-separated
            path to the module containing the dictionary (which needs to also
            contain imports for all listed exceptions), followed by the name of
            the dictionary itself. For example, for ``myapp.errors.exc_dict``,
            the dictionary ``exc_dict`` would be attempted to be imported from
            module ``myapp.errors`` (must be available in the Pythonpath). To
            ensure that all exceptions arising during the app context are
            handled, it is strongly advised to add the catch-all exception
            ``Exception``. If missing, any exceptions not listed will only
            provoke an empty JSON response.
        logging: Specifies if and how exception details should be logged. Note
            that unless :py:attr:`foca.models.config.ExceptionLoggingEnum.none`
            is specified, a JSON representation of the error, as defined in
            `exceptions`, and including _all_ members, unaffected by
            `public_members` and `private_members` filters, will be logged on
            an additional line.
        mapping: The actual referenced dictionary from `exceptions`, populated
            by FOCA.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ExceptionConfig()
        ExceptionConfig(required_members=[['title'], ['status']], extension_me\
mbers=False, status_member=['status'], public_members=None, private_members=No\
ne, exceptions='foca.errors.exceptions.exceptions', logging=<ExceptionLoggingE\
num.oneline: 'oneline'>, mapping={<class 'Exception'>: {'title': 'Internal Ser\
ver Error', 'status': 500}, <class 'werkzeug.exceptions.BadRequest'>: {'title'\
: 'Bad Request', 'status': 400}, <class 'connexion.exceptions.ExtraParameterPr\
oblem'>: {'title': 'Bad Request', 'status': 400}, <class 'werkzeug.exceptions.\
Unauthorized'>: {'title': 'Unauthorized', 'status': 401}, <class 'connexion.ex\
ceptions.OAuthProblem'>: {'title': 'Unauthorized', 'status': 401}, <class 'wer\
kzeug.exceptions.Forbidden'>: {'title': 'Forbidden', 'status': 403}, <class 'w\
erkzeug.exceptions.NotFound'>: {'title': 'Not Found', 'status': 404}, <class '\
werkzeug.exceptions.InternalServerError'>: {'title': 'Internal Server Error', \
'status': 500}, <class 'werkzeug.exceptions.BadGateway'>: {'title': 'Bad Gatew\
ay', 'status': 502}, <class 'werkzeug.exceptions.ServiceUnavailable'>: {'title\
': 'Service Unavailable', 'status': 502}, <class 'werkzeug.exceptions.GatewayT\
imeout'>: {'title': 'Gateway Timeout', 'status': 504}})
    """
    required_members: List[List[str]] = [["title"], ["status"]]
    extension_members: Union[bool, List[List[str]]] = False
    status_member: List[str] = ["status"]
    public_members: Optional[List[List[str]]] = None
    private_members: Optional[List[List[str]]] = None
    exceptions: str = "foca.errors.exceptions.exceptions"
    logging: ExceptionLoggingEnum = ExceptionLoggingEnum.oneline
    mapping: Optional[Dict[Type[BaseException], Dict[str, Any]]] = None

    @model_validator(mode="after")
    def validate_exceptions_mapping(self) -> Self:
        """Validate exceptions mapping.

        Make sure that the exceptions mapping is set and that it can be
        imported. Ensure that all exceptions have all required members and no
        additional members (unless specifically allowed). Replace default value
        for field mapping to the contents of the exceptions dictionary.

        Returns:
            Model instance with exceptions mapping set.
        """
        # Ensure that exceptions can be imported and are of the correct type
        split_module = self.exceptions.split(".")
        exc_dict_name = split_module.pop()
        module_path = ".".join(split_module)
        try:
            mod = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            raise ValueError(
                f"Module '{module_path}' referenced in field 'exceptions' "
                "could not be found."
            ) from exc
        try:
            exc_dict = getattr(mod, exc_dict_name)
        except AttributeError as exc:
            raise ValueError(
                f"Module '{module_path}' referenced in field 'exceptions' "
                f"does not have attribute '{exc_dict_name}'."
            ) from exc
        if not isinstance(exc_dict, dict):
            raise ValueError(
                f"Attribute '{exc_dict_name}' in module '{module_path}' "
                "referenced in field 'exceptions' is not a dictionary."
            )

        # Set allowed exception members
        allowed_members = deepcopy(self.required_members)
        if isinstance(self.extension_members, list):
            allowed_members += self.extension_members

        # Set flag to determine if additional members are allowed
        limited_members: bool = not (
            isinstance(self.extension_members, bool) and self.extension_members
        )

        # Ensure that status member is among required members
        if self.status_member not in self.required_members:
            raise ValueError(
                "Status member is not among required members."
            )

        # Filter members that are returned to the user
        if (
                isinstance(self.public_members, list) and
                isinstance(self.private_members, list)
        ):
            raise ValueError(
                "Both public and private member filters are active, but at "
                "most one is allowed."
            )
        if (
            limited_members
            and self.public_members
            and any(m not in allowed_members for m in self.public_members)
        ):
            raise ValueError(
                "Public members have more fields than are allowed by "
                "'required_members' and 'extension_members'."
            )
        if (
            limited_members
            and self.private_members
            and any(m not in allowed_members for m in self.private_members)
        ):
            raise ValueError(
                "Private members have more fields than are allowed by "
                "'required_members' and 'extension_members'."
            )

        # Ensure that each exception fulfills all requirements
        for key, val in exc_dict.items():

            # Keys are exceptions
            try:
                getattr(key, '__cause__')
            except AttributeError as exc:
                raise ValueError(
                    f"Key '{key}' in 'exceptions' dictionary does not appear "
                    "to be an Exception."
                ) from exc
            # Values are dictionaries
            if not isinstance(val, dict):
                raise ValueError(
                    f"Exception '{key}' in 'exceptions' dictionary does not "
                    "have member dictionary as its value."
                )
            # All required members are present
            for keys in self.required_members:
                try:
                    _get_by_path(
                        obj=val,
                        key_sequence=keys,
                    )
                except (KeyError, ValueError) as exc:
                    raise ValueError(
                        f"Exception '{key}' in 'exceptions' dictionary does "
                        "not have all fields required by 'required_members'."
                    ) from exc
            # No forbidden members are present
            if limited_members:
                members = deepcopy(val)
                for keys in allowed_members:
                    try:
                        reduce(lambda v, k: v.pop(k), keys, members)
                    except KeyError:
                        pass
                if members:
                    raise ValueError(
                        f"Exception '{key}' in 'exceptions' dictionary has "
                        "more fields than are allowed by 'required_members' "
                        "and 'extension_members'."
                    )
            # Status member values are integers
            try:
                status = _get_by_path(
                    obj=val,
                    key_sequence=self.status_member,
                )
                status = int(status)
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"Status member in exception '{key}' cannot be cast to "
                    "type integer."
                ) from exc

        # Set mapping
        self.mapping = exc_dict

        return self


class SpecConfig(FOCABaseConfig):
    """Model for configuration parameters for OpenAPI 2.x or 3.x specifications
    to be attached to a Connexion app.

    Args:
        path: A single path or list of paths to OpenAPI 2.x or 3.x
            specification in YAML format.
        path_out: Output path for modified specification file. Ignored if specs
            are not modified. If not specified, the original file path is
            stripped of the file extension and the suffix ``.modified.yaml`` is
            appended.
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
        connexion: Keyword arguments passed through to
            `connexion.apps.flask_app.add_api()`.

    Attributes:
        path: A single path or list of paths to OpenAPI 2.x or 3.x
            specification in YAML format.
        path_out: Output path for modified specification file. Ignored if specs
            are not modified. If not specified, the original file path is
            stripped of the file extension and the suffix ``.modified.yaml`` is
            appended.
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
        connexion: Keyword arguments passed through to
            `connexion.apps.flask_app.add_api()`.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:

        >>> SpecConfig(path="/my/path.yaml")
        SpecConfig(path=[PosixPath('/my/path.yaml')], path_out=PosixPath('/my/\
path.modified.yaml'), append=None, add_operation_fields=None, add_security_fie\
lds=None, disable_auth=False, connexion=None)

        >>> SpecConfig(
        ...     path=["/path/to/specs.yaml", "/path/to/add_specs.yaml"],
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
        ...     add_security_fields = {
        ...         "x-apikeyInfoFunc": "security.auth.validate_token",
        ...         "x-some-other-custom-field": "some_value",
        ...     },
        ...     disable_auth = False
        ... )
        SpecConfig(path=[PosixPath('/path/to/specs.yaml'), PosixPath('/path/to\
/add_specs.yaml')], path_out=PosixPath('/path/to/specs.modified.yaml'), append\
=[{'security': {'jwt': {'type': 'apiKey', 'name': 'Authorization', 'in': 'head\
er'}}}, {'my_other_root_field': 'some_value'}], add_operation_fields={'x-swagg\
er-router-controller': 'controllers.my_specs', 'x-some-other-custom-field': 's\
ome_value'}, add_security_fields={'x-apikeyInfoFunc': 'security.auth.validate_\
token', 'x-some-other-custom-field': 'some_value'}, disable_auth=False, connex\
ion=None)
    """
    path: Union[Path, List[Path]]
    path_out: Optional[Path] = None
    append: Optional[List[Dict]] = None
    add_operation_fields: Optional[Dict] = None
    add_security_fields: Optional[Dict] = None
    disable_auth: bool = False
    connexion: Optional[Dict] = None

    @model_validator(mode="after")
    def make_paths_absolute_and_set_path_out(self) -> Self:
        """Make paths absolute and set output path if not specified.

        Returns:
            Model instance with absolute paths and output path set.
        """
        paths = (
            self.path if isinstance(self.path, list)
            else [self.path]
        )
        self.path = [
            path if path.is_absolute()
            else path.resolve() for path in paths
        ]
        if self.path_out is None:
            _path = self.path[0].resolve()
            self.path_out = _path.parent / f"{_path.stem}.modified.yaml"
        else:
            self.path_out = Path(self.path_out).resolve()

        return self


class APIConfig(FOCABaseConfig):
    """Model for a list of configuration parameters for OpenAPI 2.x or 3.x
    specifications to be attached to a Connexion app.

    Args:
        specs: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app.

    Attributes:
        specs: List of configuration parameters for OpenAPI 2.x or 3.x
            specifications to be attached to a Connexion app.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> APIConfig(
        ...     specs=[SpecConfig(path='/path/to/specs.yaml')],
        ... )
        APIConfig(specs=[SpecConfig(path=[PosixPath('/path/to/specs.yaml')], p\
ath_out=PosixPath('/path/to/specs.modified.yaml'), append=None, add_operation_\
fields=None, add_security_fields=None, disable_auth=False, connexion=None)])
    """
    specs: List[SpecConfig] = []


class AccessControlConfig(FOCABaseConfig):
    """Model for setting access control configuration.
    For exact behaviour cf. https://github.com/casbin/pycasbin.

    Args:
        api_specs: Path to API spec definition. If None, the use default
            specs.
        api_controllers: Path to API spec controller. If None, the use default
            specs.
        db_name: Access control database name. If None, the use default specs.
        collection_name: Access control collection name. If None, the use
            default specs.
        model: Path to access control model configuration file. If None,
            the use default specs.
        owner_headers: Owner (Admin) specific header property requirements
            for casbin.
        user_headers: User specific header property requirements for casbin.

    Attributes:
        api_specs: Path to API spec definition.
        api_controllers: Path to API spec controller.
        db_name: Access control database name.
        collection_name: Access control collection name.
        model: Path to access control model configuration file.
        owner_headers: Owner (Admin) specific header property requirements
            for casbin.
        user_headers: User specific header property requirements for casbin.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> AccessControlConfig(
        ...     api_specs='/path/to/spec.yaml',
        ...     api_controllers='/path/to/spec_server.py',
        ...     db_name='access_control_db',
        ...     collection_name='access_control_collection',
        ...     model='/path/to/policy.conf',
        ...     owner_headers={'X-User', 'X-Group'},
        ...     user_headers={'X-User'}
        ... )
        AccessControlConfig(api_specs='/path/to/access_control_spec.yaml',api_\
controllers='/path/to/access_control_spec_server.py', db_name='access_control_\
db', collection_name='access_control_collection', model='/path/to/policy.co\
nf', owner_headers={'X-User', 'X-Group'}, user_headers={'X-User'})
    """
    api_specs: Optional[str] = None
    api_controllers: Optional[str] = None
    db_name: Optional[str] = None
    collection_name: Optional[str] = None
    model: Optional[str] = Field(default=None, validate_default=True)
    owner_headers: Optional[set] = None
    user_headers: Optional[set] = None

    @field_validator('model', mode='before')
    @classmethod
    def validate_model_path(cls, v: Optional[str]) -> str:
        """Validate the model path.

        Args:
            v: Model path.

        Returns:
            Model path as string; resolved relative to the caller's current
            working directory if provided path is not absolute. If no path
            is provided, return default model path.

        """
        if v is None:
            model_file = files(ACCESS_CONTROL_BASE_PATH).joinpath(
                DEFAULT_MODEL_FILE
            )
            with as_file(model_file) as _path:
                return str(_path)

        model_path = Path(v)
        if not model_path.is_absolute():
            return str(model_path.resolve())

        return v


class AuthConfig(FOCABaseConfig):
    """Model for parameters used to configure JSON Web Token (JWT)-based
    authorization for the app.

    Args:
        required: Boolean to define the auth configuration for the app.
            Defaults to true.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims.
        allow_expired: Allow/disallow expired JWTs. If ``False``, a ``401``
            authorization error is raised in response to a request containing
            an expired JWT.
        audience: List of audiences that the app identifies itself with. If
            specified, JWTs that do not contain any of the specified audiences
            are rejected. If ``None``, audience validation is disabled.
        claim_identity: The JWT claim used to identify the sender.
        claim_issuer: The JWT claim used to identify the issuer.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validation_methods: Lists the methods to be used to validate a JWT.
        validation_checks: Specify how many of the `validation_methods` need
            to pass before accepting a JWT.

    Attributes:
        required: Boolean to define the auth configuration for the app.
        add_key_to_claims: Whether to allow the application to add the identity
            provider's corresponding JSON Web Key (JWK), in PEM format, to the
            dictionary of claims.
        allow_expired: Allow/disallow expired JWTs. If ``False``, a ``401``
            authorization error is raised in response to a request containing
            an expired JWT.
        audience: List of audiences that the app identifies itself with. If
            specified, JWTs that do not contain any of the specified audiences
            are rejected. If ``None``, audience validation is disabled.
        claim_identity: The JWT claim used to identify the sender.
        claim_issuer: The JWT claim used to identify the issuer.
        algorithms: Lists the JWT-signing algorithms supported by the app.
        validation_methods: Lists the methods to be used to validate a JWT.
        validation_checks: Specify how many of the `validation_methods` need
            to pass before accepting a JWT.

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
        ...     algorithms=["RS256"],
        ...     validation_methods=["userinfo", "public_key"],
        ...     validation_checks="all",
        ... )
        AuthConfig(required=False, add_key_to_claims=True, allow_expired=False\
, audience=None, claim_identity='sub', claim_issuer='iss', algorithms=['RS256'\
], validation_methods=[<ValidationMethodsEnum.userinfo: 'userinfo'>, <Validati\
onMethodsEnum.public_key: 'public_key'>], validation_checks=<ValidationChecksE\
num.all: 'all'>)
    """
    required: bool = True
    add_key_to_claims: bool = True
    allow_expired: bool = False
    audience: Optional[List[str]] = None
    claim_identity: str = "sub"
    claim_issuer: str = "iss"
    algorithms: List[str] = ["RS256"]
    validation_methods: List[ValidationMethodsEnum] = [
        ValidationMethodsEnum.userinfo,
        ValidationMethodsEnum.public_key,
    ]
    validation_checks: ValidationChecksEnum = ValidationChecksEnum.all


class CORSConfig(FOCABaseConfig):
    """Model for Cross Origin Resource Sharing (CORS) configuration.

    Args:
        enabled: Enable/disable the CORS for the application.

    Attributes:
        enabled: Enable/disable the CORS for the application.

    Example:
        >>> CORSConfig(
        ...     enabled=True,
        ... )
        CORSConfig(enabled=True)
    """
    enabled: bool = True


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
            data type.

    Example:
        >>> SecurityConfig(
        ...     access_control=AccessControlConfig(),
        ...     auth=AuthConfig(),
        ...     cors=CORSConfig(),
        ... )
        SecurityConfig(auth=AuthConfig(required=False, add_key_to_claims=True,\
 allow_expired=False, audience=None, claim_identity='sub', claim_issuer='iss',\
 algorithms=['RS256'], validation_methods=[<ValidationMethodsEnum.userinfo: 'u\
serinfo'>, <ValidationMethodsEnum.public_key: 'public_key'>], validation_check\
s=<ValidationChecksEnum.all: 'all'>), cors=CORSConfig(enabled=True), access_co\
ntrol=AccessControlConfig(api_specs='/path/to/access_control_spec.yaml',api_co\
ntrollers='/path/to/access_control_spec_server.py', db_name='access_control_db\
', collection_name='access_control_collection', model='/path/to/policy.conf', \
owner_headers={'X-User', 'X-Group'}, user_headers={'X-User'}))
    """
    access_control: AccessControlConfig = AccessControlConfig()
    auth: AuthConfig = AuthConfig()
    cors: CORSConfig = CORSConfig()


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

    Example:
        >>> IndexConfig(
        ...     keys={'name': -1, 'id': 1},
        ...     options={'unique': True, 'sparse': False}
        ... )
        IndexConfig(keys=[('name', -1), ('id', 1)], options={'unique': True, '\
sparse': False})
    """
    keys: Optional[Union[Dict, List[Tuple]]] = None
    options: Dict = dict()

    @field_validator("keys", mode="after")
    @classmethod
    def store_enum_value(
        cls,
        v: Optional[Union[Dict, List[Tuple]]]
    ) -> Optional[Union[Dict, List[Tuple]]]:
        """Convert dict values of keys into list of tuples"""
        if v is not None and isinstance(v, dict):
            v = [
                tuple([key, val]) for key, val in v.items()
            ]
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

    Example:
        >>> CollectionConfig(
        ...     indexes=[IndexConfig(keys={'last_name': 1})],
        ... )
        CollectionConfig(indexes=[IndexConfig(keys=[('last_name', 1)], options\
={})], client=None)}, client=None)
    """
    indexes: Optional[List[IndexConfig]] = None
    client: Optional[collection.Collection] = None


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
        ...             indexes=[IndexConfig(keys={'last_name': 1})],
        ...         ),
        ...     },
        ... )
        DBConfig(collections={'my_collection': CollectionConfig(indexes=[Index\
Config(keys=[('last_name', 1)], options={})], client=None)}, client=None)
    """
    collections: Optional[Dict[str, CollectionConfig]] = None
    client: Optional[database.Database] = None


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

    _validate_level = field_validator('level')(_validate_log_level_choices)


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

    _validate_level = field_validator('level')(_validate_log_level_choices)


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
        "standard": LogFormatterConfig(),  # type: ignore
    }
    handlers: Optional[Dict[str, LogHandlerConfig]] = {
        "console": LogHandlerConfig(),  # type: ignore
    }
    root: Optional[LogRootConfig] = LogRootConfig()


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

    Example:
        >>> Config()
        Config(server=ServerConfig(host='0.0.0.0', port=8080, debug=True, envi\
ronment='development', testing=False, use_reloader=True), exceptions=Exception\
Config(required_members=[['title'], ['status']], extension_members=False, stat\
us_member=['status'], public_members=None, private_members=None, exceptions='f\
oca.errors.exceptions.exceptions', logging=<ExceptionLoggingEnum.oneline: 'one\
line'>, mapping={<class 'Exception'>: {'title': 'Internal Server Error', 'stat\
us': 500}, <class 'werkzeug.exceptions.BadRequest'>: {'title': 'Bad Request', \
'status': 400}, <class 'connexion.exceptions.ExtraParameterProblem'>: {'title'\
: 'Bad Request', 'status': 400}, <class 'werkzeug.exceptions.Unauthorized'>: {\
'title': 'Unauthorized', 'status': 401}, <class 'connexion.exceptions.OAuthPro\
blem'>: {'title': 'Unauthorized', 'status': 401}, <class 'werkzeug.exceptions.\
Forbidden'>: {'title': 'Forbidden', 'status': 403}, <class 'werkzeug.exception\
s.NotFound'>: {'title': 'Not Found', 'status': 404}, <class 'werkzeug.exceptio\
ns.InternalServerError'>: {'title': 'Internal Server Error', 'status': 500}, <\
class 'werkzeug.exceptions.BadGateway'>: {'title': 'Bad Gateway', 'status': 50\
2}, <class 'werkzeug.exceptions.ServiceUnavailable'>: {'title': 'Service Unava\
ilable', 'status': 502}, <class 'werkzeug.exceptions.GatewayTimeout'>: {'title\
': 'Gateway Timeout', 'status': 504}}), api=APIConfig(specs=[]), security=Secu\
rityConfig(auth=AuthConfig(required=False, add_key_to_claims=True, allow_expir\
ed=False, audience=None, claim_identity='sub', claim_issuer='iss', algorithms=\
['RS256'], validation_methods=[<ValidationMethodsEnum.userinfo: 'userinfo'>, <\
ValidationMethodsEnum.public_key: 'public_key'>], validation_checks=<Validatio\
nChecksEnum.all: 'all'>), cors=CORSConfig(enabled=True), access_control=Access\
ControlConfig(api_specs='/path/to/access_control_spec.yaml', api_controllers='\
/path/to/access_control_spec_server.py', db_name='access_control_db', collecti\
on_name='access_control_collection', model='/path/to/policy.conf', owner_heade\
rs={'X-User', 'X-Group'}, user_headers={'X-User'})), db=None, jobs=None, log=L\
ogConfig(version=1, disable_existing_loggers=False, formatters={'standard': Lo\
gFormatterConfig(class_formatter='logging.Formatter', style='{', format='[{asc\
time}: {levelname:<8}] {message} [{name}]')}, handlers={'console': LogHandlerC\
onfig(class_handler='logging.StreamHandler', level=20, formatter='standard', s\
tream='ext://sys.stderr')}, root=LogRootConfig(level=10, handlers=['console'])\
))
    """
    server: ServerConfig = ServerConfig()
    exceptions: ExceptionConfig = ExceptionConfig()
    api: APIConfig = APIConfig()
    security: SecurityConfig = SecurityConfig()
    db: Optional[MongoConfig] = None
    jobs: Optional[JobsConfig] = None
    log: LogConfig = LogConfig()
    model_config = ConfigDict(extra='allow')
