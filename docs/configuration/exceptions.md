# `exceptions`

FOCA responds to all exceptions with
[problem details](https://www.rfc-editor.org/rfc/rfc9457)
(by default as `application/problem+json`, cf.
[Media type](#media-type)), e.g.:

```json
{"title": "Not Found", "status": 404}
```

By default, exceptions with an HTTP error status code (e.g., werkzeug and
Connexion exceptions, such as `werkzeug.exceptions.NotFound`) get that status
and its standard reason phrase as title, and all other exceptions `500
Internal Server Error`. To customize problem details, map exception classes to
problem details in a dictionary:

```yaml
exceptions:
  mapping: my_app.exceptions.problems
  public_members: [title, status, detail]
  logging: oneline
  media_type: application/problem+json
```

```python title="my_app/exceptions.py"
from werkzeug.exceptions import NotFound


class PetNotFound(NotFound):
    pass


problems = {
    NotFound: {"title": "Not Found", "status": 404},
    PetNotFound: {
        "title": "Pet Not Found",
        "status": 404,
        "detail": "We have never heard of this pet.",
        "type": "https://example.org/problems/pet-not-found",
    },
}
```

Each entry must have an integer `status` (400 to 599, used as the response's
status code) and a string `title`; other members are optional. Exceptions
that are not listed are handled like their closest listed base class, so a
subclass of `PetNotFound` gets the problem details of `PetNotFound`, and other
subclasses of `NotFound` those of `NotFound`. An entry for `Exception` is only
used for exceptions without status code. With `public_members`, only the
listed members are returned to clients (by default, all are).

Exceptions are logged with their full problem details, here on a single line,
including any traceback (`logging`: `oneline`, `minimal`, `regular` or
`none`).

Declare the media type of error responses (by default
`application/problem+json`) and a problem details schema for error responses
in your API specifications, as FOCA's permissions API and the examples do.

## Media type

All error responses have the media type (`Content-Type`) set in
`media_type`: errors raised by your controllers, request validation and
security errors raised by Connexion (e.g., `400 Bad Request` for a missing
parameter or `401 Unauthorized` for a missing token), and `404 Not Found` and
`405 Method Not Allowed` for unknown routes and methods. Only the media type
changes; the body is always the same problem details JSON object. Clients
cannot choose the media type (e.g., via `Accept`).

Keep the default `application/problem+json` unless your app implements an
API specification that declares another media type for error responses,
e.g., `application/json`, as many GA4GH API specifications do:

```yaml
exceptions:
  media_type: application/json
```

The media type must be a JSON media type without parameters, i.e., of the
form `type/json` or `type/subtype+json` (e.g., `application/json`,
`application/problem+json` or `application/vnd.example.error+json`), as the
body is JSON; other values raise a `pydantic.ValidationError`. Media types
are case-insensitive and stored in lower case. FOCA's permissions API
specification declares `application/problem+json` for its error responses,
whatever the configured media type.

::: foca.models.config.ExceptionConfig
    options:
      members: false

::: foca.models.config.ExceptionLoggingEnum
