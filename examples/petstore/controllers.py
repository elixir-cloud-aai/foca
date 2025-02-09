"""Petstore controllers."""

import logging

from flask import (current_app, make_response)
from pymongo.collection import Collection

from exceptions import NotFound

logger = logging.getLogger(__name__)


def findPets(limit=None, tags=None):
    """
    Find pets in the database.

    This function retrieves pets from the database based on the provided
    limit and tags. If no limit is provided, all matching pets are returned.
    If no tags are provided, all pets are returned.

    Args:
        limit (int, optional): The maximum number of pets to return. Defaults
            to None.
        tags (list, optional): A list of tags to filter pets by. Defaults to
            None.

    Returns:
        list: A list of pets matching the criteria.
    """
    db_collection: Collection = (
        current_app.config.foca.db.dbs['petstore']
        .collections['pets'].client
    )
    filter_dict = {} if tags is None else {'tag': {'$in': tags}}
    if not limit:
        limit = 0
    records = db_collection.find(
        filter_dict,
        {'_id': False}
    ).sort([('$natural', -1)]).limit(limit)
    return list(records)


def addPet(pet):
    """
    Add a new pet to the database.

    This function adds a new pet to the database.

    Args:
        pet (dict): A dictionary representing the pet to be added.

    Returns:
        dict: The added pet.
    """
    db_collection: Collection = (
        current_app.config.foca.db.dbs['petstore']
        .collections['pets'].client
    )
    result = db_collection.insert_one(pet)
    return db_collection.find_one({'_id': result.inserted_id}, {'_id': False})


def findPetById(id):
    """
    Find a pet by its ID.

    This function retrieves a pet from the database based on the provided ID.

    Args:
        id (str): The ID of the pet to retrieve.

    Returns:
        dict: The pet with the specified ID.

    Raises:
        NotFound: If no pet with the specified ID is found.
    """
    db_collection: Collection = (
        current_app.config.foca.db.dbs['petstore']
        .collections['pets'].client
    )
    record = db_collection.find_one(
        {"id": id},
        {'_id': False},
    )
    if record is None:
        raise NotFound
    return record


def deletePet(id):
    """
    Delete a pet by its ID.

    This function deletes a pet from the database based on the provided ID.

    Args:
        id (str): The ID of the pet to delete.

    Returns:
        Response: An empty response with a 204 status code.

    Raises:
        NotFound: If no pet with the specified ID is found.
    """
    db_collection: Collection = (
        current_app.config.foca.db.dbs['petstore']
        .collections['pets'].client
    )
    record = db_collection.find_one(
        {"id": id},
        {'_id': False},
    )
    if record is None:
        raise NotFound
    db_collection.delete_one(
        {"id": id},
    )
    response = make_response('', 204)
    return response
