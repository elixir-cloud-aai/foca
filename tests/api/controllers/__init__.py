"""Mock controllers for the Petstore test specifications."""

from typing import Any

from foca.security.auth import validate_token  # as vt  # noqa: F401 (re-export)


def listPets() -> dict[str, Any]:
    """List all pets."""
    return {}


def createPets() -> dict[str, Any]:
    """Create a pet."""
    return {}


def showPetById() -> dict[str, Any]:
    """Show a pet by its ID."""
    return {}


def putPetsById() -> dict[str, Any]:
    """Update a pet by its ID."""
    return {}
