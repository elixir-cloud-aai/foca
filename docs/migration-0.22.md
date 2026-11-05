# Migrating to 0.22.0

## Framework-neutral errors

FOCA prepares for its release on ASGI, without Flask. Nothing breaks in this release;
do the following before the first release candidate on ASGI.

Raise and map the classes of `foca.errors` rather than those of Werkzeug and Connexion:

| Instead of | Use |
| --- | --- |
| `werkzeug.exceptions.BadRequest` | `foca.errors.BadRequest` |
| `werkzeug.exceptions.Unauthorized` | `foca.errors.Unauthorized` |
| `werkzeug.exceptions.Forbidden`, `connexion.exceptions.Forbidden` | `foca.errors.Forbidden`; `foca.errors.AccessDeniedProblem` in exception maps for Connexion's |
| `werkzeug.exceptions.NotFound` | `foca.errors.NotFound` |
| `werkzeug.exceptions.MethodNotAllowed` | `foca.errors.MethodNotAllowed` |
| `werkzeug.exceptions.Conflict` | `foca.errors.Conflict` |
| `werkzeug.exceptions.RequestEntityTooLarge` | `foca.errors.PayloadTooLarge` |
| `werkzeug.exceptions.UnprocessableEntity` | `foca.errors.UnprocessableContent` |
| `werkzeug.exceptions.InternalServerError` | `foca.errors.InternalServerError` |
| `werkzeug.exceptions.ServiceUnavailable` | `foca.errors.ServiceUnavailable` |
| `werkzeug.exceptions.HTTPException` | `foca.errors.HTTPError` |
| `connexion.exceptions.BadRequestProblem` | `foca.errors.RequestValidationProblem` |
| `connexion.exceptions.ExtraParameterProblem` | `foca.errors.ExtraParameterProblem` |
| `connexion.exceptions.OAuthProblem` | `foca.errors.AuthenticationProblem` |

Other arguments of the Werkzeug classes (`valid_methods`, `www_authenticate`,
`retry_after`) are keywords in the new classes, e.g., `MethodNotAllowed(valid_methods=["GET"])`.

Exception maps with raw Werkzeug classes keep working, but FOCA logs a warning, once per
class, except for `werkzeug.exceptions.Forbidden`, which is the same class as
`AccessDeniedProblem` for now, so replace it yourself; they stop working with the first
release candidate on ASGI.

## Invalid fields in `400` responses

Nothing breaks: responses to invalid requests keep their `title`, `status`
and `detail`, and add the members `type`, `code` and `errors`, which list all
invalid fields of the request, not only the first (see [Invalid
request](problems/invalid-request.md)). Requests rejected for invalid
parameters or bodies now raise `foca.errors.InvalidFieldsProblem`, a subclass
of `RequestValidationProblem`, so exception maps and `except` clauses keep
working. Check the following:

- With `exceptions.public_members`, add `type`, `code` and `errors` to send
  the invalid fields.
- A `type` or `code` mapped to `RequestValidationProblem` is kept; remove it
  to get those of FOCA.
- Clients and tests that compare whole problem details of `400` responses
  get the new members.
- Rejected requests with invalid fields are logged by the locations and
  codes of the fields (e.g., `Request rejected: #/name (too-long)`) instead
  of the validator's message, which quotes the rejected values.
- Declare `errors` in the problem details of your API specifications, e.g.,
  with FOCA's reusable schemas, and raise `InvalidFieldsProblem` for your own
  checks of fields.
