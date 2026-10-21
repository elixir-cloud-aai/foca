"""Petstore access control controllers.

Access control policies are enforced by FOCA for all operations
(`security.access_control.enforce`). The controllers only manage ownership:
creators of pets become their owners, and ownership is removed together with
the pet.
"""

import logging
from typing import Any

from exceptions import NotFound
from flask import Response, make_response, request
from pymongo import ReturnDocument
from pymongo.collection import Collection

from foca import get_collection
from foca.security.access_control import (
    add_resource_role,
    get_current_subject,
    remove_resource,
)

logger = logging.getLogger(__name__)


def _get_collection(name: str = "pets") -> Collection[dict[str, Any]]:
    return get_collection(db="petstore-access-control", collection=name)


def _next_pet_id() -> int:
    # IDs are taken from an atomically incremented counter, so that they
    # are never shared by concurrent requests or reused after deletion
    # (roles are granted per pet ID)
    counter = _get_collection("counters").find_one_and_update(
        {"_id": "pets"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return counter["value"] - 1


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


def addPet(pet: dict[str, Any]) -> dict[str, Any]:
    """Add a pet, owned by the current user.

    Args:
        pet: Pet to add, with its name and optional tag.

    Returns:
        Added pet, with its identifier.
    """
    db_collection = _get_collection()
    pet_id = _next_pet_id()
    record = {"id": pet_id, "name": pet["name"]}
    if "tag" in pet:
        record["tag"] = pet["tag"]
    db_collection.insert_one(record)
    del record["_id"]
    # Make the creator the owner of the new pet
    add_resource_role(
        subject=get_current_subject(),
        role="owner",
        resource=_pet_path(pet_id),
    )
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
    db_collection = _get_collection()
    record = db_collection.find_one(
        {"id": id},
        {"_id": False},
    )
    if record is None:
        raise NotFound
    return record


def deletePet(id: int) -> Response:
    """Delete a pet, with all roles granted on it.

    Args:
        id: Identifier of the pet.

    Returns:
        Empty response with status 204.

    Raises:
        werkzeug.exceptions.NotFound: The pet does not exist.
    """
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
