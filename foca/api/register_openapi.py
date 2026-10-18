"""Register and modify OpenAPI specifications."""

import logging
from collections.abc import Iterator
from pathlib import Path

from connexion import App

from foca.config.config_parser import ConfigParser
from foca.models.config import ConnexionOptions, SpecConfig
from foca.security.access_control.enforcement import AccessControlResolver

# Get logger instance
logger = logging.getLogger(__name__)

# Path Item object fields which contain an Operation object (ie: HTTP verbs).
# Reference: https://swagger.io/specification/v3/#path-item-object
_OPERATION_OBJECT_FIELDS = frozenset(
    {
        "get",
        "put",
        "post",
        "delete",
        "options",
        "head",
        "patch",
        "trace",
    }
)


# Options passed to Connexion 2 via the `options` argument of `add_api()`
_UI_OPTIONS = ("swagger_ui", "serve_spec", "swagger_ui_path")


def _connexion_add_api_kwargs(options: ConnexionOptions | None) -> dict:
    """Translate options to keyword arguments of Connexion's `add_api()`.

    This is the only place where FOCA's Connexion options are mapped to
    Connexion's API.

    Args:
        options: Options for registering an API specification.

    Returns:
        Keyword arguments for `add_api()`; unset options are omitted.
    """
    if options is None:
        return {}
    values = options.model_dump(exclude_none=True)
    kwargs = {
        key: value
        for key, value in values.items()
        if key not in (*_UI_OPTIONS, "options")
    }
    ui_options = dict(values.get("options", {}))
    ui_options.update(
        {key: values[key] for key in _UI_OPTIONS if key in values}
    )
    if ui_options:
        kwargs["options"] = ui_options
    return kwargs


def _iter_operations(spec: dict) -> Iterator[dict]:
    """Iterate over the Operation Objects of an OpenAPI specification.

    Args:
        spec: OpenAPI 2.x or 3.x specification.

    Yields:
        Operation Objects of all Path Item Objects, skipping other Path Item
        Object fields (e.g., path-level `parameters`, `summary` or `$ref`).
    """
    for path_item_object in spec.get("paths", {}).values():
        if not isinstance(path_item_object, dict):
            continue
        for field, operation_object in path_item_object.items():
            if field in _OPERATION_OBJECT_FIELDS and isinstance(
                operation_object, dict
            ):
                yield operation_object


def register_openapi(
    app: App,
    specs: list[SpecConfig],
    enforce_access_control: bool = False,
) -> App:
    """Register OpenAPI specifications with Connexion application instance.

    Args:
        app: Connexion application instance.
        specs: Sequence of :py:class:`foca.models.config.SpecConfig` instances
            describing OpenAPI 2.x and/or 3.x specifications to be registered
            with `app`.
        enforce_access_control: Whether to enforce access control policies
            on operations that require authentication and do not set
            `x-foca-access-control: false`.

    Returns:
        Connexion application instance with registered OpenAPI specifications.

    Raises:
        OSError: Specification cannot be read.
        yaml.YAMLError: Specification cannot be parsed.
    """
    # Iterate over OpenAPI specs
    for spec in specs:
        # Merge specs
        list_specs = [spec.path] if isinstance(spec.path, Path) else spec.path
        spec_parsed: dict = ConfigParser.merge_yaml(*list_specs)
        logger.debug(f"Parsed spec: {list_specs}")

        # Add/replace root objects
        if spec.append is not None:
            for item in spec.append:
                spec_parsed.update(item)
            logger.debug(f"Appended spec: {spec.append}")

        # Add/replace fields to Operation Objects
        if spec.add_operation_fields is not None:
            for operation_object in _iter_operations(spec_parsed):
                operation_object.update(spec.add_operation_fields)
            logger.debug(
                f"Added operation fields: {spec.add_operation_fields}"
            )

        # Add fields to security definitions/schemes
        if not spec.disable_auth and spec.add_security_fields is not None:
            for key, val in spec.add_security_fields.items():
                # OpenAPI 2
                sec_defs = spec_parsed.get("securityDefinitions", {})
                for sec_def in sec_defs.values():
                    sec_def[key] = val
                # OpenAPI 3
                components = spec_parsed.get("components") or {}
                sec_schemes = components.get("securitySchemes") or {}
                for sec_scheme in sec_schemes.values():
                    sec_scheme[key] = val
            logger.debug(f"Added security fields: {spec.add_security_fields}")

        # Remove security definitions/schemes and fields
        elif spec.disable_auth:
            # Open API 2
            spec_parsed.pop("securityDefinitions", None)
            # Open API 3
            spec_parsed.get("components", {}).pop("securitySchemes", None)
            # Open API 2/3
            spec_parsed.pop("security", None)
            for operation_object in _iter_operations(spec_parsed):
                operation_object.pop("security", None)
            logger.debug("Removed security fields")

        # Attach specs to connexion App
        logger.debug(f"Modified specs: {spec_parsed}")
        connexion_kwargs = _connexion_add_api_kwargs(spec.connexion)
        if enforce_access_control:
            connexion_kwargs["resolver"] = AccessControlResolver(
                resolver=connexion_kwargs.get("resolver"),
            )
        app.add_api(
            specification=spec_parsed,
            **connexion_kwargs,
        )
        logger.info(f"API endpoints added from spec: {list_specs}")

    return app
