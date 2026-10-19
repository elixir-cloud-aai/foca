# `exceptions`

FOCA responds to all exceptions with
[problem details](https://www.rfc-editor.org/rfc/rfc9457)
(`application/problem+json`), e.g.:

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

Declare `application/problem+json` and a problem details schema for error
responses in your API specifications, as FOCA's permissions API and the
examples do.

::: foca.models.config.ExceptionConfig
    options:
      members: false

::: foca.models.config.ExceptionLoggingEnum
