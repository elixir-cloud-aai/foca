"""Utility functions for interacting with a MongoDB database collection."""

from typing import (Any, Mapping, Optional)

from bson.objectid import ObjectId
from pymongo import collection as Collection


def find_one_latest(collection: Collection) -> Optional[Mapping[Any, Any]]:
    """Return newest document, stripped of `ObjectId`.

    Args:
        collection: MongoDB collection from which the document is to be
            retrieved.

    Returns:
        Newest document or ``None``, if no document exists.
    """
    try:
        return collection.find(
            {},
            {'_id': False}
        ).sort([('_id', -1)]).limit(1).next()
    except StopIteration:
        return None


def find_id_latest(collection: Collection) -> Optional[ObjectId]:
    """Return `ObjectId` of newest document.

    Args:
        collection: MongoDB collection from which `ObjectId` is to be
            retrieved.

    Returns:
        `ObjectId` of newest document or ``None``, if no document exists.
    """
    try:
        return collection.find().sort([('_id', -1)]).limit(1).next()['_id']
    except StopIteration:
        return None
