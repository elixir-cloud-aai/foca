# Invalid request

<!-- The URL of this page is the problem type
`https://foca.readthedocs.io/en/latest/problems/invalid-request/`
(`foca.errors.fields.INVALID_REQUEST_TYPE`): don't move or rename it. -->

Problem type `https://foca.readthedocs.io/en/latest/problems/invalid-request/`,
code `invalid-request`: the request has invalid fields, listed in `errors`.

FOCA apps respond to requests with invalid fields with `400 Bad Request` and
[problem details](https://www.rfc-editor.org/rfc/rfc9457) that list every
invalid field in the extension member `errors`, in the form of the example
of [RFC 9457, section 3](https://www.rfc-editor.org/rfc/rfc9457#section-3),
so that clients can show each message at its field, without validation rules
of their own:

```json
{
  "type": "https://foca.readthedocs.io/en/latest/problems/invalid-request/",
  "title": "Bad Request",
  "status": 400,
  "detail": "'abcd' is too long - 'name'",
  "code": "invalid-request",
  "errors": [
    {
      "detail": "Must be at most 3 characters long.",
      "pointer": "#/name",
      "code": "too-long"
    },
    {
      "detail": "Must be a valid URI.",
      "pointer": "#/organization/url",
      "code": "invalid-format"
    },
    {
      "detail": "Is required.",
      "pointer": "#/url",
      "code": "required"
    }
  ]
}
```

Invalid parameters are named instead, e.g.,
`{"detail": "Must be at most 10.", "parameter": "limit", "code": "too-large"}`.
`title`, `status` and `detail` are as before, so clients that only show
`detail` keep working; `detail` describes the first invalid field, in the
validator's words, which may quote the rejected value. With
`exceptions.validation_reasons: false`, it names the invalid fields by
location and code instead, e.g., `Invalid fields: #/name (too-long)` (see
[Request-specific
details](../configuration/exceptions.md#request-specific-details)). Nothing
in the format is specific to FOCA: generic RFC 9457 clients understand
`errors`, and clients can show the messages without knowing the codes.

## Entries

Each entry of `errors` has:

- `detail`: a readable message. Messages are written from the schema (e.g.,
  the maximum length), never from the rejected value, which may hold secrets,
  e.g., credentials in URLs. Locations, though, may have keys and parameter
  names of the request, e.g., of unknown properties and parameters.
- `pointer`: the field in the request body, as a
  [JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901) in URI fragment form,
  e.g., `#/organization/url` or `#/tags/0` (`#` for the whole body); or
  `parameter`: the name of the query, path, header, cookie or form data
  parameter.
- `code`: a stable, kebab-case code of what is wrong, e.g., `too-long`.

There are at most 100 entries. Long messages, parameter names and pointers
are shortened to `exceptions.max_detail_length` (pointers to the closest
containing field that fits).

## Codes

FOCA's request validation uses these codes:

| Code | Meaning |
| --- | --- |
| `required` | A required property or parameter is missing. |
| `invalid-type` | The value has the wrong type. |
| `invalid-value` | The value is not one of the allowed values (`enum`). |
| `invalid-format` | The value doesn't have its format, e.g., `uri` or `date-time`. |
| `too-long` | The string is too long (`maxLength`). |
| `too-short` | The string is too short (`minLength`). |
| `pattern-mismatch` | The string doesn't match its pattern. |
| `too-large` | The number is too large (`maximum`). |
| `too-small` | The number is too small (`minimum`). |
| `not-multiple-of` | The number is not a multiple of `multipleOf`. |
| `too-many-items` | The array has too many items (`maxItems`). |
| `too-few-items` | The array has too few items (`minItems`). |
| `duplicate-items` | The array has duplicate items (`uniqueItems`). |
| `too-many-properties` | The object has too many properties (`maxProperties`). |
| `too-few-properties` | The object has too few properties (`minProperties`). |
| `missing-dependency` | A property requires others (`dependencies`). |
| `unknown-property` | The object has a property its schema doesn't allow (`additionalProperties: false`). |
| `unknown-parameter` | The request has a parameter that the specification doesn't define (`strict_validation`). |
| `no-match` | The value matches none, or more than one, of the schemas of `anyOf` or `oneOf`. |
| `not-allowed` | The value matches the schema of `not`. |
| `read-only` | The request sets a read-only property. |
| `invalid` | Any other constraint. |

Apps add codes of their own for their own checks (see below), e.g.,
`scheme-not-allowed`; document them in their API specifications.

## What is collected

All invalid fields of the request body (JSON or form data, against the
schema of the request body) are listed, and all invalid parameters, together
in one response: first the fields of the body, then the parameters; `detail`
describes the first invalid field of the body, if any. With
`strict_validation`, unknown parameters are listed together with the invalid
declared ones; for operations without declared parameters, unknown query
parameters are only listed if the body is valid. If the invalid declared
parameters can't be listed (see below), the others are listed without them.
Body and parameters are listed together with FOCA's validators only, not
with validators of your own in the Connexion option `validator_map`. The
app's own checks run on valid requests only.
Requests rejected for other reasons, e.g., a body that is not valid JSON or
has unpaired surrogates, or form fields of an OpenAPI 3 request body that
can't be converted to their type, get no `errors`, but `detail` as before,
even if parameters are invalid too. Otherwise, if any invalid field can't be
listed (e.g., for an unexpected schema), the request is rejected as before,
without `errors`, and a warning is logged.

## Checks of your app

Raise `foca.errors.InvalidFieldsProblem` with the invalid fields of checks of
your own, e.g., of URLs, so that clients get them like those of the schema
validation:

```python
from foca.errors import FieldError, InvalidFieldsProblem, json_pointer


def post_service(body):
    errors = []
    for index, service in enumerate(body["services"]):
        if not service["url"].startswith("https://"):
            errors.append(
                FieldError(
                    detail="Must use HTTPS.",
                    code="scheme-not-allowed",
                    pointer=json_pointer("services", index, "url"),
                )
            )
    if errors:
        raise InvalidFieldsProblem(errors)
    ...
```

`json_pointer()` escapes the keys as per RFC 6901 and URI fragments. Write
messages without the rejected values. `InvalidFieldsProblem` is a
`RequestValidationProblem`: it is mapped and handled like Connexion's request
validation errors. Without a `detail` of its own, `detail` lists the
locations and messages of the fields.

## Configuration

Invalid fields are part of the request-specific details of
`exceptions.validation_details` (on by default; cf.
[`exceptions`](../configuration/exceptions.md#request-specific-details)):
without them, responses have the problem details mapped to the exception
class only. A `type` or `code` mapped to the exception class (e.g., to
`RequestValidationProblem`) is kept, e.g., for a problem type URI of your
own. With `exceptions.public_members`, list
`type`, `code` and `errors` there to send them. Set
`exceptions.validation_reasons: false` to send the invalid fields' locations
and codes as `detail` instead of the validator's reason, which may quote
rejected values (cf.
[`exceptions`](../configuration/exceptions.md#request-specific-details)).

## API specifications

Declare the problem details with `errors` in your API specifications. FOCA
ships reusable OpenAPI 3 schemas, `Problem` and `FieldError`, in
`foca/api/problem-details.yaml` (in the installed package:
`importlib.resources.files("foca.api") / "problem-details.yaml"`). Copy the
file next to your specification and merge it into it, then refer to the
schemas in your error responses:

```yaml
api:
  specs:
    - path:
        - specs/openapi.yaml
        - specs/problem-details.yaml
```

```yaml title="specs/openapi.yaml"
paths:
  /services:
    post:
      responses:
        "400":
          description: The request is invalid.
          content:
            application/problem+json:
              schema:
                $ref: "#/components/schemas/Problem"
```

FOCA's [permissions API](../access-control.md) declares the same schemas.
