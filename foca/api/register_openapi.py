"""Functions for registering OpenAPI specs with a Connexion app instance."""

from json import load, decoder
import logging
import os
from typing import Dict, List, Optional

from connexion import App
from yaml import safe_load, safe_dump, parser

from foca.config.config_parser import get_conf
from foca.errors.errors import InvalidSpecification


# Get logger instance
logger = logging.getLogger(__name__)


class OpenAPIConfig(Dict):
    """Helper class for the configuration parameters of the specification files.
    Inherits from the Dictionary class.

    Attributes (optional):
        out_file: The directory for the output specification file, in case it's
                either modified or in JSON format
        append: Fields to be added/modified in the root of the specification file
        add_operation_fields: Fields to be added/modified in Operation Objects
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
        specs_in: Dict[str, OpenAPIConfig]
) -> App:
    """
    Registers OpenAPI specs with Connexion app

    Args:
        app: A Connexion app instance
        specs_in: A Dictionary containing the path of configuration
                files as keys and OpenAPIConfig objects as values for
                further additions/modifications to the configuration

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
    for spec_path, spec in specs_in:
        modified = False
        with open(spec_path, 'r') as spec_file:
            try:
                specs = safe_load(spec_file)
            except parser.ParserError:
                try:
                    specs = load(spec_file)
                except decoder.JSONDecodeError:
                    raise InvalidSpecification

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
                    raise InvalidSpecification

        # Check if the configuration file has been modified or was of type json, and if an output file name
        # has been provided
        if any(['append', 'add_operation_fields']) in spec or get_conf(spec, 'type') == 'json':
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
