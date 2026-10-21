"""Petstore controllers."""

import logging
from typing import Any

from exceptions import NotFound
from flask import Response, make_response
from pymongo.collection import Collection

from foca import get_collection

logger = logging.getLogger(__name__)


def findPets(
    limit: int | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get pets, newest first.

    Args:
        limit: Maximum number of pets to return; all pets if not set or 0.
        tags: Tags to filter pets by; pets with any of the tags are returned.

    Returns:
        Pets.
    """
    db_collection: Collection[dict[str, Any]] = get_collection(
        db="petstore", collection="pets"
    )
    filter_dict = {} if tags is None else {"tag": {"$in": tags}}
    if not limit:
        limit = 0
    records = (
        db_collection.find(filter_dict, {"_id": False})
        .sort([("$natural", -1)])
        .limit(limit)
    )
    return list(records)


def addPet(pet: dict[str, Any]) -> dict[str, Any]:
    """Add a pet.

    Args:
        pet: Pet to add, with its name and tag.

    Returns:
        Added pet, with its identifier.
    """
    db_collection: Collection[dict[str, Any]] = get_collection(
        db="petstore", collection="pets"
    )
    counter = 0
    ctr = db_collection.find({}).sort([("$natural", -1)])
    if db_collection.count_documents({}) != 0:
        counter = ctr[0].get("id") + 1
    record = {"id": counter, "name": pet["name"], "tag": pet["tag"]}
    db_collection.insert_one(record)
    del record["_id"]
    return record


def findPetById(id: int) -> dict[str, Any]:
    """Get a pet.

    Args:
        id: Identifier of the pet.

    Returns:
        Pet.

    Raises:
        werkzeug.exceptions.NotFound: The pet does not exist.
    """
    db_collection: Collection[dict[str, Any]] = get_collection(
        db="petstore", collection="pets"
    )
    record = db_collection.find_one(
        {"id": id},
        {"_id": False},
    )
    if record is None:
        raise NotFound
    return record


def deletePet(id: int) -> Response:
    """Delete a pet.

    Args:
        id: Identifier of the pet.

    Returns:
        Empty response with status 204.

    Raises:
        werkzeug.exceptions.NotFound: The pet does not exist.
    """
    db_collection: Collection[dict[str, Any]] = get_collection(
        db="petstore", collection="pets"
    )
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
