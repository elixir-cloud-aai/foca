"""Register OpenAPI specs with a Connexion app instance.
"""

import logging
import os
from typing import (Dict, Iterable, Optional)

from connexion import App
from connexion.exceptions import InvalidSpecification
from pydantic import (BaseModel, validator)
import yaml

# Get logger instance
logger = logging.getLogger(__name__)


class OpenAPIConfig(BaseModel):
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
        >>> OpenAPIConfig(
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
    append: Optional[Iterable[Dict]] = None
    add_operation_fields: Optional[Dict] = None
    connexion: Optional[Dict] = None

    # raise error if additional arg is passed
    class Config:
        extra = 'forbid'

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


def register_openapi(
        app: App,
        specs: Iterable[OpenAPIConfig],
) -> App:
    """
    Register OpenAPI specs with Connexion app.

    Args:
        app: A Connexion app instance.
        specs: An `Iterable` of `OpenAPIConfig` objects describing OpenAPI 2.x
            and/or 3.x specifications to be registered with `app`.

    Returns:
        A Connexion app instance.

    Raises:
        OSError: Specification file cannot be accessed.
        InvalidSpecification: Specification file is not valid OpenAPI 2.x or
            3.x.
        yaml.YAMLError: Modified specification cannot be serialized.
    """
    # Iterate over API specs
    for spec in specs:
        spec_modified = False
        try:
            with open(spec.path, 'r') as spec_file:
                try:
                    spec_parsed = yaml.safe_load(spec_file)
                except yaml.parser.ParserError:
                    raise InvalidSpecification(
                        f"specification '{spec.path}' is not valid YAML"
                    )
        except OSError as e:
            raise OSError(
                f"specification file '{spec.path}' could not be read"
            ) from e

        # Add/replace root objects
        if spec.append is not None:
            for item in spec.append:
                spec_parsed.update(item)
            spec_modified = True

        # Add/replace fields to Operation Objects
        if spec.add_operation_fields is not None:
            for key, val in spec.add_operation_fields.items():
                try:
                    for path_item_object in spec_parsed['paths'].values():
                        for operation_object in path_item_object.values():
                            operation_object[key] = val
                except KeyError:
                    raise InvalidSpecification("invalid Operation Object")
            spec_modified = True

        # Write modified specs
        if spec_modified:
            try:
                with open(spec.path_out, 'w') as out_file:  # type: ignore
                    try:
                        yaml.safe_dump(spec_parsed, out_file)
                    except yaml.YAMLError as e:
                        raise yaml.YAMLError(
                            "could not encode modified specification"
                        ) from e
            except OSError as e:
                raise OSError(
                    "modified specification could not be written to file "
                    f"'{spec.path_out}'"
                ) from e
            spec_use = spec.path_out
        else:
            spec_use = spec.path

        # Attach specs to connexion App
        if spec.connexion is None:
            spec.connexion = {}
        app.add_api(
            specification=spec_use,
            **spec.dict()['connexion'],
        )

        logger.info(f"API endpoints specified in '{spec.path_out}' added.")

    return app
