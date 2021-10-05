import logging

from flask import (current_app, make_response)
from werkzeug.exceptions import NotFound

from flask import Flask
from flask_authz import CasbinEnforcer
import casbin_pymongo_adapter


app = Flask(__name__)
# Set up Casbin model config
app.config['CASBIN_MODEL'] = 'casbinmodel.conf'
# Set headers where owner for enforcement policy should be located
app.config['CASBIN_OWNER_HEADERS'] = {'X-User', 'X-Group'}
# Add User Audit Logging with user name associated to log
# i.e. `[2020-11-10 12:55:06,060] ERROR in casbin_enforcer: Unauthorized attempt: method: GET resource: /api/v1/item by user: janedoe@example.com`
app.config['CASBIN_USER_NAME_HEADERS'] = {'X-User'}
# Set up Casbin Adapter
adapter = casbin_pymongo_adapter.Adapter('mongodb://localhost:27017/', "policies")
casbin_enforcer = CasbinEnforcer(app, adapter)

logger = logging.getLogger(__name__)

error_response = {
    'code': 500,
    'message': 'Something went wrong.',
}


@casbin_enforcer.enforcer
def findPets(limit=None, tags=None):
    try:
        print("hiii")
        db_collection = (
            current_app.config['FOCA'].db.dbs['petstore']
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
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        return error_response


def addPet(pet):
    try:
        db_collection = (
            current_app.config['FOCA'].db.dbs['petstore']
            .collections['pets'].client
        )
        counter = 0
        ctr = db_collection.find({}).sort([('$natural', -1)])
        if not ctr.count() == 0:
            counter = ctr[0].get('id') + 1
        record = {
            "id": counter,
            "name": pet['name'],
            "tag": pet['tag']
        }
        db_collection.insert_one(record)
        del record['_id']
        return record
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        return error_response


def findPetById(id):
    try:
        db_collection = (
            current_app.config['FOCA'].db.dbs['petstore']
            .collections['pets'].client
        )
        record = db_collection.find_one(
            {"id": id},
            {'_id': False},
        )
        if not record:
            raise NotFound
        return record
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        return error_response


def deletePet(id):
    try:
        db_collection = (
            current_app.config['FOCA'].db.dbs['petstore']
            .collections['pets'].client
        )
        record = db_collection.find_one(
            {"id": id},
            {'_id': False},
        )
        if not record:
            raise NotFound
        db_collection.delete_one({"id": id})
        response = make_response('', 204)
        response.mimetype = current_app.config['JSONIFY_MIMETYPE']
        return response
    except Exception as e:
        logger.error(f"{type(e).__name__}: {e}")
        return error_response
