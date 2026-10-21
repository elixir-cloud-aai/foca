# Utilities

FOCA provides a few helpers that are commonly needed in microservices. See
the [API reference](reference.md) for all arguments.

## Generating identifiers

`generate_id()` generates random identifiers, e.g., for resources:

```python
from foca.utils.misc import generate_id

resource_id = generate_id(charset="ABCDEFGHJKLMNPQRSTUVWXYZ23456789", length=8)
```

Characters are drawn with Python's `secrets` module, so identifiers are not
predictable from previously generated ones. The defaults (6 letters or
digits) are fine for collision-resistant resource identifiers, but do not use
them for secrets such as tokens. Pass the characters themselves as
`charset`; expressions such as `string.ascii_uppercase + string.digits` are
not evaluated.

## Logging requests and responses

Decorate controllers with `log_traffic` to log requests and responses:

```python
import logging

from foca.utils.logging import log_traffic


@log_traffic
def get_pet(pet_id: str) -> dict: ...


@log_traffic(log_request=False, log_level=logging.DEBUG)
def list_pets() -> list[dict]: ...
```

Requests are logged with method, path, protocol and client address, and
responses with their type and status code. Response bodies may contain
secrets or personal data, so they are only logged with
`log_response_body=True`. All logged values are escaped, so that clients
cannot forge log records (e.g., with line breaks in paths).

To decide on each request whether to log, e.g., from your app's
configuration, use `log_traffic_if()` with a function returning whether to
log; it takes the same options as `log_traffic`:

```python
from foca.config.access import get_config
from foca.utils.logging import log_traffic_if


@log_traffic_if(lambda: get_config().custom.log_pets, log_request=False)
def get_pet(pet_id: str) -> dict: ...
```

FOCA's own controllers log their traffic unless disabled via
[`service_info.log_traffic`](configuration/service-info.md#serving-the-service-info)
and
[`security.access_control.log_traffic`](access-control.md#configuring-the-api).

To log other values that may be controlled by clients, escape them with
`escape_log_value()`.

## Accessing the newest database document

`find_one_latest()` and `find_id_latest()` return the most recently inserted
document of a MongoDB collection (without its `_id`) and its `_id`,
respectively, or `None` if the collection is empty:

```python
from foca import get_collection
from foca.utils.db import find_one_latest

latest = find_one_latest(get_collection(db="myDb", collection="myCollection"))
```

## Access control helpers

Helpers for checking permissions and managing resource-scoped roles in
controllers are described in [Access control](access-control.md).
