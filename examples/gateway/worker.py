"""Celery worker entry point of the gateway example app.

Run with `python -m celery --app worker:celery_app worker`. The worker passes
the same gateway settings as the app, so its tasks can use the plugin host.
"""

from pathlib import Path

from service import GATEWAY

from foca import Foca

celery_app = Foca(
    config_file=Path(__file__).parent / "config.yaml", gateway=GATEWAY
).create_celery_app()
