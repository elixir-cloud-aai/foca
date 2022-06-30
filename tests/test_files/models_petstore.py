"""Validation models for the petstore app."""

from typing import (
    List,
    Optional,
)

from pydantic import BaseModel


class Pet(BaseModel):
    """Model instance for pet.

    Args:
        id: Unique identifier for pet.
        name: The pet's name.
        tag: Optional tag for the pet.

    Attributes:
        id: Unique identifier for pet.
        name: The pet's name.
        tag: Optional tag for the pet.
    """
    id: int
    name: str
    tag: Optional[str]


class Pets(BaseModel):
    """Model instance for a list of pets.

    Args:
        pets: List of pets.

    Attributes:
        pets: List of pets.
    """
    pets: List[Pet] = []


class Error(BaseModel):
    """Model for petstore error response.

    Args:
        code: Status code.
        message: Error message.

    Attributes:
        code: Status code.
        message: Error message.
    """
    code: int
    message: str
