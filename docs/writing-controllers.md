# Writing controllers

Controllers are the functions that the operations of your API specifications
name (`operationId`). FOCA runs on Connexion 2 and Flask; a later release runs
on Connexion 3 and ASGI, without Flask (see [Migrating to
0.22.0](migration-0.22.md)). Controllers written as described here work on both
without changes:

- Take the inputs of the request from the arguments that Connexion passes.
- Read everything else via FOCA's accessors, not via Flask or Connexion.
- Return the body, status and headers as values, not as Flask responses.
- Read the configuration via `get_config()`.

The examples implement this operation, and a few more below:

```yaml title="openapi.yaml"
paths:
  /items/{id}:
    put:
      operationId: my_app.controllers.put_item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
        - name: dry_run
          in: query
          schema:
            type: boolean
            default: false
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
      responses:
        "200":
          description: Item replaced.
          content:
            application/json:
              schema:
                type: object
        "201":
          description: Item created.
          content:
            application/json:
              schema:
                type: object
```

## Inputs as arguments

Connexion passes the parameters of the operation to the controller as keyword
arguments, converted to the types of their schemas and with their defaults:
path and query parameters by their names in the specification, without
characters other than letters, digits and `_` (e.g., `page-size` as
`pagesize`, so prefer names such as `page_size`), and the request body as
`body`:

```python title="my_app/controllers.py"
from typing import Any

# For the examples only: in memory, not thread-safe, unescaped in CSV
ITEMS: dict[str, dict[str, Any]] = {}


def put_item(
    id: str, body: dict[str, Any], dry_run: bool = False
) -> tuple[dict[str, Any], int]:
    item = {**body, "id": id}
    status = 200 if id in ITEMS else 201
    if not dry_run:
        ITEMS[id] = item
    return item, status
```

Connexion passes path parameters always, so declare them all, and query
parameters and the body only if the controller declares them (all of them
with `**kwargs`). Keep the default name `body` for request bodies rather than
renaming them via `x-body-name`, as FOCA's own [permission
controllers](access-control.md#configuring-the-api) do. Don't read parameters
or the body from the request instead, e.g., from `flask.request.args` or
`request.json`: the [snapshot of the current
request](utilities.md#reading-the-current-request) has no body, and its
`query` holds the raw query parameters, unconverted and unchecked, for
building URLs such as links to other pages.

Controllers of operations with security get the result of the token validation
as arguments `user` and `token_info`, if they declare them (see [Using token
claims](authentication.md#using-token-claims)).

## Everything else via accessors

Read what isn't an input of the operation via FOCA's accessors, which work the
same whatever the web framework:

| To read | Use | Instead of |
| --- | --- | --- |
| Method, path, URL, headers, client address, route of the request | `foca.context.current_request()` (see [Reading the current request](utilities.md#reading-the-current-request)) | `flask.request`, `connexion.request` |
| Token info and claims, e.g., in helpers | `current_token_info()` and `current_claims()` of `foca.security.auth` (see [Using token claims](authentication.md#using-token-claims)) | `connexion.context`, `request_ctx.connexion_context` |
| Subject, groups and admin rights of the caller | `get_current_subject()`, `get_current_groups()`, `is_admin()` of `foca.security.access_control` (see [The current caller](access-control.md#the-current-caller)) | Own parsing of claims |
| Configuration | `foca.get_config()`, `foca.get_custom_config(MyCustomConfig)` (see [below](#configuration)) | `current_app.config.foca`, `get_app_config()` |
| MongoDB collections | `foca.get_collection(db=..., collection=...)` (see [Accessing the configuration](configuration/index.md#accessing-the-configuration)) | `current_app.config.foca.db` |

```python title="my_app/controllers.py"
from typing import Any

from foca.context import current_request
from foca.security.auth import current_claims


def get_whoami() -> dict[str, Any]:
    request = current_request()
    return {
        "subject": current_claims().get("sub"),  # None without a token
        "route": request.route,  # e.g., "/whoami"
        "accept": request.headers.get("accept"),
    }
```

Raise the errors of `foca.errors`, e.g., `NotFound`, rather than those of
Werkzeug and Connexion (see [Framework-neutral
errors](migration-0.22.md#framework-neutral-errors)).

## Return values

Return the body, and optionally the status and headers, as values, and
`NoContent` for responses without a body (Connexion's `NoContent` is a marker
that works the same on Connexion 2 and 3):

```python title="my_app/controllers.py"
from typing import Any

from connexion import NoContent

from foca.errors import NotFound

# ITEMS as above


def get_item(id: str) -> dict[str, Any]:  # body; status 200
    if id not in ITEMS:
        raise NotFound
    return ITEMS[id]


def list_items() -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    return list(ITEMS.values()), 200, {"X-Total-Count": str(len(ITEMS))}


def delete_item(id: str) -> tuple[object, int]:
    if ITEMS.pop(id, None) is None:
        raise NotFound
    return NoContent, 204


def export_items() -> tuple[str, int, dict[str, str]]:
    csv = "".join(f"{item_id}\n" for item_id in ITEMS)
    headers = {
        "Content-Type": "text/csv",
        "Content-Disposition": 'attachment; filename="items.csv"',
    }
    return csv, 200, headers
```

- **Forms:** `body`, `(body, status)`, `(body, status, headers)` and
  `(body, headers)`; headers as a dictionary. For `Location` headers, see
  [Building public URLs](utilities.md#building-public-urls).
- **Bodies:** dictionaries and lists are sent as JSON. Strings are sent as
  JSON strings for JSON media types, and for responses that declare no
  `content` (e.g., `"hello"` with quotes), and as they are for other media
  types (e.g., `text/csv` in `export_items()`). Bytes are sent as they are
  for other media types than JSON, e.g., for downloads as
  `application/octet-stream`, with `Content-Type` and `Content-Disposition`
  headers as in `export_items()`; for JSON media types, return data, not
  bytes (`500 Internal Server Error` on Connexion 2 and 3).
- **No body:** return `NoContent` with the status, e.g., `NoContent, 204`,
  not `None`.
- **Media types:** if a response declares several media types (e.g.,
  `application/json` and `text/plain`), set `Content-Type` to one of them. If
  you set `Content-Type`, use one of the declared media types exactly, e.g.,
  `text/csv`, not `text/csv; charset=utf-8`, if `text/csv` is declared.

Don't return `flask.Response` objects, including those of
`flask.make_response()`, `flask.send_file()` and `flask.jsonify()`: they work
only with Flask.

### Connexion 2 and 3

The return values above give the same status, headers and data on Connexion 2
(2.15, as FOCA uses it) and Connexion 3 (3.3, with its Flask app and its ASGI
app, `AsyncApp`), with these differences:

| Return value | Connexion 2 | Connexion 3 |
| --- | --- | --- |
| Dictionary or list | JSON, indented by 2 spaces | JSON, indented with the Flask app, compact with the ASGI app; the same data |
| String with a text media type, e.g., `text/plain` | `Content-Type` with `; charset=utf-8` added | The same with the Flask app; the media type as declared, without `charset`, with the ASGI app |
| `Content-Type` header that isn't a declared media type, e.g., with `charset` | Sent | `500 Internal Server Error` |
| No `Content-Type` header, several media types declared | One of them | `500 Internal Server Error` |
| `None, 204` | `Content-Type: application/json` | `Content-Type: text/html; charset=utf-8` with the Flask app, none with the ASGI app |
| `NoContent, 204` | Without body, the same headers | The same |
| `flask.Response` | Sent | Sent with the Flask app, `500 Internal Server Error` with the ASGI app |

So compare JSON bodies as data, not as text, e.g., in tests, and don't rely on
a `charset` in `Content-Type`.

## Configuration

Read the configuration via `foca.get_config()`, and your custom configuration,
typed, via `foca.get_custom_config()` with its model; see [Accessing the
configuration](configuration/index.md#accessing-the-configuration) and
[`custom`](configuration/custom.md). `current_app.config.foca` and
`get_app_config()` are deprecated (see [Configuration
access](migration-0.22.md#configuration-access)).
