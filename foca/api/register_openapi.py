"""Register and modify OpenAPI specifications."""

import logging
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml
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
        for method, operation_object in _iter_path_item(path_item_object):
            yield path, method, operation_object


def _iter_path_item(
    path_item_object: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Iterate over the Operation Objects of a Path Item Object.

    Args:
        path_item_object: Path Item Object.

    Yields:
        HTTP method and Operation Object of each operation.
    """
    for field, operation_object in path_item_object.items():
        if field in _OPERATION_OBJECT_FIELDS and isinstance(operation_object, dict):
            yield field, operation_object


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


def _add_operation_fields(spec: dict[str, Any], fields: dict[str, Any]) -> None:
    """Add fields to the Operation Objects of a specification, in place.

    Fields that operations set themselves are kept.

    Args:
        spec: OpenAPI 2.x or 3.x specification.
        fields: Fields to add.
    """
    for operation_object in _iter_operations(spec):
        for key, val in fields.items():
            operation_object.setdefault(key, val)


def _resolve_path_item(
    spec: dict[str, Any], path_item: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a local reference (`$ref`) of a Path Item Object.

    As Connexion does, the fields of the referenced object replace those set
    next to the reference.

    Args:
        spec: OpenAPI 2.x or 3.x specification.
        path_item: Path Item Object.

    Returns:
        Path Item Object with local references resolved (up to a depth of
        10); references that cannot be resolved to a mapping (e.g., to other
        files) are kept.
    """
    for _ in range(10):
        ref = path_item.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return path_item
        target: Any = spec
        for token in ref[2:].split("/"):
            key = unquote(token).replace("~1", "/").replace("~0", "~")
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return path_item
        path_item = {
            **{key: val for key, val in path_item.items() if key != "$ref"},
            **target,
        }
    return path_item


def _protections(spec: dict[str, Any]) -> dict[str, set[str]]:
    """Get the protections of the operations of a specification.

    Path items given via local references are resolved, as by Connexion.

    Args:
        spec: OpenAPI 2.x or 3.x specification.

    Returns:
        Protections of each operation (`<METHOD> <path>`): `security` if it
        requires authentication (operation-level or inherited security
        requirements, none of them empty), `access control` if it does not
        opt out of policy enforcement (via `x-foca-access-control: false` or
        `x-foca-self-service: true`). Path items with references that cannot
        be resolved (e.g., to other files) are listed as `<path>`, with
        protection `reference <$ref>`.
    """
    protections: dict[str, set[str]] = {}
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return protections
    for path, path_item_object in paths.items():
        if not isinstance(path_item_object, dict):
            continue
        resolved = _resolve_path_item(spec, path_item_object)
        if "$ref" in resolved:
            protections[path] = {f"reference {resolved['$ref']}"}
        for method, operation_object in _iter_path_item(resolved):
            protected = set()
            security = operation_object.get("security", spec.get("security"))
            # Invalid values are rejected by Connexion
            if isinstance(security, list) and _requires_authentication(security):
                protected.add("security")
            if (
                operation_object.get("x-foca-access-control") is not False
                and operation_object.get("x-foca-self-service") is not True
            ):
                protected.add("access control")
            protections[f"{method.upper()} {path}"] = protected
    return protections


def _security_schemes(spec: dict[str, Any]) -> Any:
    """Get the security definitions (OpenAPI 2) and schemes (OpenAPI 3)."""
    components = spec.get("components")
    return (
        spec.get("securityDefinitions"),
        components.get("securitySchemes") if isinstance(components, dict) else None,
    )


def _warn_weakened(before: dict[str, Any], after: dict[str, Any], label: str) -> None:
    """Warn of protections that patches remove from a specification.

    Warns of operations that are new or required authentication or were
    subject to access control before the patches, but not after, and of
    changed security definitions or schemes.

    Args:
        before: Specification before the patches.
        after: Specification after the patches.
        label: Name or paths of the specification, for logs.
    """
    protected_before = _protections(before)
    weakened = []
    for route, protected in _protections(after).items():
        if route not in protected_before:
            # new operations (and references) are compared with full
            # protection, so that new or renamed public operations are listed
            if protected != {"security", "access control"}:
                weakened.append(route)
        elif protected_before[route] - protected:
            weakened.append(route)
    if weakened:
        logger.warning(
            "Patches of API spec %s add or change operations so that they do "
            "not require authentication ('security') or opt out of access "
            "control ('x-foca-access-control', 'x-foca-self-service'), or "
            "add references to other files: %s. Make sure that this is "
            "intended.",
            label,
            ", ".join(weakened),
        )
    if _security_schemes(before) != _security_schemes(after):
        logger.warning(
            "Patches of API spec %s change its security definitions or "
            "schemes. Make sure that this is intended.",
            label,
        )


def _load_patch(patch: Path | dict[str, Any]) -> dict[str, Any]:
    """Load a patch of a specification.

    Args:
        patch: Path to a YAML or JSON file, or a mapping.

    Returns:
        Patch; empty if the file is empty or has comments only.

    Raises:
        OSError: The file cannot be read.
        ValueError: The file cannot be parsed or is not a mapping.
    """
    if isinstance(patch, dict):
        return patch
    content = ConfigParser.parse_yaml(patch)
    # Empty files (also with comments only) have no document, unlike `null`
    if content is None and yaml.compose(patch.read_text()) is None:
        return {}
    if not isinstance(content, dict):
        raise ValueError(f"Patch '{patch}' is not a mapping")
    return content


def _apply_patches(
    spec: dict[str, Any], patches: list[Path | dict[str, Any]], label: str
) -> dict[str, Any]:
    """Deep-merge patches into a specification.

    Applied patches are logged.

    Args:
        spec: OpenAPI 2.x or 3.x specification.
        patches: Patches (cf. :py:class:`foca.models.config.SpecConfig`).
        label: Name or paths of the specification, for logs.

    Returns:
        Patched specification.

    Raises:
        OSError: A patch file cannot be read.
        ValueError: A patch file cannot be parsed or is not a mapping, or
            patches set fields of path items defined via `$ref` (Connexion
            replaces them with the fields of the referenced object, so the
            patches would not take effect).
    """
    patched = spec
    patched_paths: set[str] = set()
    for patch in patches:
        content = _load_patch(patch)
        patch_paths = content.get("paths")
        if isinstance(patch_paths, dict):
            patched_paths.update(
                path
                for path, path_item in patch_paths.items()
                if isinstance(path_item, dict) and set(path_item) - {"$ref"}
            )
        patched = deep_merge(patched, content)
    paths = patched.get("paths")
    overridden = sorted(
        path
        for path in patched_paths
        if isinstance(paths, dict)
        and isinstance(paths.get(path), dict)
        and "$ref" in paths[path]
        and set(paths[path]) - {"$ref"}
    )
    if overridden:
        raise ValueError(
            f"Patches of API spec {label} set fields of path items defined via "
            "'$ref', which the fields of the referenced objects replace: "
            f"{', '.join(overridden)}; patch the referenced objects instead"
        )
    logger.info(
        "Applied patches to API spec %s: %s",
        label,
        ", ".join(
            str(patch) if isinstance(patch, Path) else f"inline patch #{index}"
            for index, patch in enumerate(patches, start=1)
        ),
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
            not a mapping, patches set fields of path items defined via
            `$ref`, or the specification is invalid after applying patches.
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
        unpatched: dict[str, Any] | None = None
        if spec.patches:
            # Compare protections with operation fields added, as they are
            # added after patches
            unpatched = deepcopy(spec_parsed)
            if spec.add_operation_fields is not None:
                _add_operation_fields(unpatched, spec.add_operation_fields)
            spec_parsed = _apply_patches(spec_parsed, spec.patches, label)

        # Add fields to Operation Objects, keeping fields they set themselves
        if spec.add_operation_fields is not None:
            _add_operation_fields(spec_parsed, spec.add_operation_fields)
            logger.debug("Added operation fields: %s", spec.add_operation_fields)
        if unpatched is not None:
            _warn_weakened(unpatched, spec_parsed, label)

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
        except Exception:
            if spec.patches:
                logger.error(
                    "Registering API spec %s failed; patches were applied to it, "
                    "check whether they cause the error",
                    label,
                )
            raise
        logger.info("API endpoints added from spec: %s", label)

    _check_routes_enforced(app.app)
    return app
