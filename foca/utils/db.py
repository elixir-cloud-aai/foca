"""Utility functions for interacting with a MongoDB database collection."""

from collections.abc import Mapping
from typing import Any

from bson.objectid import ObjectId
from pymongo.collection import Collection


def find_one_latest(collection: Collection) -> Mapping[Any, Any] | None:
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


def find_id_latest(collection: Collection) -> ObjectId | None:
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
