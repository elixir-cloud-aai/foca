"""Parser for YAML-based app configuration."""

from importlib import import_module
import logging
from logging.config import dictConfig
from pathlib import Path
from typing import (Dict, Optional)

from addict import Dict as Addict
from pydantic import BaseModel
import yaml

from foca.models.config import (Config, LogConfig)

logger = logging.getLogger(__name__)


class ConfigParser():
    """Parse FOCA config files.

    Args:
        config_file: Path to config file in YAML format.
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

    Attributes:
        config_file: Path to config file in YAML format.
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
        config_file: Optional[Path] = None,
        custom_config_model: Optional[str] = None,
        format_logs: bool = True
    ) -> None:
        """Constructor method."""
        if config_file is not None:
            self.config = Config(**self.parse_yaml(config_file))
        else:
            self.config = Config()
        if custom_config_model is not None:
            setattr(
                self.config,
                'custom',
                self.parse_custom_config(
                    model=custom_config_model,
                )
            )
        if format_logs:
            self._configure_logging()
        logger.debug(f"Parsed config: {self.config.dict(by_alias=True)}")

    def _configure_logging(self) -> None:
        """Configure logging."""
        try:
            dictConfig(self.config.log.dict(by_alias=True))
        except Exception as e:
            dictConfig(LogConfig().dict(by_alias=True))
            logger.warning(
                f"Failed to configure logging. Falling back to default "
                f"settings. Original error: {type(e).__name__}: {e}"
            )

    @staticmethod
    def parse_yaml(conf: Path) -> Dict:
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
                    raise ValueError(
                        f"file '{conf}' is not valid YAML"
                    ) from exc
        except OSError as exc:
            raise OSError(
                f"file '{conf}' could not be read"
            ) from exc

    @staticmethod
    def merge_yaml(*args: Path) -> Dict:
        """Parse and merge a set of YAML files.

        Merging is done iteratively, from the first, second to the n-th
        argument. Dictionary items are updated, not overwritten. For exact
        behavior cf. https://github.com/mewwts/addict.

        Args:
            *args: One or more paths to YAML files.

        Returns:
            Dictionary of merged YAML file contents, or ``None`` if no
            arguments have been supplied; if only a single YAML file path is
            provided, no merging is done.
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
            ``myapp.config.models.CustomConfig`, where ``CustomConfig`` is the
            actual importable name of a `pydantic` model for your custom
            configuration, deriving from ``BaseModel``.

        Returns:
            Custom configuration model instantiated with the parameters listed
                in the app configuration's ``custom``.
        """
        module = Path(model).stem
        model_class = Path(model).suffix[1:]
        try:
            model_class = getattr(import_module(module), model_class)
        except ModuleNotFoundError:
            raise ValueError(
                f"failed validating custom configuration: module '{module}' "
                "not available"
            )
        except (AttributeError, ImportError):
            raise ValueError(
                f"failed validating custom configuration: module '{module}' "
                f"has no class {model_class} or could not be imported"
            )
        try:
            custom_config = model_class(  # type: ignore[operator]
                **self.config.custom)  # type: ignore[attr-defined]
        except Exception as exc:
            raise ValueError(
                "failed validating custom configuration: provided custom "
                f"configuration does not match model class in '{model}'"
            ) from exc
        return custom_config
