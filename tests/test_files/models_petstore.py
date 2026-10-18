"""Validation models for the petstore app."""

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
    tag: str | None


class Pets(BaseModel):
    """Model instance for a list of pets.

    Args:
        pets: List of pets.

    Attributes:
        pets: List of pets.
    """

    pets: list[Pet] = []


class Error(BaseModel):
    """Model for petstore error response (problem details, RFC 9457).

    Args:
        title: Short summary of the problem type.
        status: Status code.
        detail: Explanation of the problem.

    Attributes:
        title: Short summary of the problem type.
        status: Status code.
        detail: Explanation of the problem.
    """

    title: str
    status: int
    detail: str | None = None
