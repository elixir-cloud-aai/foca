"""Petstore controllers."""

import logging

from exceptions import NotFound
from flask import make_response
from pymongo.collection import Collection

from foca import get_collection

logger = logging.getLogger(__name__)


def findPets(limit=None, tags=None):
    db_collection: Collection = get_collection(db="petstore", collection="pets")
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
    db_collection: Collection = get_collection(db="petstore", collection="pets")
    counter = 0
    ctr = db_collection.find({}).sort([("$natural", -1)])
    if db_collection.count_documents({}) != 0:
        counter = ctr[0].get("id") + 1
    record = {"id": counter, "name": pet["name"], "tag": pet["tag"]}
    db_collection.insert_one(record)
    del record["_id"]
    return record


def findPetById(id):
    db_collection: Collection = get_collection(db="petstore", collection="pets")
    record = db_collection.find_one(
        {"id": id},
        {"_id": False},
    )
    if record is None:
        raise NotFound
    return record


def deletePet(id):
    db_collection: Collection = get_collection(db="petstore", collection="pets")
    record = db_collection.find_one(
        {"id": id},
        {"_id": False},
    )
    if record is None:
        raise NotFound
    db_collection.delete_one(
        {"id": id},
    )
    response = make_response("", 204)
    return response
