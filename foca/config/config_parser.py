"""Parser for YAML-based app configuration."""

import logging
from logging.config import dictConfig
from typing import (Dict, Optional)

from addict import Dict as Addict
import yaml

from foca.models.config import (Config, LogConfig)

logger = logging.getLogger(__name__)


class ConfigParser():
    """Parser for FOCA config files.

    :var config_file: Path to config file in YAML format, defaults to ``None``
    :type config_file: Optional[str], optional
    :var format_logs: Whether log formatting should be configured, defaults to
        ``True``
    :type format_logs: bool, optional
    """

    def __init__(
        self,
        config_file: Optional[str] = None,
        format_logs: bool = True
    ) -> None:
        """Constructor method.
        """
        if config_file:
            self.config = Config(**self.parse_yaml(config_file))
        else:
            self.config = Config()
        if format_logs:
            self.configure_logging()
        logger.debug(f"Parsed config: {self.config.dict(by_alias=True)}")

    def configure_logging(self) -> None:
        """Configure logging.
        """
        try:
            dictConfig(self.config.log.dict(by_alias=True))
        except Exception as e:
            dictConfig(LogConfig().dict(by_alias=True))
            logger.warning(
                f"Failed to configure logging. Falling back to default "
                f"settings. Original error: {type(e).__name__}: {e}"
            )

    @staticmethod
    def parse_yaml(conf: str) -> Dict:
        """Load YAML file.

        :param conf: Path to YAML file
        :type conf: str
        :raises yaml.YAMLError: file cannot be accessed
        :raises OSError: file cannot not be parsed
        :return: Dictionary of YAML file contents
        :rtype: Dict
        """
        try:
            with open(conf) as config_file:
                try:
                    return yaml.safe_load(config_file)
                except yaml.YAMLError:
                    raise yaml.YAMLError(
                        f"file '{conf}' is not valid YAML"
                    )
        except OSError as e:
            raise OSError(
                f"file '{conf}' could not be read"
            ) from e

    @staticmethod
    def merge_yaml(*args: str) -> Optional[Dict]:
        """Parse and merge a set of YAML files. Merging is done
        iteratively, from the first, second to the nth argument.
        Dict items are updated, not overwritten. For exact
        behavior cf. https://github.com/mewwts/addict.

        :return: Dictionary of merged YAML file contents, or ``None``
            if no arguments have been supplied; if only a single YAML
            file path is provided, no merging is done.
        :rtype: Optional[Dict]
        """
        args_list = list(args)
        if not args_list:
            return None
        yaml_dict = Addict(ConfigParser.parse_yaml(args_list.pop(0)))

        for arg in args_list:
            yaml_dict.update(Addict(ConfigParser.parse_yaml(arg)))

        return yaml_dict.to_dict()
