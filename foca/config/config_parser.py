"""YAML config parser and access/validation functions."""

from itertools import chain
import logging
import os
from typing import (Any, List, Mapping)

import yaml

from addict import Dict


# Get logger instance
logger = logging.getLogger(__name__)


class YAMLConfigParser(Dict):
    """Config parser for YAML files.

    Allows sequential updating of configs via file paths and environment
    variables. Uses the `addict` package for updating config dictionaries.
    """

    def update_from_yaml(
        self,
        config_paths: List[Any] = [],
        config_vars: List[Any] = [],
    ) -> str:
        """Updates config dictionary from file paths or environment variables
        pointing to one or more YAML files.

        Multiple file paths and environment variables are accepted. Moreover,
        a given environment variable may point to several files, with paths
        separated by colons. All available file paths/pointers are used to
        update the dictionary in a sequential order, with nested dictionary
        entries being successively and recursively overridden. In other words:
        if a given nested dictionary key occurs in multiple YAML files, its
        last value will be retained. File paths in the `config_paths` list are
        used first (lowest precedence), from the first to the last item/path,
        followed by file paths pointed to by the environment variables in
        `config_vars` (highest precedence), form the first to the last
        item/variable. If a given variable points to multiple file paths, these
        will be used for updating from the first to the last path.

        Args:
            config_paths: List of YAML file paths.
            config_vars: List of environment variables, each pointing to one or
                         more YAML files, separated by colons; unset variables
                         are ignored.

        Returns:
            A string of all file paths that were used to update the dictionary,
            separated by colons.

        Raises:
            FileNotFoundError: Any of the files were not found.
            PermissionError: Any of the files were not accessible.
        """
        # Get ordered list of file paths
        paths = config_paths + [os.environ.get(var) for var in config_vars]
        paths = list(filter(None, paths))
        paths = [item.split(':') for item in paths]
        paths = list(chain.from_iterable(paths))

        # Update dictionary
        for path in paths:
            try:
                with open(path) as f:
                    self.update(yaml.safe_load(f))
            except (FileNotFoundError, PermissionError):
                raise

        return ':'.join(paths)

