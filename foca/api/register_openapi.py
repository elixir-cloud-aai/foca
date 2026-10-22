"""Register and modify OpenAPI specifications."""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from connexion import App
from connexion.exceptions import InvalidSpecification

from foca.config.config_parser import ConfigParser, deep_merge
from foca.models.config import ConnexionOptions, SpecConfig
from foca.security.access_control.enforcement import (
    AccessControlResolver,
    _requires_authentication,
)
from foca.security.access_control.ownership_server import _check_routes_enforced

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


# Options passed to Connexion 2 via the `options` argument of `add_api()`:
# fields of `ConnexionOptions` and the names of Connexion's options
_UI_OPTIONS = {
    "swagger_ui": "swagger_ui",
    "serve_spec": "serve_spec",
    "swagger_ui_path": "swagger_url",
}


def _connexion_add_api_kwargs(options: ConnexionOptions | None) -> dict[str, Any]:
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
        {name: values[key] for key, name in _UI_OPTIONS.items() if key in values}
    )
    # The Swagger UI needs the specification, so it is not served without it;
    # enabling it nevertheless is rejected on validation
    if options._spec_disabled() and ui_options.get("swagger_ui") is None:
        ui_options["swagger_ui"] = False
    if ui_options:
        kwargs["options"] = ui_options
    return kwargs


def _iter_routes(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Iterate over the Operation Objects of an OpenAPI specification.

    Args:
        spec: OpenAPI 2.x or 3.x specification.

    Yields:
        Path, HTTP method and Operation Object of each operation of all Path
        Item Objects, skipping other Path Item Object fields (e.g.,
        path-level `parameters`, `summary` or `$ref`).
    """
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return
    for path, path_item_object in paths.items():
        if not isinstance(path_item_object, dict):
            continue
        for field, operation_object in path_item_object.items():
            if field in _OPERATION_OBJECT_FIELDS and isinstance(operation_object, dict):
                yield path, field, operation_object


def _iter_operations(spec: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Iterate over the Operation Objects of an OpenAPI specification.

    Args:
        spec: OpenAPI 2.x or 3.x specification.

    Yields:
        Operation Objects of all Path Item Objects, skipping other Path Item
        Object fields (e.g., path-level `parameters`, `summary` or `$ref`).
    """
    for _, _, operation_object in _iter_routes(spec):
        yield operation_object


def _protected_routes(spec: dict[str, Any]) -> dict[str, set[str]]:
    """Get the protections of each operation of a specification.

    Args:
        spec: OpenAPI 2.x or 3.x specification.

    Returns:
        Protections of each operation (`<METHOD> <path>`): `security` if it
        requires authentication (operation-level or inherited security
        requirements, none of them empty), `access control` if it does not
        opt out of policy enforcement (via `x-foca-access-control: false` or
        `x-foca-self-service: true`).
    """
    protections: dict[str, set[str]] = {}
    for path, method, operation_object in _iter_routes(spec):
        protected = set()
        if _requires_authentication(
            operation_object.get("security", spec.get("security"))
        ):
            protected.add("security")
        if (
            operation_object.get("x-foca-access-control") is not False
            and operation_object.get("x-foca-self-service") is not True
        ):
            protected.add("access control")
        protections[f"{method.upper()} {path}"] = protected
    return protections


def _load_patch(patch: Path | dict[str, Any]) -> dict[str, Any]:
    """Load a patch of a specification.

    Args:
        patch: Path to a YAML or JSON file, or a mapping.

    Returns:
        Patch; empty if the file is empty.

    Raises:
        OSError: The file cannot be read.
        ValueError: The file cannot be parsed or is not a mapping.
    """
    if isinstance(patch, dict):
        return patch
    content = ConfigParser.parse_yaml(patch)
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ValueError(f"Patch '{patch}' is not a mapping")
    return content


def _apply_patches(
    spec: dict[str, Any], patches: list[Path | dict[str, Any]], label: str
) -> dict[str, Any]:
    """Deep-merge patches into a specification.

    Applied patches are logged, and a warning lists operations whose
    authentication or access control the patches remove.

    Args:
        spec: OpenAPI 2.x or 3.x specification.
        patches: Patches (cf. :py:class:`foca.models.config.SpecConfig`).
        label: Name or paths of the specification, for logs.

    Returns:
        Patched specification.

    Raises:
        OSError: A patch file cannot be read.
        ValueError: A patch file cannot be parsed or is not a mapping.
    """
    patched = spec
    for patch in patches:
        patched = deep_merge(patched, _load_patch(patch))
    logger.info(
        "Applied patches to API spec %s: %s",
        label,
        ", ".join(
            str(patch) if isinstance(patch, Path) else f"inline patch #{index}"
            for index, patch in enumerate(patches, start=1)
        ),
    )
    after = _protected_routes(patched)
    weakened = [
        route
        for route, protected in _protected_routes(spec).items()
        if route in after and protected - after[route]
    ]
    if weakened:
        logger.warning(
            "Patches of API spec %s remove authentication ('security') or "
            "access control ('x-foca-access-control', 'x-foca-self-service') "
            "from operations: %s. Make sure that this is intended.",
            label,
            ", ".join(weakened),
        )
    return patched


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
        OSError: Specification or patch cannot be read.
        yaml.YAMLError: Specification cannot be parsed.
        ValueError: Operations routed to the controllers of
            :py:mod:`foca.security.access_control.ownership_server` do not
            enforce access control policies, a patch cannot be parsed or is
            not a mapping, or the specification is invalid after applying
            patches.
    """
    # Iterate over OpenAPI specs
    for spec in specs:
        # Merge specs
        list_specs = [spec.path] if isinstance(spec.path, Path) else spec.path
        spec_parsed: dict[str, Any] = ConfigParser.merge_yaml(*list_specs)
        logger.debug("Parsed spec: %s", list_specs)

        # Add/replace root objects
        if spec.append is not None:
            for item in spec.append:
                spec_parsed.update(item)
            logger.debug("Appended spec: %s", spec.append)

        # Apply patches
        label = (
            f"'{spec.name}'"
            if spec.name is not None
            else ", ".join(str(path) for path in list_specs)
        )
        if spec.patches:
            spec_parsed = _apply_patches(spec_parsed, spec.patches, label)

        # Add fields to Operation Objects, keeping fields they set themselves
        if spec.add_operation_fields is not None:
            for operation_object in _iter_operations(spec_parsed):
                for key, val in spec.add_operation_fields.items():
                    operation_object.setdefault(key, val)
            logger.debug("Added operation fields: %s", spec.add_operation_fields)

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
            logger.debug("Added security fields: %s", spec.add_security_fields)

        # Remove security definitions/schemes and fields
        elif spec.disable_auth:
            if enforce_access_control:
                logger.warning(
                    "Authentication is disabled for spec %s "
                    "('disable_auth'), so access control policies are not "
                    "enforced on its operations ('access_control.enforce'): "
                    "they are public. Only disable authentication for local "
                    "development.",
                    list_specs,
                )
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
        logger.debug("Modified specs: %s", spec_parsed)
        connexion_kwargs = _connexion_add_api_kwargs(spec.connexion)
        if enforce_access_control:
            connexion_kwargs["resolver"] = AccessControlResolver(
                resolver=connexion_kwargs.get("resolver"),
            )
        try:
            app.add_api(
                specification=spec_parsed,
                **connexion_kwargs,
            )
        except InvalidSpecification as exc:
            if not spec.patches:
                raise
            raise ValueError(
                f"API spec {label} is invalid after applying patches: {exc.message}"
            ) from exc
        logger.info("API endpoints added from spec: %s", list_specs)

    _check_routes_enforced(app.app)
    return app
