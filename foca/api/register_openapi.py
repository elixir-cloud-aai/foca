"""Helper class & function definitions for registering OpenAPI specs with a
Connexion app instance."""

from json import load, decoder
import logging
import os
from typing import Dict, List, Optional

from connexion import App
from connexion.exceptions import InvalidSpecification
from yaml import (safe_load,
                  safe_dump,
                  parser,
                  )

from foca.config.config_parser import get_conf

# Get logger instance
logger = logging.getLogger(__name__)


class OpenAPIConfig(Dict):
    """Helper class for the configuration parameters of the specification files.
    Inherits from the Dictionary class. The purpose of this class is to help
    the user neatly pass any OpenAPI preferences/configurations in an easily
    verifiable way, that follows the FOCA vocabulary and structure.

    Attributes (optional):
        out_file: The directory for the output specification file, in case it's
                either modified or in JSON format. If no path is specified by
                the user, the output will be stored in the source directory,
                under a modified filename
        append: Fields to be added/modified in the root of the specification
                file. By making use of this list of dictionaries object, the
                user can add or modify specific OpenAPI2 or OpenAPI3 specs.
                For OpenAPI 2, see https://swagger.io/specification/v2/
                For OpenAPI 3, see
                https://swagger.io/specification/#oas-document
        add_operation_fields: Fields to be added/modified in Operation Objects.
                By making use of this dictionary object, the user can add or
                modify specific fields of the Operation Objects of each Path
                Info Object fo the required paths field; this is mostly to
                support the addition (or replacement) of the
                x-swagger-router-controller field
                For OpenAPI 2, see
                https://swagger.io/specification/v2/#operation-object
                For OpenAPI 3, see
                https://swagger.io/specification/#operation-object

        An example OpenAPIConfig instance would look like this:
            my_specs_config = OpenAPIConfig(
                out_file='/path/to/my/modified/specs.yaml',
                append=[
                    {
                        'security':
                            'jwt':
                                'type': 'apiKey',
                                'name': 'Authorization',
                                'in': 'header',
                    },
                    {
                       'my_other_root_field': 'some_value',
                    },
                ],
                add_operation_fields = {
                    'x-swagger-router-controller': 'controllers.ga4gh.wes',
                    'some-other-custom-field': 'some_value',
                },
            )
    """

    def __init__(
            self,
            out_file: Optional[str],
            append: Optional[List[Dict]],
            add_operation_fields: Optional[Dict]
    ) -> None:
        super().__init__()
        if out_file:
            self.out_file = out_file
        if append:
            self.append = append
        if add_operation_fields:
            self.add_operation_fields = add_operation_fields


def register_openapi(
        app: App,
        specs: Dict[str, OpenAPIConfig]
) -> App:
    """
    Registers OpenAPI specs with Connexion app

    Args:
        app: A Connexion app instance
        specs: A Dictionary containing the absolute path of OpenAPI
            specification files as keys and OpenAPIConfig objects as
            values for further additions/modifications to the configuration.

    Returns:
        A Connexion app instance

    Raises:
        InvalidSpecification: The InvalidSpecification exception is
                            raised in case the configuration file is
                            malformed (invalid YAML/JSON formats) or
                            in the case of invalid specs field access.
        FileNotFoundError: Any of the files were not found.
        PermissionError: Any of the files were not accessible.
    """
    # Iterate over list of API specs
    for spec_path, spec in specs:
        with open(spec_path, 'r') as spec_file:
            try:
                specs = safe_load(spec_file)
            except parser.ParserError:
                try:
                    specs = load(spec_file)
                except decoder.JSONDecodeError:
                    raise InvalidSpecification("The specification object\
                     is not in a valid YAML/JSON format")

        # Add/replace content to the root of the specs
        if 'append' in spec:
            for item in spec['append']:
                specs.update(item)

        # Add/replace fields to Operation Objects
        if 'add_operation_fields' in spec:
            for key, val in spec['add_operation_fields'].items():
                try:
                    for path_item_object in specs['paths'].values():
                        for operation_object in path_item_object.values():
                            operation_object[key] = val
                except (AttributeError, KeyError):
                    raise InvalidSpecification("Invalid Operation Object\
                     access")

        # Check if the configuration file has been modified or was of type
        # json, and if an output file name has been provided
        if any(['append', 'add_operation_fields']) in spec or \
                get_conf(spec, 'type') == 'json':
            if 'out_file' in spec:
                spec_path = spec['out_file']
            else:
                spec_path = os.path.splitext(spec_path)[0] + 'modified.yaml'

        # Generate the output file
        with open(spec_path, 'w') as out_file:
            safe_dump(specs, out_file)

        # Generate API endpoints from OpenAPI spec
        try:
            app.add_api(
                spec_path,
                strict_validation=get_conf(spec, 'strict_validation'),
                validate_responses=get_conf(spec, 'validate_responses'),
                swagger_ui=get_conf(spec, 'swagger_ui'),
                swagger_json=get_conf(spec, 'swagger_json'),
            )

            logger.info("API endpoints specified in '{path}' added.".format(
                path=spec_path,
            ))

        except (FileNotFoundError, PermissionError) as e:
            logger.critical(
                (
                    "API specification file not found or accessible at "
                    "'{path}'. Execution aborted. Original error message: "
                    "{type}: {msg}"
                ).format(
                    path=spec_path,
                    type=type(e).__name__,
                    msg=e,
                )
            )
            raise SystemExit(1)

    return app
