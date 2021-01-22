import requests


class PetStore:
    """
    Petstore Object containing useful functions and required url
    """
    def __init__(self):
        self.url = "http://localhost/pets"

    def generate_url(self, id):
        new_url = self.url + "/" + str(id)
        return new_url


petstore = PetStore()


def test_post_request():
    """
    Test for POST request for adding new pet
    """
    temp_data = {"name": "karl", "tag": "frog"}
    response = requests.post(petstore.url, json=temp_data)
    assert response.status_code == 200


def test_get_all_pets_request():
    """
    Test for GET request for fetching all pets
    """
    response = requests.get(petstore.url)
    assert response.status_code == 200


def test_get_pet_by_id_request():
    """
    Test for GET request for fetching pet by id
    """
    new_url = petstore.generate_url(0)
    response = requests.get(new_url)
    assert response.status_code == 200


def test_delete_request():
    """
    Test for DELETE request for deleting pet by id
    """
    new_url = petstore.generate_url(0)
    response = requests.delete(new_url)
    assert response.status_code == 204
