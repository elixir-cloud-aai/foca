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

Other arguments of the Werkzeug classes (`valid_methods`, `retry_after`) are
keywords in the new classes, e.g., `MethodNotAllowed(valid_methods=["GET"])`.

### Challenges in `headers`

`foca.errors.Unauthorized` carries its `WWW-Authenticate` challenges in
`headers`, like any other header, also those of FOCA's token validation and
access control. Werkzeug's `www_authenticate` is deprecated on this class:

- Pass challenges as headers, e.g.,
  `Unauthorized(headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})`,
  instead of `Unauthorized(www_authenticate=...)`.
- Read them from `exc.get_headers()` instead of `exc.www_authenticate`, e.g.,
  to tell an invalid token (`Bearer error="invalid_token"`) from other
  rejections.

Give several challenges as one header each, e.g.,
`headers=[("WWW-Authenticate", "Basic"), ("WWW-Authenticate", "Bearer")]`;
FOCA reads one challenge per header.

Both still work, with a `DeprecationWarning`: the argument adds its
challenges to `headers`, and the attribute returns copies of those of
`headers` (`None` if there are none), so changes to them are lost unless
they are set again. They are removed with the first release
candidate on ASGI.

Exception maps with raw Werkzeug classes keep working, but FOCA logs a warning, once per
class, except for `werkzeug.exceptions.Forbidden`, which is the same class as
`AccessDeniedProblem` for now, so replace it yourself; they stop working with the first
release candidate on ASGI.

## Configuration access

`foca.get_config()` and `foca.get_custom_config()` are the only supported ways
to read the configuration in apps. Nothing breaks in this release, but the
following are deprecated, emit a `DeprecationWarning` and are removed with the
first release candidate on ASGI, which has no Flask app to attach the
configuration to:

| Instead of | Use |
| --- | --- |
| `current_app.config.foca`, `app.config.foca` | `get_config()`, in the app context of the app if it isn't the current one |
| `foca.config.access.get_app_config(app)` | `get_config()`, as above |
| `current_app.config.foca.custom` | `get_custom_config(MyCustomConfig)`, typed |
| Setting `app.config.foca` | Change the configuration that `get_config()` returns, e.g., `get_config().db = ...` |

`get_config()` returns the configuration of the current app, or, outside app
contexts (e.g., in Celery workers or scripts), that of the most recently
created FOCA app. Where you passed an app that isn't the current one, e.g., in
tests, enter its app context:

```python
# Before
config = get_app_config(app)
# After
with app.app_context():
    config = get_config()
```

To find remaining uses, run your tests with
`-W error::DeprecationWarning` or search for `config.foca` and
`get_app_config`.

To warn, FOCA gives Flask configurations of Flask's own class (as Connexion
creates them) a subclass of it until the attribute is removed:
`isinstance(app.config, flask.Config)` holds, but `type(app.config) is
flask.Config` doesn't. Apps with a configuration class of their own
(`config_class`) keep it, and `app.config.foca` doesn't warn there.

## Request and claims accessors

Nothing breaks. To prepare for the release on ASGI, where Flask's request and
Connexion's context are gone or differ (e.g., in which part of the path they
give), read the request in controllers and helpers via FOCA's accessors:

| Instead of | Use |
| --- | --- |
| `flask.request.method`, `.path`, `.script_root`, `.url`, `.headers`, `.content_length`, `.remote_addr` | `foca.context.current_request()`: `method`, `path`, `root_path`, `url`, `headers`, `content_length`, `client_address` |
| `flask.request.environ["SERVER_PROTOCOL"]` | `current_request().protocol` |
| `flask.request.args`, to build URLs (e.g., links to other pages) | `current_request().query` (as sent, read-only; `getlist()` for all values) |
| `flask.request.endpoint` | `current_request().operation_id` (the `operationId` of the specification), or `foca.utils.urls.get_operation_id()` |
| `flask.request.blueprint`, to find the API of the request | `foca.utils.urls.get_api_base_path()` |
| `flask.request.url_rule`, `.view_args` | `current_request().route` (the path template of the specification with the base path of the API, e.g., `/ga4gh/tes/v1/tasks/{id}`, not Flask's `<id>`), `.path_params` |
| `request_ctx.connexion_context["token_info"]`, `connexion.context` | `foca.security.auth.current_token_info()` |
| Claims from the token info | `foca.security.auth.current_claims()` (`get_current_claims()` returns the same) |

Take path, query and body parameters, e.g., instead of `request.json` or
`request.args`, from the arguments that Connexion passes to controllers. See
[Reading the current request](utilities.md#reading-the-current-request).

FOCA's own access control, resource ownership, token validation, plugin
gateway, URL and pagination helpers, logging and request validation read the
request in the same way. The one exception is the request body size limit
(`server.max_content_length`), which is middleware: it reads the raw input
(`wsgi.input`) before Connexion does, and moves to ASGI middleware with the
release on ASGI. What changes with it:

- The `operation_id` field of [log records](configuration/log.md) and the
  operation named in logs of nonconforming responses come from FOCA's route
  table: the `operationId` of the specification, also for operations routed
  via `x-openapi-router-controller` (before, e.g., `controllers_findPets` for
  `findPets`), and empty for routes that aren't operations of the app's
  specifications (e.g., added via Flask, or Flask's automatic answers to
  `OPTIONS`), and for Connexion apps not created by FOCA.
- `get_api_base_path()` and `get_resource_path()` find the API of the
  current request from FOCA's registry of APIs instead of Flask's
  blueprints. For requests to routes that aren't operations (e.g., the
  Swagger UI of an API), they fall back as for requests outside the APIs.
- Query strings that aren't UTF-8 no longer fail pagination links or the
  check for unexpected query parameters; such bytes stay percent-encoded.

The controllers of the permissions API, `postPermission()` and
`putPermission()`, take the rule as the request body that Connexion passes as
`body`, so FOCA's specification no longer sets `x-body-name`. Specifications of
your own that route operations to them (e.g., via `access_control.api_specs`,
copied from FOCA's) keep working with `x-body-name: permission`, but it is
deprecated: the controllers emit a `DeprecationWarning` for requests that pass
the body as `permission`, which Python shows only where deprecation warnings
are enabled, e.g., in tests or with `-W default::DeprecationWarning`, so check
your specifications. Remove `x-body-name` from their request bodies; with any
other name, the controllers fail (`500`). When you call them directly, e.g., in
tests, pass the rule as keyword argument `body`.

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

## Deprecations in the logs

Nothing breaks. Besides emitting a `DeprecationWarning`, FOCA now logs each of
its deprecations once per process (once per Gunicorn worker) at `WARNING`, on
the logger `foca.deprecation` (see
[Deprecations](configuration/log.md#deprecations)), so that deprecated uses
show in the logs of deployed apps, where Python hides the warnings. Silence
them by setting a higher level for that logger.
