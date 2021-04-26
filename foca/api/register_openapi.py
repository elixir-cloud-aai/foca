"""Register and modify OpenAPI specifications."""

import logging
from typing import List

from connexion import App
import yaml

from foca.models.config import SpecConfig
from foca.config.config_parser import ConfigParser

# Get logger instance
logger = logging.getLogger(__name__)


def register_openapi(
        app: App,
        specs: List[SpecConfig],
) -> App:
    """
    Register OpenAPI specifications with Connexion application instance.

    Args:
        app: Connexion application instance.
        specs: Sequence of :py:class:`foca.models.config.SpecConfig` instances
            describing OpenAPI 2.x and/or 3.x specifications to be registered
            with `app`.

    Returns:
        Connexion application instance with registered OpenAPI specifications.

    Raises:
        OSError: Modified specification cannot be written.
        yaml.YAMLError: Modified specification cannot be serialized.
    """
    # Iterate over OpenAPI specs
    for spec in specs:

        # Merge specs
        spec_parsed = ConfigParser.merge_yaml(*spec.path)
        logger.debug(f"Parsed spec: {spec.path}")

        # Add/replace root objects
        if spec.append is not None:
            for item in spec.append:
                spec_parsed.update(item)
            logger.debug(f"Appended spec: {spec.append}")

        # Add/replace fields to Operation Objects
        if spec.add_operation_fields is not None:
            for key, val in spec.add_operation_fields.items():
                for path_item_object in spec_parsed.get('paths', {}).values():
                    for operation_object in path_item_object.values():
                        operation_object[key] = val
            logger.debug(
                f"Added operation fields: {spec.add_operation_fields}"
            )

        # Add fields to security definitions/schemes
        if not spec.disable_auth and spec.add_security_fields is not None:
            for key, val in spec.add_security_fields.items():
                # OpenAPI 2
                sec_defs = spec_parsed.get('securityDefinitions', {})
                for sec_def in sec_defs.values():
                    sec_def[key] = val
                # OpenAPI 3
                sec_schemes = spec_parsed.get(
                    'components', {'securitySchemes': {}}
                ).get('securitySchemes', {})  # type: ignore
                for sec_scheme in sec_schemes.values():
                    sec_scheme[key] = val
            logger.debug(f"Added security fields: {spec.add_security_fields}")

        # Remove security definitions/schemes and fields
        elif spec.disable_auth:
            # Open API 2
            spec_parsed.pop('securityDefinitions', None)
            # Open API 3
            spec_parsed.get('components', {}).pop('securitySchemes', None)
            # Open API 2/3
            spec_parsed.pop('security', None)
            for path_item_object in spec_parsed.get('paths', {}).values():
                for operation_object in path_item_object.values():
                    operation_object.pop('security', None)
            logger.debug("Removed security fields")

        # Write modified specs
        try:
            with open(spec.path_out, 'w') as out_file:  # type: ignore
                yaml.safe_dump(spec_parsed, out_file)
        except OSError as e:
            raise OSError(
                "Modified specification could not be written to file "
                f"'{spec.path_out}'"
            ) from e
        except yaml.YAMLError as e:
            raise yaml.YAMLError(
                "Could not encode modified specification"
            ) from e
        logger.debug(f"Wrote specs to file: {spec.path_out}")

        # Attach specs to connexion App
        logger.debug(f"Modified specs: {spec_parsed}")
        spec.connexion = {} if spec.connexion is None else spec.connexion
        app.add_api(
            specification=spec.path_out,
            **spec.dict().get('connexion', {}),
        )
        logger.info(f"API endpoints added from spec: {spec.path_out}")

    return app
