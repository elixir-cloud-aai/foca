# Migrating to 0.22.0

FOCA 0.22.0 prepares for the ASGI release, which removes Flask: see
[Deprecated, removed in the ASGI release](#deprecated-removed-in-the-asgi-release)
and [Preparing for the ASGI release](preparing-for-asgi.md).

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
| `foca.config.access.set_app_config(app, config)` | Nothing for apps that FOCA creates; for scripts, see [Preparing for the ASGI release](preparing-for-asgi.md#configuration) |
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

To find remaining uses, turn deprecation warnings into errors in your tests
(see [Finding what still has to
move](preparing-for-asgi.md#finding-what-still-has-to-move)) or search for
`config.foca` and `get_app_config`.

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

## Test client

Nothing breaks. On ASGI, Flask's test client bypasses the middleware that
serves requests (security, validation, CORS, errors), and Flask's test
contexts are gone. Move your tests to `foca.testing` (see [Test
client](testing.md#test-client)), whose API stays:

| Instead of | Use |
| --- | --- |
| `app.app.test_client()` | `with foca.testing.Client(app) as client:`, or the fixture `foca_client` of the pytest plugin with a fixture `foca_app` of yours |
| `response.json`, `response.get_json()` | `response.json()` |
| `response.data`, `response.get_data()` | `response.content` |
| `response.get_data(as_text=True)` | `response.text` |
| `response.content_type`, `response.mimetype` | `response.headers["content-type"]` |
| `client.get(..., query_string=...)` | `client.get(..., params=...)` |
| `client.post(..., data=b"...", content_type="...")` | `client.post(..., content=b"...", headers={"Content-Type": "..."})` |
| `client.open(path, method="PUT")` | `client.request("PUT", path)` |
| `client.set_cookie(...)` | A `Cookie` header |
| `app.app.test_request_context(path, method=..., headers=..., query_string=...)` | `foca.testing.request_context(path, method, headers, app=app, params=...)`; for code that reads the body, send a request with `Client` |
| Setting `request_ctx.connexion_context["token_info"]` | `request_context(..., token_info={...})` |
| `app.app.app_context()`, to call controllers or helpers | `request_context(app=app)`, which gives the app context too; `get_config()` needs neither (see [Configuration access](#configuration-access)) |

`form` data (`data={...}`) stays as it is. Redirects aren't followed unless
you pass `follow_redirects=True`, as with Flask's client. `params` replace
query parameters of the same name in the URL, as in httpx, rather than adding
to them. Don't rely on the client address of requests of the client, which
changes with the release on ASGI; set it with `request_context(...,
client_address=...)`. Tests of WSGI middleware, which need other WSGI
environment values, keep using Werkzeug's `Client` on the Flask app until the
release on ASGI.

## Invalid fields in `400` responses

Nothing breaks: responses to invalid requests keep their `title`, `status`
and `detail`, and add the members `type`, `code` and `errors`, which list all
invalid fields of the request, not only the first, those of the body and the
parameters together (see [Invalid request](problems/invalid-request.md)).
`detail` still describes the first invalid field of the body, if any, as
Connexion checks the body before the parameters. Requests rejected for invalid
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

## Finding Flask imports

Nothing breaks. FOCA's repository has the [ast-grep](https://ast-grep.github.io/)
rule `flask-import`
([`.ast-grep/rules/flask-import.yml`](https://github.com/elixir-cloud-aai/foca/blob/dev/.ast-grep/rules/flask-import.yml);
it isn't part of the Python package), which finds what still has to move
before the ASGI release: imports of Flask and its extensions (e.g.,
Flask-Cors), Werkzeug, Connexion's Flask modules, and Connexion's `request`
and `context`. Its note names the replacement for each: the
accessors above, `foca.errors`, `foca.testing`, `foca.get_config()`, and
tuples as responses. Werkzeug stays installed, but don't import it.

To adopt it, copy the file into your app's `.ast-grep/rules/`, set its
`files:` to your package and tests (e.g., `my_app/**` and `tests/**`), and run
`ast-grep scan` (e.g., via pre-commit). The rule has the severity `warning`
from FOCA 0.22, so the scan reports but doesn't fail; with the ASGI release,
FOCA's copy becomes an `error`: update your copy then.

## Deprecated, removed in the ASGI release

Nothing breaks in 0.22. The first release candidate of the ASGI release moves
to Connexion 3 `AsyncApp` and removes Flask, Flask-Cors and everything in FOCA
that takes a Flask app. In 0.22, FOCA's functions that take a Flask app
(`get_app_config()`, `set_app_config()`, `enable_cors()` and
`foca.factories.celery_app.create_celery_app()`), `app.config.foca` and
`www_authenticate` of `foca.errors.Unauthorized` emit a `DeprecationWarning`,
and exception maps with raw Werkzeug classes are logged as deprecated; FOCA
logs each deprecation once per process. Flask's own objects can't warn; the
ast-grep rule `flask-import` reports them (see [Finding Flask
imports](#finding-flask-imports)).

[Preparing for the ASGI release](preparing-for-asgi.md) lists everything that
goes away, with what to use now, examples, and how to find what is left.

## Validation reasons without request values

The `detail` of responses to requests rejected by Connexion's validation
changes by default. Before, with `exceptions.validation_details` (the
default), it was the validator's reason, which quotes rejected values, e.g.,
`'abcd' is too long - 'name'`, and for parameters also the schema. Now it
names the invalid fields by location and code, e.g., `Invalid fields: #/name
(too-long)`, or, if no invalid fields are listed (e.g., for a missing
parameter, a body that isn't valid JSON or has unpaired surrogates), it is
the mapped `detail`, if any, or `The request is invalid.`
The invalid fields are listed in `errors`, without their values (see
[Request-specific
details](configuration/exceptions.md#request-specific-details)). Status,
`title` and the logs are unchanged.

Update tests and clients that check the old messages in `detail`: check the
invalid fields in `errors` instead, e.g., their `pointer` or `parameter` and
`code`. If clients depend on the reasons, set the new option
`exceptions.validation_reasons: true` to keep them.
