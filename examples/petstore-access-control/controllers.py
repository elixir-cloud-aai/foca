"""Petstore access control controllers.

Access control policies are enforced by FOCA for all operations
(`security.access_control.enforce`). The controllers only manage ownership:
creators of pets become their owners, and ownership is removed together with
the pet.
"""

import logging

from exceptions import InternalServerError, NotFound
from flask import current_app, make_response, request
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from foca.security.access_control import (
    add_resource_role,
    get_current_subject,
    remove_resource,
)

logger = logging.getLogger(__name__)

MAX_INSERT_ATTEMPTS = 10


def _get_collection() -> Collection:
    return (
        current_app.config.foca.db.dbs["petstore-access-control"]
        .collections["pets"]
        .client
    )


def findPets(limit=None, tags=None):
    db_collection = _get_collection()
    filter_dict = {} if tags is None else {"tag": {"$in": tags}}
    if not limit:
        limit = 0
    records = (
        db_collection.find(filter_dict, {"_id": False})
        .sort([("$natural", -1)])
        .limit(limit)
    )
    return list(records)


def _pet_path(id: int) -> str:
    # Canonical resource path of a pet, independent of how the request path
    # spells the ID (e.g., `/pets/00` for pet 0)
    base_path = request.path.rstrip("/")
    if request.view_args and "id" in request.view_args:
        base_path = base_path.rsplit("/", 1)[0]
    return f"{base_path}/{id}"


def addPet(pet):
    db_collection = _get_collection()
    # IDs are unique (cf. index in `config.yaml`); if a concurrent request
    # took the next ID, try again with the one after
    for _ in range(MAX_INSERT_ATTEMPTS):
        last = db_collection.find_one(
            {}, {"_id": False, "id": True}, sort=[("id", -1)]
        )
        counter = 0 if last is None else last["id"] + 1
        record = {"id": counter, "name": pet["name"], "tag": pet["tag"]}
        try:
            db_collection.insert_one(record)
            break
        except DuplicateKeyError:
            continue
    else:
        raise InternalServerError
    del record["_id"]
    # Make the creator the owner of the new pet
    add_resource_role(
        subject=get_current_subject(),
        role="owner",
        resource=_pet_path(counter),
    )
    return record


def findPetById(id):
    db_collection = _get_collection()
    record = db_collection.find_one(
        {"id": id},
        {"_id": False},
    )
    if record is None:
        raise NotFound
    return record


def deletePet(id):
    db_collection = _get_collection()
    record = db_collection.find_one(
        {"id": id},
        {"_id": False},
    )
    if record is None:
        raise NotFound
    db_collection.delete_one(
        {"id": id},
    )
    # Remove all roles granted on the deleted pet
    remove_resource(_pet_path(id))
    response = make_response("", 204)
    return response
