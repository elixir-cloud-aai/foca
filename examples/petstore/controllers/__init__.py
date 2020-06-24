PETS = {}


def listPets():
    return {"pets": [pet for pet in PETS.values()]}


def createPets(pet):
    PETS[pet] = 1
    return {"pets": [pet for pet in PETS.values()]}


def showPetById(id):
    if id in PETS:
        return PETS[id]
    else:
        return {"No pets found"}
