# Migrating to 0.14.0

FOCA 0.14.0 closes several security issues, removes options that had no
effect, and simplifies the configuration. Most apps need to change their
configuration; configs with removed options fail validation with a clear
error, so start the app after each step to see what is left.

## Requirements

- Python 3.10 or later.
- Flask 3, Werkzeug 3, pydantic 2 and Connexion 2.15 (Connexion 3 is not
  supported yet). Custom configuration models must use the
  [pydantic 2 API](https://docs.pydantic.dev/latest/migration/).
- Flask-PyMongo and flask-authz are no longer installed. Install them
  yourself if your app uses them directly.
- MongoDB 4.4 or later: current PyMongo releases refuse to connect to older
  servers (e.g., MongoDB 3.6). FOCA is tested with MongoDB 7.0 and 8.0. Note
  that MongoDB 8.0 refuses to start on Linux kernels 6.19 to 7.0.13
  ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)); use
  MongoDB 7.0 on such hosts.
  Upgrade existing databases one major version at a time.

## Authentication

Bearer tokens are only accepted from trusted issuers, and their signatures
are always verified locally ([details](authentication.md)). List the issuers,
and set an audience:

=== "0.13"

    ```yaml
    security:
      auth:
        required: True
        validation_methods: [userinfo, public_key]
        validation_checks: all
    ```

=== "0.14"

    ```yaml
    security:
      auth:
        issuers:
          - https://login.elixir-czech.org/oidc/
        audience:
          - my-app
        validate_userinfo: false
    ```

- Without `issuers`, **all tokens are rejected**.
- With `issuers`, **`audience` is required**: tokens must be issued for
  the app. Set `verify_audience: false` to accept tokens for any audience
  (not recommended).
- `validation_methods`, `validation_checks`, `claim_issuer` and `required`
  are removed. `validate_userinfo: true` additionally validates tokens via
  the issuer's user info endpoint.
- Tokens must contain the claims `exp`, `iat`, `iss` and `claim_identity`.
- Token claims are no longer copied into request headers. Use the
  `token_info` argument of controllers, or `get_current_subject()` and
  `get_current_groups()` from `foca.security.access_control.enforcement`.

## Access control

Access control is now opt-in and requires trusted token issuers:

=== "0.13"

    ```yaml
    security:
      auth:
        required: True
      access_control:
        api_specs: api/permissions.yaml
        api_controllers: my_app/permissions.py
        db_name: access_control_db
        collection_name: policy_rules
        model: policies.conf
        owner_headers: [admin]
        user_headers: [user_id]
    ```

=== "0.14"

    ```yaml
    security:
      auth:
        issuers:
          - https://login.elixir-czech.org/oidc/
        audience:
          - my-app
      access_control:
        enabled: true
        enforce: true
        api_specs: api/permissions.yaml
        db_name: access_control_db
        collection_name: policy_rules
        model: policies.conf
    ```

- Set `enabled: true` if your app uses the permissions API or enforces
  policies. Apps without it no longer get an access control database or
  permissions API.
- `api_controllers`, `owner_headers` and `user_headers` are removed.
  Controllers of custom permissions API specifications are resolved via
  their `operationId`s.
- The `check_permissions` decorator is removed. Enforce policies on all
  operations requiring authentication with `enforce: true`, or on single
  controllers with the `enforce_access_control` decorator; subjects are
  taken from validated tokens ([details](access-control.md)).
- The reference model (`resource_role_model.conf`) anchors action patterns,
  i.e., the `act` values of policy rules must match the whole HTTP method:
  `P` no longer matches `PUT`, `POST` and `PATCH`; use `PUT|POST|PATCH` or
  `P.*` instead.
- `GET /permissions` returns `{"results": [...], "pagination": {...}}`
  instead of a list, and takes `page_size` and `page` instead of `limit`.
- The permissions API rejects rules with unknown policy types, extra keys,
  a wrong number of values or invalid values (400). `PUT /permissions/{id}`
  rejects identifiers that do not match `id_charset` and `id_length`
  (default: 6 letters or digits).
- Rule identifiers are unique (enforced with an index). If stored rules
  share an identifier, writing rules fails until the duplicates are
  removed.
- The Swagger UI and specification of the permissions API are only served
  with `api_swagger_ui: true` and `api_serve_spec: true`.
- `CasbinRule.dict()` is renamed to `to_dict()`, `Adapter` takes a pymongo
  collection (`Adapter(collection=...)`), and `Adapter.save_policy()`
  replaces all stored rules.

## Exceptions

Errors are returned as [problem details](configuration/exceptions.md):

=== "0.13"

    ```yaml
    exceptions:
      required_members: [["message"], ["code"]]
      status_member: ["code"]
      exceptions: my_app.exceptions.exceptions
    ```

    ```python
    exceptions = {
        NotFound: {"message": "We have never heard of this pet!", "code": 404},
    }
    ```

=== "0.14"

    ```yaml
    exceptions:
      mapping: my_app.exceptions.problems
    ```

    ```python
    problems = {
        NotFound: {
            "title": "Not Found",
            "status": 404,
            "detail": "We have never heard of this pet!",
        },
    }
    ```

- `exceptions` is renamed to `mapping`. Each entry needs an integer
  `status` and a string `title`.
- `required_members`, `extension_members`, `status_member` and
  `private_members` are removed, and `public_members` is a flat list of
  member names.
- Exceptions not listed in the mapping get the status of their HTTP
  exception class, so the mapping only needs to list exceptions with custom
  problem details. Subclasses of listed exceptions get the problem details
  of their closest listed base class (previously 500).
- `ServiceUnavailable` returns 503 (previously 502).
- `foca.errors.exceptions` no longer provides a default `exceptions`
  dictionary or re-exports werkzeug exceptions; import these from
  `werkzeug.exceptions`.
- Update the error responses in your API specifications to
  `application/problem+json`, and clients that read `message` or `code`.

## Server

- `server.debug` is `false` by default. Set it explicitly for local
  development only.
- `server.environment` and `server.use_reloader` are removed.

## API specifications

- Relative paths in configuration files (`api.specs[].path`,
  `security.access_control.api_specs` and `model`) are resolved against the
  directory of the configuration file, not the working directory. For
  example, if `config.yaml` and `api/openapi.yaml` are in the same
  directory, use `path: api/openapi.yaml`, wherever the app is started.
- `api.specs[].path_out` is removed (modified specifications were never
  written).
- `add_operation_fields` only applies to operations, not to other members of
  path items (e.g., `parameters`).

## Databases

- All databases share one MongoDB client per app. `create_mongo_client(host,
  port, db=None)` is public and no longer takes a Flask app; select
  databases with `client[get_mongo_db_name(db)]`.
- `register_mongodb()` no longer takes `app`, and `add_new_database()` is
  removed.
- Configured indexes are only created, no longer dropped and recreated on
  startup. Conflicting existing indexes need to be dropped once.
- `MONGO_URI` is supported, and with `MONGO_USERNAME`/`MONGO_PASSWORD`, the
  client authenticates against the database in `MONGO_DBNAME`, if set
  ([details](configuration/db.md#environment-variables)).
- `PymongoDirectionEnum` is removed.
- Registered collections can be accessed with `foca.get_collection()`, also
  outside of request contexts (e.g., in Celery tasks).

## Background jobs

`CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` take precedence over
`jobs.host`, `jobs.port` and `jobs.backend` if set.

## Logging

`log_traffic` no longer logs response bodies; set `log_response_body=True`
to keep logging them. The configuration is no longer logged, only the names
of its sections.

## Utilities

`generate_id()` no longer evaluates `charset` expressions such as
`string.ascii_uppercase + string.digits`; pass the characters themselves.

The module `foca.version` is removed; use `foca.__version__` instead.

## Docker image

Images built `FROM` the FOCA image run as non-root user `foca` (UID 1000,
group 0). Dockerfiles that install packages or write to root-owned paths
need to switch users around these steps:

```dockerfile
FROM elixircloud/foca:latest

USER root
RUN pip install --no-cache-dir .
USER 1000
```

The image no longer contains build tools or utilities that apps may have
relied on (e.g., `gcc`, `git`, `curl`, `jq`). Install utilities that your
app needs at runtime in your own image, as `root`:

```dockerfile
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl jq \
    && rm -rf /var/lib/apt/lists/*
USER 1000
```

Tools that are only needed to build your app (e.g., compilers) are better
installed in a separate build stage that is not part of the final image.

Release images are tagged with the FOCA version (e.g., `0.14.0`, or
`0.14.0-py3.12` for other Python versions), and `latest` only refers to the
latest release; images of the `dev` branch keep their dated tags. To build
the image yourself, pass the FOCA version with `--build-arg
FOCA_VERSION=<version>`.
