from pymongo import MongoClient
import logging
import json


logger = logging.getLogger(__name__)


db = MongoClient(host='mongodb', port=27017)
db_collection = db.petstore.pets


def findPets(limit=None, tags=None):
    try:
        filter_dict = {} if tags is None else {'tag': {'$in': tags}}
        if limit is None:
            records = db_collection.find(filter_dict).sort([('$natural', -1)])
        else:
            records = db_collection.find(filter_dict).sort(
                [('$natural', -1)]
                ).limit(limit)

        response_arr = []
        for doc in records:
            record_result = {
                    "name": doc['name'],
                    "tag": doc['tag'],
                    "id": doc['pet_id']
                }
            response_arr.append(record_result)

        return json.dumps(response_arr)
    except Exception as e:
        logger.error(
            "Original Error : {error}".format(error=e)
        )
        return json.dumps({
            'code': 0,
            'message': 'Cannot view pets. Something went wrong.'
            })


def addPet(pet):
    try:
        ctr = db_collection.find({}).sort([('$natural', -1)])
        counter = 0
        if ctr.count() == 0:
            counter = 0
        else:
            counter = ctr[0].get('pet_id') + 1
        db_collection.insert({
            "pet_id": counter,
            "name": pet['name'],
            "tag": pet['tag']
            })
        return findPets(limit=1)
    except Exception as e:
        logger.error(
            "Original Error : {error}".format(error=e)
        )
        return json.dumps({
            'code': 0,
            'message': 'Cannot add pet, something went wrong.'
            })


def findPetById(id):
    try:
        pet_record = db_collection.find({"pet_id": id})[0]
        if pet_record is None:
            return json.dumps({'code': 0, 'message': 'Pet does not exists.'})
        return json.dumps({
            "name": pet_record['name'],
            "tag": pet_record['tag'],
            "id": pet_record['pet_id']
            })
    except Exception as e:
        logger.error(
            "Original Error : {error}".format(error=e)
        )
        return json.dumps({
            'code': 0,
            'message': 'Pet does not exists.'
            })


def deletePet(id):
    try:
        db_collection.remove({"pet_id": id})
        return findPets()
    except Exception as e:
        logger.error(
            "Original Error : {error}".format(error=e)
        )
        return json.dumps({
            'code': 0,
            'message': 'Cannot delete flat. Something went wrong.'
            })
