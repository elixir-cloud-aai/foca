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
Internal Server Error`. For `413`, `414`, `416` and `422`, titles are the
[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110) phrases on all Python
versions (e.g., `Content Too Large`; Python 3.12 and earlier have older ones,
e.g., `Request Entity Too Large`). To customize problem details, map exception
classes to problem details in a dictionary:

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
are case-insensitive and stored in lower case.

FOCA's default [permissions API](access-control.md) specification declares
the configured media type for its error responses. Custom specifications
(`security.access_control.api_specs`) and your own specifications must
declare it themselves.

## Headers

Error responses keep the headers of werkzeug HTTP exceptions and of
Connexion's `ProblemException` (`headers`), except `Content-Type` (cf.
[Media type](#media-type)), e.g.:

- `Allow` for `405 Method Not Allowed` (unknown methods, or
  `MethodNotAllowed(valid_methods=[...])`)
- `WWW-Authenticate` for `401 Unauthorized`
  (`Unauthorized(www_authenticate=...)`)
- `Retry-After` for, e.g., `503 Service Unavailable`, if set
  (`ServiceUnavailable(retry_after=120)`)

Headers are dropped if the exception is mapped to problem details with
another status, as they may not apply to it.

`401 Unauthorized` responses of FOCA's
[token validation](../authentication.md) and access control carry a Bearer
challenge as per [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750#section-3),
so that clients can tell, e.g., that they need to log in again:

- `WWW-Authenticate: Bearer error="invalid_token", error_description="The
  access token could not be validated"` for invalid tokens (e.g., malformed,
  expired or of an untrusted issuer)
- `WWW-Authenticate: Bearer` otherwise, e.g., if the identity provider is not
  available or the token lacks the identity claim of
  [access control](../access-control.md), as logging in again would not
  help

Security errors raised by Connexion without a challenge of their own (e.g.,
for a missing token) get `WWW-Authenticate: Bearer`, as FOCA supports Bearer
tokens only. For operations secured with other schemes (e.g., HTTP Basic),
raise exceptions with their own challenge in your security functions
(`Unauthorized(www_authenticate=...)`).

::: foca.models.config.ExceptionConfig
    options:
      members: false

::: foca.models.config.ExceptionLoggingEnum
