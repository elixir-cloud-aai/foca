"""Tests for the database utilties module."""

import mongomock

from foca.utils.db import find_id_latest, find_one_latest


def test_find_one_latest():
    """Test that find_one_latest return recently added object without _id
    field.
    """
    collection = mongomock.MongoClient().db.collection
    obj1 = {'_id': 1, 'name': 'first'}
    obj2 = {'_id': 2, 'name': 'seond'}
    obj3 = {'_id': 3, 'name': 'third'}

    collection.insert_many([obj1, obj2, obj3])
    res = find_one_latest(collection)
    assert res == {'name': 'third'}


def test_find_one_latest_returns_None():
    """Test that find_one_latest return empty if collection is empty."""
    collection = mongomock.MongoClient().db.collection
    assert find_one_latest(collection) is None


def test_find_id_latest():
    """Test that find_id_latest return recently added id."""
    collection = mongomock.MongoClient().db.collection
    obj1 = {'_id': 1, 'name': 'first'}
    obj2 = {'_id': 2, 'name': 'seond'}
    obj3 = {'_id': 3, 'name': 'third'}

    collection.insert_many([obj1, obj2, obj3])
    res = find_id_latest(collection)
    assert res == 3


def test_find_id_latest_returns_None():
    """Test that find_one_latest return empty if collection is empty."""
    collection = mongomock.MongoClient().db.collection
    assert find_id_latest(collection) is None
