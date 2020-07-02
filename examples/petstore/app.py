#!/usr/bin/env python3
from pathlib import Path

from foca.foca import foca

if __name__ == '__main__':
    config = str(Path(__file__).resolve().parent / "config.yaml")
    app = foca("config.yaml")
    app.run(port=8080)
