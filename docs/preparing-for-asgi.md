# Preparing for the ASGI release

The first release candidate of FOCA's ASGI release moves to Connexion 3
`AsyncApp` and removes Flask and Flask-Cors. FOCA 0.22 is the preparation
release: it removes nothing, warns about what it can, and adds replacements
that already work on Connexion 2. FOCA's functions that take a Flask app warn
when called; Flask's own objects (e.g., `flask.request` or `make_response()`)
can't warn, so FOCA's ast-grep rule reports their imports instead (see
[Finding what still has to move](#finding-what-still-has-to-move)). Move your
app to the replacements now, on FOCA 0.22, so that the upgrade to the ASGI
release is mostly a version bump.

When work on the ASGI release starts, FOCA cuts a `0.22.x` branch from the
latest 0.22 release, for dependency and security fixes while apps migrate.

## Removed in the ASGI release, and what to use now

| Removed in the ASGI release | Use now (FOCA 0.22) |
| --- | --- |
| [Flask app object](#flask-app-object): `app.app` | What you used it for: `get_config()`, `foca.testing`, FOCA's configuration |
| [Request objects](#request-objects): `flask.request`, `g`, `current_app`, `connexion.request`, `connexion.context`, `request_ctx` | Arguments of controllers, `foca.context.current_request()`, `current_token_info()` and `current_claims()` of `foca.security.auth` |
| [Configuration](#configuration): `current_app.config.foca`, `get_app_config()`, `set_app_config()` | `foca.get_config()`, `foca.get_custom_config()`; for `set_app_config()`, see [Configuration](#configuration) |
| [Errors](#errors): exceptions of Werkzeug and Connexion, also in exception maps, and `www_authenticate` of `foca.errors.Unauthorized` | `foca.errors`; `WWW-Authenticate` headers |
| [Responses](#responses): `flask.Response`, `make_response()`, `send_file()`, `jsonify()` | `body`, `(body, status)` or `(body, status, headers)` tuples |
| [Tests](#tests): Flask's test client, `app_context()`, `test_request_context()` | `foca.testing.Client`, `foca.testing.request_context`, the `foca_client` fixture |
| [CORS](#cors): Flask-Cors, `enable_cors(flask_app)` | `security.cors` of the configuration (`CORSConfig` stays) |
| [Celery](#celery): `foca.factories.celery_app.create_celery_app(flask_app)` | `Foca.create_celery_app()` |
| [Serving](#serving): Gunicorn's `gthread` worker class and `threads` | `foca.gunicorn_config`, without setting `worker_class` or `threads` yourself; no change needed before the ASGI release |
| [Deployment](#deployment): the `gunicorn.threads` value of the Helm chart | Keep it until the ASGI release; no change needed before |

Of these, the functions that take a Flask app (`get_app_config()`,
`set_app_config()`, `enable_cors()` and
`foca.factories.celery_app.create_celery_app()`), the `app.config.foca`
attribute and the `www_authenticate` attribute of `foca.errors.Unauthorized`
emit a `DeprecationWarning`, and FOCA logs each deprecation once per process (see [Deprecations in the
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
from foca import Foca
from foca.config.access import get_app_config

app = Foca(config_file="config.yaml").create_app()
client = app.app.test_client()
config = get_app_config(app.app)

# After
from foca import Foca, get_config
from foca.testing import Client

app = Foca(config_file="config.yaml").create_app()
client = Client(app)
config = get_config()
```

`get_config()` returns the configuration of the current app or, outside app
contexts, of the most recently created FOCA app; with several apps, e.g., in
tests, see [Configuration access](migration-0.22.md#configuration-access).

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
    return {
        "sub": context["token_info"]["claims"]["sub"],
        "ua": request.headers["User-Agent"],
    }


# After
from foca.context import current_request
from foca.security.auth import current_claims


def get_whoami():
    return {
        "sub": current_claims().get("sub"),  # None without a token
        "ua": current_request().headers.get("User-Agent"),
    }
```

### Configuration

See [Configuration access](migration-0.22.md#configuration-access).

```python
# Before
from flask import current_app

config = current_app.config.foca

# After
from foca import get_config

config = get_config()
```

`set_app_config()` isn't needed for apps that FOCA creates
(`Foca.create_app()`), which get their configuration attached. Scripts that
attach a configuration to a Flask app of their own, e.g., to use
`get_config()` and `get_collection()` without creating the app, call
`Foca.use_config()` instead, which needs neither Flask nor an app context:

```python
# Before
from flask import Flask
from foca import Foca
from foca.config.access import set_app_config
from foca.database.register_mongodb import register_mongodb

foca = Foca(config_file="config.yaml")
app = Flask(__name__)
set_app_config(app, foca.conf)
register_mongodb(foca.conf.db, create_indexes=False)
with app.app_context():
    run()

# After
from foca import Foca

foca = Foca(config_file="config.yaml")
foca.use_config()
run()
```

`use_config()` registers the databases (with the collection of the stored
policy rules if access control is enabled) without creating indexes and runs
nothing else of the startup of apps, so it writes nothing to the database
(see [Configuration access](migration-0.22.md#configuration-access)). There
is no app context, so what only apps register isn't available (e.g.,
`current_app.config["casbin_adapter"]`): scripts that need it create the app,
after `Foca.disable_startup_writes()` if they must not write.

### Errors

Raise and map the classes of `foca.errors` (see [Framework-neutral
errors](migration-0.22.md#framework-neutral-errors)).

```python
# Before
from werkzeug.exceptions import NotFound

# After
from foca.errors import NotFound
```

Exception maps with raw Werkzeug classes are logged as deprecated in 0.22 and
stop working with the ASGI release. `www_authenticate` of
`foca.errors.Unauthorized` is deprecated: pass challenges as `WWW-Authenticate`
headers (see [Challenges in `headers`](migration-0.22.md#challenges-in-headers)).

### Responses

Return the body, and the status code and headers if needed, as a tuple;
Connexion serializes it as the specification says.

```python
# Before
from flask import jsonify, make_response, send_file

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
from foca.testing import Client, request_context

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
from foca.models.config import CORSConfig
from foca.security.cors import enable_cors

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
from foca.factories.celery_app import create_celery_app

celery_app = create_celery_app(app.app)

# After
from foca import Foca

# With the same arguments as for the app, e.g., `custom_config_model`
celery_app = Foca(
    config_file="config.yaml", custom_config_model="my_app.config.CustomConfig"
).create_celery_app()
```

### Serving

In 0.22, [`foca.gunicorn_config`](deployment.md#running-in-production) runs
Gunicorn with `gthread` workers and 4 threads each. ASGI apps can't run in
these: the ASGI release is planned to switch `foca.gunicorn_config` to Uvicorn
workers, where `threads` no longer applies. Keep using `foca.gunicorn_config`;
if your own config module sets `worker_class` or `threads`, remove them when
you upgrade.

```python title="my_app/gunicorn_config.py"
# Before
from foca.gunicorn_config import *  # noqa: F403

worker_class = "gthread"
threads = 8
timeout = 60

# After (works now, and with the ASGI release)
from foca.gunicorn_config import *  # noqa: F403

timeout = 60
```

### Deployment

With Uvicorn workers, which have no threads setting, the
`<valuesKey>.gunicorn.threads` value of the [Helm chart](helm-chart.md) is
planned to go away in the ASGI release. Keep it until then, and remove it
when you upgrade:

```yaml title="values.yaml"
# FOCA 0.22 (`myApp` is the chart's `valuesKey`)
myApp:
  gunicorn:
    workers: 1
    threads: 4

# ASGI release (planned)
myApp:
  gunicorn:
    workers: 1
```

## Finding what still has to move

Two checks together find what still has to move:

- **Deprecation warnings** of FOCA's functions, attributes and exception maps,
  which FOCA also logs once per process on the logger `foca.deprecation`. To
  make them fail your tests, turn deprecation warnings into errors, but keep
  ignoring those of Connexion 2's own use of jsonschema, as FOCA does:

    ```toml title="pyproject.toml"
    [tool.pytest.ini_options]
    filterwarnings = [
        "error::DeprecationWarning",
        "ignore:jsonschema\\.(RefResolver|exceptions\\.RefResolutionError) is deprecated:DeprecationWarning",
        "ignore:Accessing jsonschema\\.draft4_format_checker is deprecated:DeprecationWarning",
        "ignore:Passing a schema to Validator\\.iter_errors is deprecated:DeprecationWarning",
    ]
    ```

- **FOCA's ast-grep rule `flask-import`**, which reports imports of Flask and
  its extensions, Werkzeug, Connexion's Flask modules, and Connexion's
  `request` and `context`, i.e., uses of Flask's own objects, which can't warn
  (see [Finding Flask imports](migration-0.22.md#finding-flask-imports)).
  The rule has the severity `warning` in FOCA 0.22; set it to `error` in your
  copy when you move to the ASGI release.

Neither finds everything: `app.config.foca` warns only on Flask's own
configuration class (not on a `config_class` of your app), and the rule
doesn't report `connexion.App` or attributes of an aliased `connexion`
module. Search for `config.foca` and `connexion.App` as well.

Werkzeug stays installed, as Connexion 3 depends on it, but apps must not
import it: its classes are not part of FOCA's API in the ASGI release.

## What doesn't change

- API specifications, and controllers that take their parameters and body as
  arguments and return tuples.
- The configuration file.
- Celery tasks.
