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


def get_conf_type(
    config: Mapping[Any, Any],
    *args: str,
    types: Any = False,
    invert_types: bool = False,
    touchy: bool = True
):
    """Returns the value corresponding to a chain of keys from a nested
    dictionary.

    Args:
        config: Dictionary containing config values.
        *args: Keys of nested dictionary, from outer to innermost.
        types: Tuple of allowed types for return values; if `False`, no
               checking is done.
        invert_types: Types specified in parameter `types` are *forbidden*;
                      ignored if `types` is `False`.
        touchy: If `True`, exceptions will raise `SystemExit(1)`; otherwise
                exceptions are re-raised.

    Raises:
        AttributeError: May occur when an illegal value is provided for
                        `*args`; raised only if `touchy` is `False`.
        KeyError: Raised when dictionary keys passed in `*args` are not
                  available and `touchy` is `False`.
        TypeError: The return value is not of any of the allowed `types` or
                   is among any of the forbidden `types` (if `invert_types` is
                   `True`); only raised if `touchy` is `False`.
        SystemExit: Raised if any exception occurs and `touchy` is `True`.
    """
    # Get value for list of keys
    keys = list(args)
    try:
        val = config[keys.pop(0)]
        while keys:
            val = val[keys.pop(0)]

        # Check type
        if types:
            if not invert_types:
                if not isinstance(val, types):
                    raise TypeError(
                        (
                            "Value '{val}' expected to be of type '{types}', "
                            "but is of type '{type}'."
                        ).format(
                            val=val,
                            types=types,
                            type=type(val),
                        )
                    )
            else:
                if isinstance(val, types):
                    raise TypeError(
                        (
                            "Type '{type}' of value '{val}' is forbidden."
                        ).format(
                            type=type(val),
                            val=val,
                        )
                    )

    except (AttributeError, KeyError, TypeError, ValueError) as e:

        if touchy:
            logger.exception(
                (
                    'Config file corrupt. Execution aborted. Original error '
                    'message: {type}: {msg}'
                ).format(
                    type=type(e).__name__,
                    msg=e,
                )
            )
            raise SystemExit(1)

        else:
            raise

    else:
        return val


def get_conf(
    config: Mapping[str, Any],
    *args: str,
    touchy: bool = True
):
    """Returns the value corresponding to a chain of keys from a nested
    dictionary. Extracts only 'leafs' of nested dictionary.

    Shortcut for ```
    get_conf_type(
        config,
        *args,
        types=(dict, list),
        invert_types=True
    ```

    See signature for `get_conf_type()` for info on arguments and errors.
    """
    return get_conf_type(
        config,
        *args,
        types=(dict, list),
        invert_types=True,
        touchy=touchy,
    )
