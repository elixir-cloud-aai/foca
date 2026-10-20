"""Service info shared by tests."""

from copy import deepcopy

# Module with functions returning service info fields at runtime
FIELDS = "tests.test_files.service_info_fields"
SERVICE = {
    "id": "org.example.my-service",
    "name": "My service",
    "type": {
        "group": "org.example",
        "artifact": "my-api",
        "version": "1.0.0",
    },
    "description": "Serves examples.",
    "organization": {"name": "Example", "url": "https://example.org"},
    "contactUrl": "mailto:support@example.org",
    "documentationUrl": "https://docs.example.org",
    "environment": "dev",
    "version": "1.2.3",
}
REQUIRED = ("id", "name", "type", "organization", "version")
# Value removing a field in `service()`
REMOVE = object()


def service(**fields) -> dict:
    """Copy the service info, with fields replaced, or removed if `REMOVE`."""
    copy = deepcopy(SERVICE)
    for field, value in fields.items():
        if value is REMOVE:
            copy.pop(field, None)
        else:
            copy[field] = value
    return copy
