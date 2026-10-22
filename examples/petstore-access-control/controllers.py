"""Petstore access control controllers.

Access control policies are enforced by FOCA for all operations
(`security.access_control.enforce`). The controllers only manage ownership,
with FOCA's resource ownership helpers: creators of pets become their owners,
users only list the pets they have roles on (admins list all), owners manage
the roles on their pets via FOCA's ready-made controllers, and roles are
removed together with the pet.
"""

import logging
from collections.abc import Callable
from typing import Any

from exceptions import NotFound
from flask import Response, make_response, request
from pymongo import ReturnDocument
from pymongo.collection import Collection

from foca import get_collection
from foca.security.access_control import (
    add_owner,
    ownership_server,
    remove_resource,
    resource_filter,
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


def _pets_path() -> str:
    # Full request path of the pets, including the base path of the API, as
    # policies are enforced on full request paths; for `/pets` and
    # `/pets/{id}`
    path = request.path.rstrip("/")
    if request.view_args and "id" in request.view_args:
        path = path.rsplit("/", 1)[0]
    return path


def _pet_path(id: int) -> str:
    # Canonical resource path of a pet, independent of how the request path
    # spells the ID (e.g., `/pets/00` for pet 0)
    return f"{_pets_path()}/{id}"


def _pets_filter() -> dict[str, Any]:
    # Filter for the pets the caller (or the user a service client acts for)
    # has roles on, or for all pets for admins; resource identifiers are
    # strings, pet IDs integers
    query = resource_filter(_pets_path())
    if "id" in query:
        ids = [int(i) for i in query["id"]["$in"] if i.isascii() and i.isdigit()]
        query = {"id": {"$in": ids}}
    return query


def findPets(
    limit: int | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get the pets the caller has roles on, newest first.

    Args:
        limit: Maximum number of pets to return; all pets if not set or 0.
        tags: Tags to filter pets by; pets with any of the tags are returned.

    Returns:
        Pets.
    """
    db_collection = _get_collection()
    filter_dict = _pets_filter()
    if tags is not None:
        filter_dict["tag"] = {"$in": tags}
    if not limit:
        limit = 0
    records = (
        db_collection.find(filter_dict, {"_id": False})
        .sort([("$natural", -1)])
        .limit(limit)
    )
    return list(records)


def addPet(pet: dict[str, Any]) -> dict[str, Any]:
    """Add a pet, owned by its creator.

    The creator is the calling user or service client, or the user that an
    allowed service client acts for (`X-On-Behalf-Of` header).

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
    # Make the creator the owner of the new pet, before it can be found
    add_owner(_pet_path(pet_id))
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
    if db_collection.find_one({"id": id}) is None:
        raise NotFound
    # Remove all roles granted on the pet before deleting it, so that nobody
    # can act on it meanwhile, and after, to revoke roles granted meanwhile;
    # if deleting fails, the pet is left without owners (admins can still
    # delete it)
    remove_resource(_pet_path(id))
    db_collection.delete_one({"id": id})
    remove_resource(_pet_path(id))
    response = make_response("", 204)
    return response


def _pet_exists(id: int) -> Callable[[], bool]:
    # Existence check for FOCA's role controllers, so that roles granted
    # while a pet is deleted are revoked again
    return lambda: _get_collection().count_documents({"id": id}) > 0


# Roles on pets are managed by FOCA's ready-made controllers, which take the
# pet and the user from the route; operations are routed to this module
# (`x-openapi-router-controller`), so via an alias and wrappers passing
# existence checks
getPetRoles = ownership_server.getResourceRoles


def putPetUserRole(id: int, **kwargs: Any) -> dict[str, str]:
    """Grant a user a role on a pet, replacing their other roles.

    Args:
        id: Identifier of the pet.
        **kwargs: Further arguments of the controller (cf.
            `foca.security.access_control.ownership_server.putUserRole`).

    Returns:
        Subject type (`user`), user identifier and granted role.
    """
    role: dict[str, str] = ownership_server.putUserRole(
        exists=_pet_exists(id), **kwargs
    )
    return role


def deletePetUserRole(id: int, **kwargs: Any) -> str:
    """Revoke the roles of a user on a pet.

    Args:
        id: Identifier of the pet.
        **kwargs: Further arguments of the controller (cf.
            `foca.security.access_control.ownership_server.deleteUserRole`).

    Returns:
        User identifier.
    """
    user: str = ownership_server.deleteUserRole(exists=_pet_exists(id), **kwargs)
    return user
