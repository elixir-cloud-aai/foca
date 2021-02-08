import requests


class PetStore:
    """Petstore class.

    Attributes:
        url: Petstore API base URL.
    """
    def __init__(self):
        """Class constructor method."""
        self.url = "http://localhost:80/pets"

    def add_path_to_url(self, id):
        """Return petstore base URL with appended path"""
        return "/".join([self.url, str(id)])


petstore = PetStore()


def test_post_request():
    """Test `POST /pets` for successfully adding a new pet."""
    temp_data = {"name": "karl", "tag": "frog"}
    response = requests.post(petstore.url, json=temp_data)
    assert response.status_code == 200


def test_get_all_pets_request():
    """Test `GET /pets` for successfully fetching all pets."""
    response = requests.get(petstore.url)
    assert response.status_code == 200


def test_get_pet_by_id_request():
    """Test for `GET /pets/{id}` for successfully fetching a pet with a given
    id.
    """
    new_url = petstore.add_path_to_url("0")
    response = requests.get(new_url)
    assert response.status_code == 200


def test_delete_request():
    """Test for `DELETE /pets/{id}` for successfully deleting a pet with a
    given id.
    """
    new_url = petstore.add_path_to_url("0")
    response = requests.delete(new_url)
    assert response.status_code == 204
