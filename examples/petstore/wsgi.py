"""WSGI entry point of the petstore example app, e.g., for Gunicorn."""

from pathlib import Path

from foca import Foca


def create_app():
    """Create the app; called once per worker process."""
    config_file = Path(__file__).parent / "config.yaml"
    return Foca(config_file=config_file).create_app()
