#!/usr/bin/env python3
import logging
from pathlib import Path
import sys

from foca.foca import foca

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    config = str(Path(__file__).resolve().parent / "config.yaml")
    app = foca("config.yaml")
    app.run(port=8080)
    logger.info("Petstore is running...")
