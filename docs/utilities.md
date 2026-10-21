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
registrations. `foca.utils.urls` provides helpers for this, extracted from
[cloud-registry](https://github.com/elixir-cloud-aai/cloud-registry). FOCA
does not know which fields of your data hold URLs, so you configure the
allowed schemes for each of your fields:

```python
from foca.utils.urls import (
    HTTP_SCHEMES,
    SUPPORTED_SCHEMES,
    check_url_fields,
    normalize_url,
)

URL_FIELDS = {
    "url": ["https"],
    "documentationUrl": HTTP_SCHEMES,
    "contactUrl": SUPPORTED_SCHEMES,  # `http`, `https` and `mailto`
    "organization.url": HTTP_SCHEMES,
}


def post_service(body: dict) -> str:
    check_url_fields(body, fields=URL_FIELDS)
    key = normalize_url(body["url"], schemes=["https"], name="Service URLs")
    ...
```

* `check_url(url, schemes, name)` checks a URL as written, so that it can be
  stored as is. `http` and `https` URLs need a valid host (international
  host names as per IDNA 2008, IPv6 addresses with optional zone
  identifiers) and must not have user info or port `0`; `mailto` URLs need
  email addresses with domain names, and no authority. Whitespace (also
  leading or trailing), control and format characters (except zero width
  (non-)joiners in international host names) and malformed
  percent-encodings are rejected, as other tools may strip or interpret
  them differently.
* `check_url_fields(data, fields)` checks the URLs in the given fields of an
  object, as dot-separated paths of keys (e.g., `organization.url`), each
  with its allowed schemes. Missing fields are not checked.
* `normalize_url(url, schemes, name)` checks an HTTP(S) URL and returns it
  normalized: lowercase scheme and host, encoded international host names,
  canonical IPv6 addresses, no default ports, trailing dots of hosts, dot
  segments, repeated or trailing slashes, and normalized percent-encodings.
  Queries and fragments are removed, unless you pass `keep_query=True` or
  `keep_fragment=True`. `http` and `https` URLs stay different.

Invalid URLs raise `InvalidURL`, a `ValueError` whose message says why,
naming the URLs as passed via `name` (e.g., `URLs in 'contactUrl' need valid
email addresses.`), so that you can return it to clients. FOCA's problem
handler responds to uncaught `InvalidURL`s with `400 Bad Request` (without
the message, unless you [map the exception](configuration/exceptions.md)).
Schemes that are not supported raise a plain `ValueError`, as they are an
error of the app, not of the client.

To let deployments restrict schemes, e.g., to allow only `https` for URLs
that clients send tokens to, add an option to your [custom
configuration](configuration/custom.md), so that invalid values are rejected
on startup:

```python
from typing import Literal

from pydantic import BaseModel, Field


class ServicesConfig(BaseModel):
    # pydantic copies mutable defaults for each instance
    url_schemes: list[Literal["http", "https"]] = Field(default=["https"], min_length=1)
```

For fields that may also hold `mailto` URLs, use `URLScheme`
(`Literal["http", "https", "mailto"]`) from `foca.utils.urls`.

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
