# Preparing for the ASGI release

The first release candidate of FOCA's ASGI release moves to Connexion 3
`AsyncApp` and removes Flask and Flask-Cors. FOCA 0.22 is the preparation
release: it removes nothing, but deprecates what goes away and adds
replacements that already work on Connexion 2. Move your app to them now, on
FOCA 0.22, so that the upgrade to the ASGI release is mostly a version bump.

FOCA keeps a `0.22.x` branch with dependency and security fixes while apps
migrate.

## Removed in the ASGI release, and what to use now

| Removed in the ASGI release | Use now (FOCA 0.22) |
| --- | --- |
| [Flask app object](#flask-app-object): `app.app` | Nothing that needs it: `get_config()`, `foca.testing`, FOCA's configuration |
| [Request objects](#request-objects): `flask.request`, `g`, `current_app`, `connexion.request`, `connexion.context`, `request_ctx` | Arguments of controllers, `foca.context.current_request()`, `current_token_info()` and `current_claims()` of `foca.security.auth` |
| [Configuration](#configuration): `current_app.config.foca`, `get_app_config()`, `set_app_config()` | `foca.get_config()`, `foca.get_custom_config()` |
| [Errors](#errors): exceptions of Werkzeug and Connexion | `foca.errors` |
| [Responses](#responses): `flask.Response`, `make_response()`, `send_file()`, `jsonify()` | `body`, `(body, status)` or `(body, status, headers)` tuples |
| [Tests](#tests): Flask's test client, `app_context()`, `test_request_context()` | `foca.testing.Client`, `foca.testing.request_context`, the `foca_client` fixture |
| [CORS](#cors): Flask-Cors, `enable_cors(flask_app)` | `security.cors` of the configuration (`CORSConfig` stays) |
| [Celery](#celery): `foca.factories.celery_app.create_celery_app(flask_app)` | `Foca.create_celery_app()` |
| [Serving](#serving): Gunicorn's `gthread` worker class and `threads` | Nothing before the ASGI release: keep `foca.gunicorn_config` |
| [Deployment](#deployment): the `gunicorn.threads` value of the Helm chart | Nothing before the ASGI release |

The functions that take a Flask app (`get_app_config()`, `set_app_config()`,
`enable_cors()` and `foca.factories.celery_app.create_celery_app()`), the
`app.config.foca` attribute and the `www_authenticate` attribute of
`foca.errors.Unauthorized` emit a `DeprecationWarning`, and FOCA logs each
deprecation once per process (see [Deprecations in the
logs](migration-0.22.md#deprecations-in-the-logs)). Exception maps with raw
Werkzeug classes keep working in 0.22, with a logged warning, and stop working
with the ASGI release (see [Framework-neutral
errors](migration-0.22.md#framework-neutral-errors)).

### Flask app object

`Foca.create_app()` returns a Connexion app, whose `app` attribute is the
Flask app in 0.22. The ASGI release has no Flask app. Use the replacements
below for what you used the Flask app for; FOCA attaches the configuration,
CORS and error handlers itself.

```python
# Before
app = Foca(config_file="config.yaml").create_app()
client = app.app.test_client()
config = get_app_config(app.app)

# After
app = Foca(config_file="config.yaml").create_app()
client = Client(app)  # foca.testing.Client
config = get_config()
```

### Request objects

Take path, query and body parameters from the arguments that Connexion passes
to controllers, and the rest from FOCA's accessors (see [Request and claims
accessors](migration-0.22.md#request-and-claims-accessors)). For data kept per
request in `g`, pass it as an argument instead.

```python
# Before
from flask import request
from connexion import context


def get_whoami():
    return {"sub": context["token_info"]["sub"], "ua": request.headers["User-Agent"]}


# After
from foca.context import current_request
from foca.security.auth import current_claims


def get_whoami():
    return {
        "sub": current_claims().get("sub"),
        "ua": current_request().headers.get("User-Agent"),
    }
```

### Configuration

See [Configuration access](migration-0.22.md#configuration-access).
`set_app_config()` has no replacement: FOCA attaches the configuration to the
apps it creates.

```python
# Before
config = current_app.config.foca

# After
config = foca.get_config()
```

### Errors

Raise and map the classes of `foca.errors` (see [Framework-neutral
errors](migration-0.22.md#framework-neutral-errors)).

```python
# Before
from werkzeug.exceptions import NotFound

# After
from foca.errors import NotFound
```

### Responses

Return the body, and the status code and headers if needed, as a tuple;
Connexion serializes it as the specification says.

```python
# Before
return make_response(jsonify(pet), 201)

# After
return pet, 201

# Before
return send_file(path, mimetype="text/plain")

# After
return path.read_bytes(), 200, {"Content-Type": "text/plain"}
```

### Tests

Use `foca.testing` (see [Testing](testing.md) and [Test
client](migration-0.22.md#test-client)).

```python
# Before
client = app.app.test_client()
with app.app.test_request_context("/pets", headers=headers):
    ...

# After
client = Client(app)
with request_context("/pets", headers=headers, app=app):
    ...
```

### CORS

Configure CORS in `security.cors` of the configuration; FOCA enables it. Its
fields (`allow_origins`, `allow_methods`, `allow_headers`, `expose_headers`,
`allow_credentials`, `max_age`) already match Starlette's `CORSMiddleware`,
which replaces Flask-Cors in the ASGI release.

```python
# Before
enable_cors(app.app, CORSConfig(allow_origins=["https://example.org"]))
```

```yaml
# After, in the configuration file
security:
  cors:
    allow_origins:
      - https://example.org
```

### Celery

```python
# Before
celery_app = create_celery_app(app.app)

# After
celery_app = Foca(config_file="config.yaml").create_celery_app()
```

### Serving

In 0.22, `foca.gunicorn_config` runs Gunicorn with `gthread` workers and 4
threads each. In the ASGI release, it uses Uvicorn workers instead
(`uvicorn_worker.UvicornWorker`), and `threads` no longer applies. Nothing
needs to change before then if you use `foca.gunicorn_config` (see
[Deployment](deployment.md)); if your own config module sets `worker_class`
or `threads`, remove them when you upgrade.

### Deployment

In the ASGI release, the `<valuesKey>.gunicorn.threads` value of the
[Helm chart](helm-chart.md) goes away, as Uvicorn workers have no threads
setting. Nothing needs to change before then.

## Finding what still has to move

Run your tests with `-W error::DeprecationWarning` to find deprecated calls,
and adopt FOCA's ast-grep rule `flask-import`, which reports imports of Flask
and its extensions, Werkzeug, Connexion's Flask modules, and Connexion's
`request` and `context` (see [Finding Flask
imports](migration-0.22.md#finding-flask-imports)). The rule has the severity
`warning` in FOCA 0.22; set it to `error` in your copy when you move to the
ASGI release.

Werkzeug stays installed, as Connexion 3 depends on it, but apps must not
import it: its classes are not part of FOCA's API in the ASGI release.

## What doesn't change

- API specifications, and controllers that take their parameters and body as
  arguments and return tuples.
- The configuration file.
- Celery tasks.
