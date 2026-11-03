# Migrating to 0.20.0

FOCA 0.20.0 removes pip from the FOCA image, changes how gateway plugins
key some of their stored data and lets operators key the plugin store's
hashes with a secret.
Apps whose images install packages on top of the FOCA image (e.g., with
`pip install` in their `Dockerfile`) need to install them with uv instead
(first breaking change below). Gateway apps that record failed runs
(`plugins.provenance.record_failed`) and read these records by their `_id`
need the second breaking change, and gateway apps that keep the plugin
store in a database the secret for its keys (under other changes).
Upgrading from 0.17 or earlier? Start with [Migrating to
0.18.0](migration-0.18.md).

## Breaking changes

### No pip in the FOCA image

The FOCA image (`elixircloud/foca`) no longer contains pip: pip vendors
copies of other packages (urllib3, msgpack and setuptools), which image
scans report in every image built on FOCA, although only pip uses them, and
only at build time; apps had to accept them in their `.trivyignore.yaml`.
`pip install` in a `Dockerfile` based on the FOCA image now fails (`pip: not
found`).

Install packages with [uv](https://docs.astral.sh/uv/) instead, from a stage
pinned by digest, without copying uv into the image (see [installing Python
packages](getting-started.md#installing-python-packages)):

```dockerfile title="Dockerfile"
FROM ghcr.io/astral-sh/uv:<version>@sha256:<digest> AS uv

FROM elixircloud/foca:0.20.0-py3.13@sha256:<digest>
USER root
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    UV_NO_CACHE=1 uv pip install --system --require-hashes --only-binary :all: \
        --no-deps --compile-bytecode -r /tmp/requirements.txt \
    && uv pip check --system
USER 1000
```

1. Add the uv stage (Dependabot's `docker` ecosystem updates its tag and
   digest together).
2. Replace each `pip install` of the final stage with `uv pip install
   --system`, as above; `requirements.txt` lists all packages that the image
   adds to the FOCA image, with their hashes (e.g., `uv export` from a lock
   file, without FOCA and its dependencies). Install the app's own wheel
   with `--no-index --no-deps` and without `--require-hashes` (see
   [installing Python
   packages](getting-started.md#installing-python-packages)). Build stages may keep pip (e.g., in a virtual environment
   created with `python -m venv`).
3. Remove the entries for packages vendored by pip from `.trivyignore.yaml`
   (in FOCA 0.19.0: setuptools 70.3.0, urllib3 2.7.0 and msgpack 1.1.2).
4. Images that add plugins to an app's image install them the same way.

Packages installed with uv record `uv` as their installer, and their
`RECORD` files differ from pip's: the `RECORD` digests in [plugin
provenance](plugins.md) change once, although the plugin versions don't.

### Failure records keyed by run ID

Records of failed pipeline runs (`ProvenanceStore.save_failure()`, only
with `plugins.provenance.record_failed: true`) were keyed (`_id`) by the
client-supplied `X-Request-ID`, so a client could reuse an ID to overwrite
an earlier record or to collide with the records of other clients. They
are now keyed by `run-` and the run's server-generated random ID; the
request ID stays in the record as the field `request_id`, for correlation.

Nothing needs to be changed in the app itself. Code, queries or
dashboards that look up failure records by `_id` and a request ID need to
query the field `request_id` instead (add an index on it if you query
often). Records written by FOCA 0.19.0 keep their old keys until they are
deleted (e.g., by the TTL index of the collection, cf.
`foca.plugins.provenance.FAILURE_INDEXES`; without one, never), so while
they exist, lookups by request ID must cover both: `_id` (old records) and
`request_id` (all records).

## Other changes

### Errors of `GuardedClient`

`foca.utils.http.GuardedClient` now decides by whether sending a request has
started if an error is `RequestNotSent` or `RequestMaybeSent`, rather than by
the kind of error. Apps that catch these classes need no change, but may see
other errors than before:

- A TLS error while waiting for or reading a response: `RequestMaybeSent`
  (`connection_lost`), previously `RequestNotSent` (`tls_error`). Don't retry
  such requests elsewhere.
- A connection lost or a timeout during the TLS handshake: `RequestNotSent`
  (`connect_error`), previously `RequestMaybeSent`.
- A deadline that passes before sending: `DeadlineExceeded`.
- A redirect with a body above `max_response_size`: `ResponseTooLarge`, without
  reading the whole body first.
- A redirect to an invalid URL: returned like others, previously
  `InvalidRequestURL`.
- A redirect whose body can't be decoded: `RequestMaybeSent`, as for other
  responses, previously returned without its body.
- `auth` with response hooks (e.g., `HTTPDigestAuth`): `InvalidRequest`, as
  they read bodies without limit. Use a `(user, password)` tuple or a handler
  that only sets headers.

### Secret for the keys of the plugin store

`ctx.store.key_for()` hashes parts that may be personal data (e.g., a
user's identifier) with plain SHA-256: anyone with access to the database
or a backup who knows an identifier can compute the key and find that
user's entries. The new setting `plugins.defaults.store.key_secret` (or,
if it is not set, the environment variable
`FOCA_PLUGINS_STORE_KEY_SECRET`) keys the hashes (HMAC-SHA256, prefix
`hk:` instead of `h:`). See [plugin
store](configuration/plugins.md#plugin-store) for how to set it, and how
to rotate it.

- Without the secret, nothing changes, except that gateway apps whose
  plugins declare stored data (`data_stored`) and keep their store entries
  in the database log a warning at startup.
- **Setting the secret makes existing entries that plugins keyed with
  `key_for()` unreachable** (e.g., per-user budgets start anew), as their
  keys change; they expire after at most `max_ttl` (default: 30 days). The
  same holds when you change or remove the secret later.
- All processes and replicas of the app must have the same secret; set it
  from a secret store (e.g., a Kubernetes Secret as environment variable),
  never in a committed configuration file. Roll all replicas at once or
  accept that, during the update, processes with the old and the new secret
  don't see each other's entries.
- Validation errors of the configuration no longer show the invalid input
  (`input_value`), as it may contain the secret; the error still names the
  setting and the reason.
- **Helm charts** based on foca-common 0.4.0 or later pass the secret from
  an existing Secret (key `store-key-secret`) to the app and the worker:
  set `plugins.existingSecret` to its name (see [deployment
  notes](helm-chart.md#deployment-notes)). App charts update their
  dependency on foca-common to 0.4.0, copy `app-values.schema.json` again
  and add `plugins.existingSecret: ""` to their values. Rendering fails if
  the config overlay sets `config.plugins.defaults.store.key_secret`, as
  it is a ConfigMap.
