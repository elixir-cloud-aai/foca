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

## Request-specific details

Problem details mapped to an exception class are the same for all its
exceptions. So by default, requests rejected by Connexion's validation (e.g.,
for a missing parameter or a request body that does not match its schema)
get the same response, whatever was wrong with them. To tell clients the
reason, turn on `validation_details`:

```yaml
exceptions:
  validation_details: true
  max_detail_length: 500
```

Responses to Connexion's validation exceptions (`BadRequestProblem` and
`ExtraParameterProblem`) then get the validator's reason as `detail` (for
form data, the reasons joined by `; `), with the title and status as mapped
(and the mapped details if there is no reason), e.g.:

```json
{
  "title": "Bad Request",
  "status": 400,
  "detail": "'abcd' is too long - 'name'"
}
```

Like all error responses, they have the configured media type and only the
public members (without `detail` in `public_members`, the reason is not
sent). The reason is logged at `INFO`, shortened and escaped (unless
`logging` is `none`), instead of the exception; as validators quote rejected
values, these may appear in the logs (shortened), e.g., of query parameters
and headers. Connexion's validation logger (`connexion.decorators.validation`)
logs the validator's message in full and unescaped (and rejected values can
be as large as the request body), so its messages on rejected requests are
filtered out, for all apps of the process; its messages on invalid
responses are kept.
Exceptions that your app raises as `BadRequestProblem` (e.g., FOCA's
[access control](../access-control.md) for an invalid user acted for) get
their `detail` as well, so keep internals out of it. `validation_details` is
off by default, so that responses and logs stay as they are.

Details longer than `max_detail_length` (by default 500 characters, at least
1) are shortened in the middle, keeping their start and end (e.g., the field
names at the end of a validator's message), joined by `…`.

To respond with request-specific details from your controllers (e.g., which
identifier conflicts), use `problem_response()`, with extension members as
keyword arguments:

```python
from foca.errors.exceptions import problem_response
from werkzeug.exceptions import Conflict


def post_service(body):
    service_id = body["id"]
    if exists(service_id):
        return problem_response(
            Conflict(f"Service '{service_id}' exists."),
            serviceId=service_id,
        )
    ...
```

```json
{
  "title": "Conflict",
  "status": 409,
  "detail": "Service 'x' exists.",
  "serviceId": "x"
}
```

The response has the problem details mapped to the class of the werkzeug
exception, with its status code, its description as `detail` (shortened to
`max_detail_length`) and the extension members, and, like all error
responses, the configured media type, only the public members and the
headers of the exception (e.g., `WWW-Authenticate`); with `public_members`,
list the extension members there, too. Exceptions that are not werkzeug
HTTP exceptions (e.g., Connexion's) or have no HTTP error status code or
description raise a `ValueError`, as do the extension members `title`,
`status` and `detail`. To shorten request-specific
values in your own log messages the same way, use `shorten_detail()`, and
escape them with `foca.utils.logging.escape_log_value()`:

```python
from foca.errors.exceptions import shorten_detail
from foca.utils.logging import escape_log_value

logger.info("Rejected: %s", escape_log_value(shorten_detail(reason)))
```

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
Connexion's `ProblemException` (`headers`), except headers describing the
body (`Content-Type`, cf. [Media type](#media-type), `Content-Length`,
`Content-Encoding` and `Transfer-Encoding`), e.g.:

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
