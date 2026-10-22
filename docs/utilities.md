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

## Checking and normalizing URLs

Apps that store URLs sent by clients (e.g., service URLs, documentation or
contact links, access URLs) should check them before storing them, and
compare base URLs in a normalized form, e.g., to detect duplicate
registrations. FOCA does not know which fields of your data hold URLs, so you
name the fields and the schemes each allows (`http`, `https` and `mailto` are
supported). For example, to let deployments restrict service URLs, which
clients send tokens to, to `https`, add an option to your [custom
configuration](configuration/custom.md):

```python
from typing import Literal

from pydantic import BaseModel, Field

from foca import get_config
from foca.utils.urls import (
    HTTP_SCHEMES,
    SUPPORTED_SCHEMES,
    check_url_fields,
    normalize_url,
)


# Field `services` of the custom configuration model
class ServicesConfig(BaseModel):
    url_schemes: list[Literal["http", "https"]] = Field(default=["https"], min_length=1)


def post_service(body: dict) -> str:
    url_schemes = get_config().custom.services.url_schemes
    check_url_fields(
        body,
        fields={
            "url": url_schemes,
            "documentationUrl": HTTP_SCHEMES,
            "contactUrl": SUPPORTED_SCHEMES,
            "organization.url": HTTP_SCHEMES,
        },
    )
    key = normalize_url(body["url"], schemes=url_schemes, name="Service URLs")
    ...
```

* `check_url(url, schemes)` checks a URL as written, so that it can be stored
  as is. It rejects, among others, URLs with whitespace (also leading or
  trailing; strip it first to tolerate it), control or format characters,
  unencoded characters that URLs do not allow (e.g., `<` or `|`) or user
  info, and hosts that are not valid domain names or IP addresses, or that
  are longer than DNS allows (253 characters, labels of 63; also checked
  before international names are encoded).
* `check_url_fields(data, fields)` checks the URLs in the given fields of an
  object, as dot-separated paths of keys (e.g., `organization.url`). Missing
  fields are not checked.
* `normalize_url(url, schemes)` checks an HTTP(S) URL and returns it
  normalized (e.g., lowercase host, no default port or trailing slash).
  Queries and fragments are removed, unless you pass `keep_query=True` or
  `keep_fragment=True`.

See the [API reference](reference.md#utilities) for all rules. Invalid URLs
raise `InvalidURL`, a `ValueError` whose message says why, naming the URLs
as passed via `name` (e.g., `URLs in 'contactUrl' need valid email
addresses.`). FOCA's problem handler responds to uncaught `InvalidURL`s with
`400 Bad Request`, without the message; to return it to clients, catch the
exception in your controller. Unsupported schemes raise a plain `ValueError`,
as they are an error of the app, not of the client. For configuration fields
that may also allow `mailto`, use `URLScheme` (`Literal["http", "https",
"mailto"]`) from `foca.utils.urls`.

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
log; it takes the same options as `log_traffic`. The function is called
without arguments on every request, in the request context, so that
`get_config()` works; e.g., with a `log_pet_requests` option in your
[custom configuration](configuration/custom.md):

```python
from foca.config.access import get_config
from foca.utils.logging import log_traffic_if


@log_traffic_if(lambda: get_config().custom.log_pet_requests)
def get_pet(pet_id: str) -> dict: ...
```

Called outside of a request context (e.g., in an app context, by a CLI
command), the decorated function runs without logging, and the function
deciding whether to log is not called.

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
