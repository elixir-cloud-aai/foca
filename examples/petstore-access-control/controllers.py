"""Petstore access control controllers.

Access control policies are enforced by FOCA for all operations
(`security.access_control.enforce`). The controllers only manage ownership:
creators of pets become their owners, and ownership is removed together with
the pet.
"""

import logging

from exceptions import NotFound
from flask import current_app, make_response, request
from pymongo.collection import Collection

from foca.security.access_control import (
    add_resource_role,
    get_current_subject,
    remove_resource,
)

logger = logging.getLogger(__name__)


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


def addPet(pet):
    db_collection = _get_collection()
    counter = 0
    ctr = db_collection.find({}).sort([("$natural", -1)])
    if db_collection.count_documents({}) != 0:
        counter = ctr[0].get("id") + 1
    record = {"id": counter, "name": pet["name"], "tag": pet["tag"]}
    db_collection.insert_one(record)
    del record["_id"]
    # Make the creator the owner of the new pet
    add_resource_role(
        subject=get_current_subject(),
        role="owner",
        resource=f"{request.path.rstrip('/')}/{counter}",
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
    remove_resource(request.path)
    response = make_response("", 204)
    return response
