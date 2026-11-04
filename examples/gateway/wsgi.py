"""WSGI entry point of the gateway example app, e.g., for Gunicorn."""

from pathlib import Path

from connexion import App
from service import GATEWAY

from foca import Foca


def create_app() -> App:
    """Create the app; called once per worker process.

    Returns:
        The app.
    """
    config_file = Path(__file__).parent / "config.yaml"
    return Foca(config_file=config_file, gateway=GATEWAY).create_app()
