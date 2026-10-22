"""Celery worker entry point of the petstore example app.

Run with `python -m celery --app worker:celery_app worker` (e.g., by the Helm
chart's worker); the example has no tasks.
"""

from pathlib import Path

from foca import Foca

celery_app = Foca(config_file=Path(__file__).parent / "config.yaml").create_celery_app()
