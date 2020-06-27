from pymongo import MongoClient
import json


db = MongoClient(host='mongodb', port=27017)
db_collection = db.petstore.pets


def findPets(limit=None, tag=None):
    if limit is None:
        records = db_collection.find({}).sort([('$natural', -1)])
    else:
        records = db_collection.find({}).sort([('$natural', -1)]).limit(limit)

    response_arr = []
    for doc in records:
        record_result = {
                "name": doc['name'],
                "tag": doc['tag'],
                "id": int(str(doc['_id']), 16)
            }
        response_arr.append(record_result)

    return json.dumps(response_arr)


def addPet(pet):
    pet_dec = pet
    db_collection.insert({"name": pet_dec['name'], "tag": pet_dec['tag']})
    return findPets(limit=1)


def findpetbyid():
    return {3}


def deletePet():
    return {4}
