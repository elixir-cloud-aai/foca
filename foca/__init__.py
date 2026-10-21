"""FOCA root package."""

from importlib.metadata import PackageNotFoundError, version

from foca.config.access import (  # noqa: F401 (re-export)
    get_collection,
    get_config,
)
from foca.foca import Foca  # noqa: F401 (re-export)

# Version from git tags, set when the package is built (via setuptools-scm)
try:
    __version__ = version("foca")
except PackageNotFoundError:  # not installed, e.g., run from a source tree
    __version__ = "0+unknown"
