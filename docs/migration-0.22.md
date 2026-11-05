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
release candidate on ASGI. `www_authenticate` of
`Unauthorized` exceptions that FOCA raises for tokens is no longer set: the challenge is
in `get_headers()`, which is what goes into responses.
