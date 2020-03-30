"""Utility functions for MongoDB document insertion, updates and retrieval."""

from typing import (Any, Mapping, Optional)

from bson.objectid import ObjectId
from pymongo import collection as Collection


def find_one_latest(collection: Collection) -> Optional[Mapping[Any, Any]]:
    """Returns newest/latest object, stripped of the object id, or None if no
    object exists: collection.
    """
    try:
        return collection.find(
            {},
            {'_id': False}
        ).sort([('_id', -1)]).limit(1).next()
    except StopIteration:
        return None


def find_id_latest(collection: Collection) -> Optional[ObjectId]:
    """Returns object id of newest/latest object, or None if no object exists.
    """
    try:
        return collection.find().sort([('_id', -1)]).limit(1).next()['_id']
    except StopIteration:
        return None
