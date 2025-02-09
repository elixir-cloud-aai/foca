"""
Configuration file for the Sphinx documentation builder.

This file contains a selection of the most common options. For a full
list see the documentation:
https://www.sphinx-doc.org/en/master/usage/configuration.html

If extensions (or modules to document with autodoc) are in another directory,
add these directories to sys.path here. If the directory is relative to the
documentation root, use os.path.abspath to make it absolute, like shown here.
"""

from pathlib import Path
import sys

from sphinx.ext import apidoc

# Set up the path to include the root directory
root_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root_dir))

# Execute the version file to get the version information
exec(open(root_dir / "foca" / "version.py").read())

# -- Project information -----------------------------------------------------

project = 'FOCA'
copyright = '2022, ELIXIR Cloud & AAI'
author = 'ELIXIR Cloud & AAI'

# The full version, including alpha/beta/rc tags
release = __version__  # noqa: F821


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
]

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build']


# Default doc to search for
master_doc = 'index'

# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = []


# -- Automation -------------------------------------------------------------

def run_apidoc(_):
    """
    Auto-generate API documentation.

    This function runs the Sphinx apidoc tool to generate API documentation
    for the FOCA project.
    """
    ignore_paths = [
    ]
    argv = [
        "--force",
        "--module-first",
        "-o", "./modules",
        "../../foca"
    ] + ignore_paths
    apidoc.main(argv)


def setup(app):
    """
    Set up the Sphinx application.

    This function connects the 'builder-inited' event to the run_apidoc
    function to automatically generate API documentation when the builder
    is initialized.

    Args:
        app: The Sphinx application object.
    """
    app.connect('builder-inited', run_apidoc)
