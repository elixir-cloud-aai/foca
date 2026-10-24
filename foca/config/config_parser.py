"""Parser for YAML-based app configuration."""

import logging
import os
from collections.abc import Sequence
from copy import deepcopy
from importlib import import_module
from logging.config import dictConfig
from pathlib import Path
from typing import Any

import yaml
from addict import Dict as Addict
from pydantic import BaseModel

from foca.models.config import Config, LogConfig

logger = logging.getLogger(__name__)

# Environment variable listing config files to merge into the app's config,
# separated by `os.pathsep` (e.g., `:` on Linux)
CONFIG_OVERLAY_ENV_VAR = "FOCA_CONFIG_OVERLAY"


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge dictionaries recursively.

    Args:
        base: Dictionary to merge into.
        overlay: Dictionary to merge; mappings are merged recursively, while
            all other values (including lists and ``None``) replace those in
            `base`.

    Returns:
        Merged dictionary; the arguments are not modified.
    """
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


# Sections that can be given as mappings of names to entries (named entries),
# so that config overlays can add, change or remove single entries
NAMED_ENTRY_SECTIONS: tuple[tuple[str, ...], ...] = (
    ("api", "specs"),
    ("security", "access_control", "policies"),
)


def _get_section(data: Any, keys: Sequence[str]) -> Any:
    """Get the value of a nested section, or `None` if it is not set."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _check_named_entries(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Check that an overlay can be merged into the named entries of a base.

    Args:
        base: Configuration merged so far.
        overlay: Configuration to merge.

    Raises:
        ValueError: The overlay sets named entries of a section (cf.
            `NAMED_ENTRY_SECTIONS`) that is a non-empty list in `base`, so
            that the entries of the list would be lost, or it removes
            (`null`) entries that are not set in `base`, e.g., because of a
            misspelled name.
    """
    for keys in NAMED_ENTRY_SECTIONS:
        entries = _get_section(overlay, keys)
        if not isinstance(entries, dict):
            continue
        section = ".".join(keys)
        current = _get_section(base, keys)
        if isinstance(current, list) and current:
            raise ValueError(
                f"Cannot change '{section}' to named entries, as earlier config "
                "files set it to a list, whose entries would be lost; give the "
                "complete list instead, or use named entries in all files"
            )
        known = current if isinstance(current, dict) else {}
        unknown = [
            str(name)
            for name, entry in entries.items()
            if entry is None and name not in known
        ]
        if unknown:
            raise ValueError(
                f"Config removes unknown entries of '{section}': {', '.join(unknown)}"
            )


def _as_paths(config_file: Path | str | Sequence[Path | str] | None) -> list[Any]:
    """Normalize one or more config file paths to a list of paths."""
    if config_file is None:
        return []
    if isinstance(config_file, (str, os.PathLike)):
        return [Path(config_file)]
    return [Path(path) for path in config_file]


def get_overlay_files() -> list[Path]:
    """Get config files listed in environment variable `FOCA_CONFIG_OVERLAY`.

    Returns:
        Absolute paths of the listed files, in order; relative paths are
        resolved against the current working directory.

    Raises:
        FileNotFoundError: A listed file does not exist.
    """
    value = os.environ.get(CONFIG_OVERLAY_ENV_VAR, "")
    paths = [Path(path).resolve() for path in value.split(os.pathsep) if path]
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(
                f"Config overlay '{path}' listed in environment variable "
                f"'{CONFIG_OVERLAY_ENV_VAR}' not found"
            )
    return paths


class ConfigParser:
    """Parse FOCA config files.

    Config files are merged in order (cf. :py:func:`deep_merge`), followed by
    any files listed in environment variable `FOCA_CONFIG_OVERLAY`, and the
    result is validated. Sections of `NAMED_ENTRY_SECTIONS` (API specs and
    access control policies) can be given as mappings of names to entries,
    so that later files can add, change (merge) or remove (`null`) single
    entries; files must not change such a section from a non-empty list to
    named entries, nor remove entries that are not set. Relative file paths
    in the configuration (e.g., of API specifications) are resolved against
    the directory of the first config file (the base config file), or
    against the current working directory if no config file is given.

    Args:
        config_file: Path to config file in YAML format, or sequence of paths
            to config files to be merged.
        custom_config_model: Path to model to be used for custom config
            parameter validation, supplied in "dot notation", e.g.,
            ``myapp.config.models.CustomConfig`, where ``CustomConfig`` is the
            actual importable name of a `pydantic` model for your custom
            configuration, deriving from ``BaseModel``. FOCA will attempt to
            instantiate the model with the values passed to the ``custom``
            section in the application's configuration, if present. Wherever
            possible, make sure that default values are supplied for each
            config parameters, so as to make it easier for others to
            write/modify their app configuration.
        format_logs: Whether log formatting should be configured.
        gateway: Whether the app is a gateway; if so, configurations with
            keys of earlier middleware stacks are refused with a migration
            hint (cf. :py:func:`foca.plugins.loader.check_legacy_keys`).

    Attributes:
        config_files: Paths to config files merged into the configuration,
            including overlays from `FOCA_CONFIG_OVERLAY`.
        config_dir: Directory that relative paths in the configuration are
            resolved against.
        custom_config_model: Path to model to be used for custom config
            parameter validation, supplied in "dot notation", e.g.,
            ``myapp.config.models.CustomConfig`, where ``CustomConfig`` is the
            actual importable name of a `pydantic` model for your custom
            configuration, deriving from ``BaseModel``. FOCA will attempt to
            instantiate the model with the values passed to the ``custom``
            section in the application's configuration, if present. Wherever
            possible, make sure that default values are supplied for each
            config parameters, so as to make it easier for others to
            write/modify their app configuration.
        format_logs: Whether log formatting should be configured.
    """

    def __init__(
        self,
        config_file: Path | str | Sequence[Path | str] | None = None,
        custom_config_model: str | None = None,
        format_logs: bool = True,
        gateway: bool = False,
    ) -> None:
        """Constructor method."""
        config_files = _as_paths(config_file)
        self.config_dir: Path = (
            config_files[0].resolve().parent if config_files else Path.cwd()
        )
        self.config_files: list[Path] = config_files + get_overlay_files()
        data: dict[str, Any] = {}
        for path in self.config_files:
            content = self.parse_yaml(path)
            if content is None:
                content = {}
            if not isinstance(content, dict):
                raise ValueError(f"file '{path}' is not a YAML mapping")
            try:
                _check_named_entries(data, content)
            except ValueError as exc:
                raise ValueError(f"file '{path}': {exc}") from exc
            data = deep_merge(data, content)
        if gateway:
            # Before validating `custom`, so that apps' strict custom models
            # don't reject the keys first, without the hint
            from foca.plugins.loader import check_legacy_keys  # noqa: PLC0415

            check_legacy_keys(data)
        self.config = Config.model_validate(data, context={"base_dir": self.config_dir})
        if custom_config_model is not None:
            setattr(
                self.config,
                "custom",
                self.parse_custom_config(
                    model=custom_config_model,
                ),
            )
        if format_logs:
            self._configure_logging()
        if self.config_files:
            logger.info(
                "Configuration files merged: %s",
                ", ".join(str(path) for path in self.config_files),
            )
        # Section names only, as values (e.g., in `custom`) may be secrets
        logger.debug(
            "Parsed config sections: %s",
            ", ".join(sorted(self.config.model_dump().keys())),
        )

    def _configure_logging(self) -> None:
        """Configure logging."""
        try:
            dictConfig(self.config.log.model_dump(by_alias=True))
        except Exception as e:
            dictConfig(LogConfig().model_dump(by_alias=True))
            logger.warning(
                "Failed to configure logging. Falling back to default "
                "settings. Original error: %s: %s",
                type(e).__name__,
                e,
            )

    @staticmethod
    def parse_yaml(conf: Path) -> dict[str, Any]:
        """Parse YAML file.

        Args:
            conf: Path to YAML file.

        Returns:
            Dictionary of `conf` contents.

        Raises:
            OSError: File cannot be accessed.
            ValueError: File contents cannot be parsed.
        """
        try:
            with open(conf) as config_file:
                try:
                    return yaml.safe_load(config_file)
                except yaml.YAMLError as exc:
                    raise ValueError(f"file '{conf}' is not valid YAML") from exc
        except OSError as exc:
            raise OSError(f"file '{conf}' could not be read") from exc

    @staticmethod
    def merge_yaml(*args: Path) -> dict[str, Any]:
        """Parse and merge a set of YAML files.

        Merging is done iteratively, from the first, second to the n-th
        argument. Dictionary items are updated, not overwritten. For exact
        behavior cf. https://github.com/mewwts/addict.

        Args:
            *args: One or more paths to YAML files.

        Returns:
            Dictionary of merged YAML file contents, or an empty dictionary if
            no arguments have been supplied; if only a single YAML file path
            is provided, no merging is done.
        """
        args_list = list(args)
        if not args_list:
            return {}
        yaml_dict = Addict(ConfigParser.parse_yaml(args_list.pop(0)))

        for arg in args_list:
            yaml_dict.update(Addict(ConfigParser.parse_yaml(arg)))

        return yaml_dict.to_dict()

    def parse_custom_config(self, model: str) -> BaseModel:
        """Parse custom configuration and validate against a model.

        The method will attempt to instantiate the model with the parameters
        provided in the application configuration's ``custom`` section, if
        provided. Any required configuration parameters for which no defaults
        are provided in the model indeed will have to be listed in such a
        section.

        Args:
            model: Path to model to be used for configuration validation,
                supplied in "dot notation", e.g.,
                ``myapp.config.models.CustomConfig`, where ``CustomConfig`` is
                the actual importable name of a `pydantic` model for your
                custom configuration, deriving from ``BaseModel``.

        Returns:
            Custom configuration model instantiated with the parameters listed
            in the app configuration's ``custom``.
        """
        module, _, model_name = model.rpartition(".")
        try:
            model_class = getattr(import_module(module), model_name)
        except ModuleNotFoundError as exc:
            raise ValueError(
                f"failed validating custom configuration: module '{module}' "
                "not available"
            ) from exc
        except (AttributeError, ImportError, ValueError) as exc:
            raise ValueError(
                f"failed validating custom configuration: module '{module}' "
                f"has no class {model_name} or could not be imported"
            ) from exc
        custom = getattr(self.config, "custom", None) or {}
        try:
            custom_config = model_class(**custom)
        except Exception as exc:
            raise ValueError(
                "failed validating custom configuration: provided custom "
                f"configuration does not match model class in '{model}'"
            ) from exc
        return custom_config
