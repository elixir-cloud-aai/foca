"""Integration tests for petstore app."""

import requests

from tests.test_files.models_petstore import (
    Error,
    Pet,
    Pets,
)

PETSTORE_URL = "http://localhost:80"
NAME_PET = "karl"
TAG_PET = "frog"
EXTRA_PARAM_ARG = "extra"
BODY_PET_1 = {
    "name": NAME_PET,
    "tag": TAG_PET,
}
BODY_PET_2 = {
    "name": NAME_PET,
    "tag": TAG_PET,
    "extra_parameter": EXTRA_PARAM_ARG,
}
INVALID_ID = "X"


def test_add_pet_200():
    """Test `POST /pets` for successfully adding a new pet."""
    response = requests.post(
        url=f"{PETSTORE_URL}/pets",
        json=BODY_PET_1,
    )
    assert response.status_code == 200
    response_data = Pet(**response.json())
    assert isinstance(response_data, Pet)
    assert response_data.name == NAME_PET
    assert response_data.tag == TAG_PET


def test_add_pet_extra_parameter_200():
    """Test `POST /pets` to ensure that extra parameter is ignored."""
    response = requests.post(
        url=f"{PETSTORE_URL}/pets",
        json=BODY_PET_2,
    )
    assert response.status_code == 200
    response_data = Pet(**response.json())
    assert isinstance(response_data, Pet)
    assert response_data.name == NAME_PET
    assert response_data.tag == TAG_PET
    assert getattr(response_data, 'extra_parameter', None) is None


def test_add_pet_required_arguments_missing_400():
    """Test `POST /pets` with required arguments missing."""
    response = requests.post(
        url=f"{PETSTORE_URL}/pets",
        json={},
    )
    assert response.status_code == 400
    print(response.json())
    response_data = Error(**response.json())
    assert response_data.code == 400
    assert response_data.message == (
        "We don't quite understand what it is you are looking for."
    )


def test_get_all_pets_200():
    """Test `GET /pets` for successfully fetching all pets."""
    response = requests.get(
        url=f"{PETSTORE_URL}/pets",
    )
    assert response.status_code == 200
    response_data = Pets(pets=response.json())
    assert isinstance(response_data, Pets)


def test_get_all_pets_check_record_number():
    """Test `GET /pets` to ensure that the number of records increased after
    adding an additional pet.
    """
    response = requests.get(
        url=f"{PETSTORE_URL}/pets",
    )
    assert response.status_code == 200
    records = len(response.json())
    requests.post(
        url=f"{PETSTORE_URL}/pets",
        json=BODY_PET_1,
    )
    new_response = requests.get(
        url=f"{PETSTORE_URL}/pets",
    )
    assert new_response.status_code == 200
    assert len(new_response.json()) == records + 1


def test_get_pet_by_id_200():
    """Test for `GET /pets/{id}` for successfully fetching a pet with a given
    id.
    """
    post_response = requests.post(
        url=f"{PETSTORE_URL}/pets",
        json=BODY_PET_1,
    )
    pet_id = Pet(**post_response.json()).id
    response = requests.get(
        url=f"{PETSTORE_URL}/pets/{pet_id}",
    )
    assert response.status_code == 200
    response_data = Pet(**response.json())
    assert isinstance(response_data, Pet)
    assert response_data.name == NAME_PET
    assert response_data.tag == TAG_PET


def test_get_pet_by_id_404():
    """Test for `GET /pets/{id}` for fetching a non-existent pet."""
    response = requests.get(
        url=f"{PETSTORE_URL}/pets/{INVALID_ID}",
    )
    assert response.status_code == 404
    response_data = Error(**response.json())
    assert response_data.code == 404
    assert response_data.message == "We have never heard of this pet! :-("


def test_delete_pet_204():
    """Test for `DELETE /pets/{id}` for successfully deleting a pet with a
    given id.
    """
    post_response = requests.post(
        url=f"{PETSTORE_URL}/pets",
        json=BODY_PET_1,
    )
    pet_id = Pet(**post_response.json()).id
    get_response_pre = requests.get(
        url=f"{PETSTORE_URL}/pets/{pet_id}",
    )
    assert get_response_pre.status_code == 200
    response = requests.delete(
        url=f"{PETSTORE_URL}/pets/{pet_id}",
    )
    assert response.status_code == 204
    get_response_post = requests.get(
        url=f"{PETSTORE_URL}/pets/{pet_id}",
    )
    assert get_response_post.status_code == 404


def test_delete_pet_404():
    """Test for `DELETE /pets/{id}` for deleting a non-existent pet."""
    response = requests.delete(
        url=f"{PETSTORE_URL}/pets/{INVALID_ID}",
    )
    assert response.status_code == 404
    response_data = Error(**response.json())
    assert response_data.code == 404
    assert response_data.message == "We have never heard of this pet! :-("
