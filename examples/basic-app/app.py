#!/usr/bin/env python3
import logging
from pathlib import Path
import sys

from foca.foca import foca

logger = logging.getLogger(__name__)

PETS = {}

def listPets():
    return {"pets": [pet for pet in PETS.values()]}


def createPets(pet):
    PETS[pet] = 1
    return {"pets": [pet for pet in PETS.values()]}


def showPetById(id):
    if id in PETS:
        return PETS[id]
    else:
        return {"No pets found"}


if __name__ == '__main__':
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    config = str(Path(__file__).resolve().parent / "config.yaml")
    app = foca(config)
    app.run(port=8080)
    logger.warning("app running")
