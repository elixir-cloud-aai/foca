# Gateway plugins

Gateway apps such as proTES (TES) and, later, proWES (WES) accept requests
from clients and forward them to backends. FOCA's plugin engine
(`foca.plugins`) lets operators process these requests with installed
plugins: change tasks (e.g., add inputs or executors), reject them (e.g., by
policy), choose and order the backends, and adapt each forwarded request.
The same engine and the same plugins serve all gateway apps.

Terms:

- **plugin**: an installable, versioned unit, a Python class registered
  under an entry point of the group `foca.plugins`;
- **pipeline**: the ordered steps that a request goes through;
- **step**: one position of a pipeline, with one or more ordered
  **alternatives** (a plugin with parameters); alternatives 2 to *n* are
  fallbacks.

Operators configure plugins and pipelines in the [`plugins`
section](configuration/plugins.md) only: there is no API to change them at
runtime (change the configuration and restart instead).

For gateway app authors, in short (cf. [using the engine in
apps](#using-the-engine-in-apps)): pass `Foca(..., gateway=GatewayApp(api=...,
validate=..., sanitize=...))`, so that `create_app()` checks the plugins on
startup; get the host of the process with `foca.plugins.runtime.get_host()`
in controllers and Celery tasks; and remove keys of earlier middleware
stacks (`middlewares`, `custom.middlewares`), which are refused with a
migration hint. Apps that are no gateways ignore the `plugins` section (with
a warning).

## Phases

A gateway request passes four phases. A plugin implements the hooks of any
of them; each phase visits the steps whose plugins implement its hook.

| Phase | Hook | Runs | The hook |
|---|---|---|---|
| `request` | `on_request(request, ctx)` | once | changes the inbound request (`InboundRequest`) in place |
| `route` | `on_route(candidates, ctx)` | once | returns a subset of the candidate backends (`Backend`), in order |
| `forward` | `on_forward(outbound, ctx)` | per attempt | changes the outbound request (`OutboundRequest`) in place |
| `response` | `on_response(response, ctx)` | per attempt | changes the response to the client (`BackendResponse`) in place, in reverse step order |

Plugins run after authentication, validation, access control and the app's
own sanitation: the `request` phase sees a validated request of an
authenticated caller, and the app checks the payload again after each
successful hook, so plugins cannot let through what clients may not send.

## Failure semantics

| Outcome | Raised by | Engine |
|---|---|---|
| success | the hook returns in time, with valid output | keeps the hook's copy; next step |
| not applicable | `NotApplicable` | next alternative |
| error | `PluginError` or any other exception | next alternative; logged |
| timeout | the deadline passed | next alternative |
| invalid output | the app's check fails, or `on_route` returns other backends | next alternative |
| rejected | `Rejected(detail, status)` | stops at once; the client gets the status (400, 403, 409, 413 or 422); in the `response` phase, the backend has accepted the request already, so a rejection counts as an error |

Each alternative works on a **copy** of its input, which the engine keeps
only if the alternative succeeds; a failing alternative leaves no trace.
When all alternatives of a step fail, the request fails with `500`, unless
the step has `on_failure: skip`. In the `forward` phase, the attempt fails
instead and the next backend is tried; in the `response` phase, the step is
skipped, as the backend accepted the request already.

Hooks run in a bounded thread pool (`plugins.defaults.workers`) and are
abandoned when their deadline passes (Python cannot stop threads); as they
work on copies, abandoned hooks cannot change the request, but side effects
they cause later are their own. When all workers are busy, further calls
fail at once, as timed out.

## Per-request pipelines and backends

Clients choose the pipeline and the backends of a request in two reserved
tags of the request (TES and WES `tags`, a string map: spec-compliant and
supported by existing clients), under the prefix `plugins.tags.prefix`
(default `gateway.`):

- `gateway.pipeline`: the name of a preset (`plugins.presets`), or a
  pipeline as JSON, e.g.,
  `[{"plugin": "org.example.tes.data.decrypt"}, {"id": "route", "alternatives": [{"plugin": "org.example.tes.routing.distance", "params": {"max_km": 2000}}]}]`;
  steps of clients cannot set `timeout`, `on_failure` or
  `if_missing_phase`;
- `gateway.backends`: a JSON object (at most 8 KiB)
  `{"type": "order" | "allow" | "forbid", "backends": [...]}` with one or
  more backend identifiers or URLs (items with `://` match URLs only,
  others identifiers only):
  - `order`: the listed backends first, in the client's order, then the
    other candidates in the server's order;
  - `allow`: only the listed backends, in the server's order;
  - `forbid`: all candidates but the listed ones, in the server's order.

  The selection applies to the candidates after the `route` phase: clients
  can only narrow or reorder the configured and verified backends; listed
  backends that are not candidates are discarded (and recorded, cf.
  [provenance](#provenance)), never added.

!!! warning "A client pipeline replaces the default pipeline"

    A preset or client pipeline (even an empty one, `[]`) runs **instead
    of** `plugins.pipeline`: only the frame (`client_pipelines.prepend` and
    `append`) runs for every request. Put policies that must not be skipped
    into `prepend`.

Before any plugin runs, the engine checks the choice and refuses it with
`400` if the tags are no map or invalid, if per-request choices are disabled
(`plugins.client_pipelines.enabled`, `plugins.client_backends.enabled`),
if the pipeline exceeds the limits (`max_steps`, `max_alternatives`,
`max_size`), if the caller lacks a required role, or if it uses a plugin
that is not available to the caller (unknown, not installed, disabled, not
`client_selectable`, or restricted by `roles`; all with the same message,
so that clients cannot probe the setup). Presets may use plugins that are
not `client_selectable`, but need the plugins' `roles` too (unknown presets
and presets that need a role get the same message). Apps of other APIs than
TES pass the request's tags explicitly (`host.start(..., tags=...)`, e.g.,
the parsed `tags` form field of WES). Presets and client pipelines are
framed by the admin's steps (`client_pipelines.prepend` and `append`),
which clients can neither remove nor reorder; appended steps with
`if_missing_phase` (e.g., a default `route` step) are added only if no other
step implements that phase. Requests without the pipeline tag get the
default pipeline (`plugins.pipeline`), without frame.

All other tags under the prefix are written by the gateway only: clients
setting them get `400` (`reserved-tag`), so that dashboards can trust them.
Clients that re-submit a fetched task drop them first, except the two
above.

## Forwarding

The app tries the candidates in order. For each, it reports the outcome of
the attempt:

- **not accepted** (e.g., connection refused, `4xx`): the next backend is
  tried;
- **outcome unknown** (e.g., read timeout, `5xx`, as proxies answer `502`
  to `504` also when the backend got the request): no other backend is
  tried, so that the request is not executed twice;
- **accepted**: the `response` phase runs.

All attempts of a request together may take `plugins.defaults.forward_timeout`
seconds (`503` when exceeded); no backend accepting gives `503` too.

Outbound requests carry a `Via` header: the inbound request's `Via` header,
passed through verbatim, plus the gateway's name (`plugins.gateway_id`, by
default the host and port of `server.public_url`). Inbound requests whose
`Via` header already names this gateway are refused (`508 Loop Detected`),
as are `Via` headers that would exceed 1024 characters with the gateway's
entry (`400`). Backends at or below the gateway's own public URL are
never candidates. Credentials of backends are added by the app after the
`forward` phase, so plugins never see them; the client's own credentials
are never forwarded by the engine.

## Writing plugins

```python
from pydantic import BaseModel
from foca.plugins import Context, InboundRequest, NotApplicable, Plugin, Rejected


class Config(BaseModel):  # admin settings (`plugins.available.<id>.config`)
    registries: list[str]


class Params(BaseModel):  # parameters in a pipeline step
    strict: bool = True


class AllowedImages(Plugin[Config, Params]):
    """Allow executor images of listed registries only."""

    Config = Config
    Params = Params
    apis = frozenset({"tes"})  # or {"*"}
    maturity = "stable"  # experimental | stable | deprecated

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        executors = request.payload.get("executors", [])
        if not executors:
            raise NotApplicable("no executors")
        for executor in executors:
            registry = executor["image"].partition("/")[0]
            if registry not in self.config.registries:
                raise Rejected(f"Image not allowed: {executor['image']}", 403)
```

Register the class in the plugin's `pyproject.toml`:

```toml
[project.entry-points."foca.plugins"]
"org.example.tes.policy.images" = "example_plugins.images:AllowedImages"
```

Rules:

- Plugins get plain data (`foca.plugins.model`) and an explicit context
  (`Context`): the caller (`ctx.principal`, with claims and token), the
  validated parameters (`ctx.params`), the payload before any plugin
  (`ctx.original`), the deadline (`ctx.deadline`, `ctx.remaining()`), a
  per-request state (`ctx.state`), a logger (`ctx.log`) and annotations for
  records (`ctx.record()`, no personal data). Don't import the web framework
  (Flask, Werkzeug, Connexion, Starlette): plugins then run unchanged on
  FOCA 1.0.
- One instance serves all requests of a process, from several threads:
  keep per-request data in `ctx.state`, not on `self`.
- `setup()` may run more than once per process, so it must be idempotent.
- Identifiers of plugins outside FOCA's own repositories start with a
  prefix of their own (e.g., `org.example.`).
- Hooks are plain functions (`def`); FOCA 1.0 will also accept `async def`
  with the same arguments.

## Using the engine in apps

Gateway apps declare themselves when creating the FOCA app, with their API
and their payload checks:

```python
from foca import Foca
from foca.plugins.runtime import GatewayApp

foca = Foca(
    config_file="config.yaml",
    custom_config_model="my_gateway.config.CustomConfig",
    gateway=GatewayApp(
        api="tes",
        name="my-gateway",  # name and version of the app, for provenance
        version="1.2.0",
        validate=check_task,  # raises if a task is invalid
        sanitize=sanitize_task,  # returns the sanitized task
        validate_outbound=check_relay,  # optional, per attempt
    ),
)
app = foca.create_app()  # loads the plugins once, to fail fast
```

- `validate` runs after each successful `request` hook: if it raises, the
  hook's output counts as invalid, and the next alternative is tried.
- `sanitize` runs before `validate`: its exceptions (e.g., `413` for too
  much inline content) stop the request and reach the app unchanged.
- `validate_outbound` checks each attempt's outbound request after the
  `forward` phase (e.g., that the app can serialize it): if it raises, the
  attempt counts as not accepted (`invalid_outbound`).
- These callbacks (and the `build` function of `run.attempts()`) are
  trusted app code: they run in the request's thread, outside the plugin
  deadlines, so they must be fast and must not do I/O.
- A process serves one gateway app: `Foca(...)` with other gateway settings
  fails while the host of the first app is in use
  (`foca.plugins.runtime.reset()` closes it, e.g., in tests). The host is
  closed at exit.
- Configurations with keys of earlier middleware stacks (`middlewares`,
  `custom.middlewares`) are refused with a migration hint, before the app's
  custom model is validated.

Controllers and Celery tasks get the host of their process with
`foca.plugins.runtime.get_host()`; each process creates its own host (and
thread pool) on first use, after Gunicorn or Celery forked it. In the
controller, after the app's own checks:

```python
from foca.plugins import BackendResponse, NotAccepted
from foca.plugins.adapter import current_principal, inbound_from_request, request_id
from foca.plugins.runtime import get_host

run = get_host().start(
    inbound_from_request(body), current_principal(), request_id=request_id()
)
run.request()
candidates = run.route(backends)
for attempt in run.attempts(candidates):
    try:
        remote_id = send(attempt.outbound, timeout=attempt.remaining())
    except NotAccepted as outcome:  # also OutcomeUnknown, its subclass
        attempt.report(outcome)
        continue
    attempt.succeeded(BackendResponse(status=200), remote_id=remote_id)
    break
if run.final_reason == "outcome_unknown":
    ...  # a backend may have the task: mark it as such, don't retry
```

The app's client raises `NotAccepted(reason)` when a backend certainly did
not accept the request (e.g., `connect_error`, `tls_error`, `not_public`,
`no_credentials`, `http_403`), and `OutcomeUnknown(reason)` when it may have
(e.g., `read_timeout`, `connection_lost`, `http_502`); the loop then stops
after `OutcomeUnknown` without raising, with `run.final_reason` set to
`outcome_unknown`.

`foca.plugins.adapter` is the only module that uses Connexion and Flask: it
builds the plain request model (`inbound_from_request()`), the caller
(`current_principal()`, with the claims and mapped roles that access
control uses, so `roles` in the plugin configuration are access control
roles), the request ID (`request_id()`: a safe `X-Request-ID` header of
the client, or a new one), and problem responses for gateway errors
(`problem()`).

## Provenance

Each run records what was requested and resolved (prospective) and what
actually ran (retrospective). `run.provenance()` returns it as plain JSON
data for the app to store with its resource (e.g., the task):

- `engine`: versions of FOCA, the plugin API and the app (`app`: name and
  version from `GatewayApp(name=..., version=...)`, the host's `app_info`);
- `pipeline`: `source` (`default`, `preset` or `client`), `preset`,
  `requested` (a reference to the client's tag, e.g.,
  `tags[gateway.pipeline]`, as the tag is stored with the task anyway),
  `resolved` (steps with all parameters) and `resolved_hash`
  (`sha256:` of its RFC 8785 canonical JSON);
- `plugins`: the plugins that ran, by build key (a hash of identifier,
  distribution, version, entry point, RECORD digest and image digest);
- `executions`: per hook call, in order: step, phase, alternative, plugin,
  outcome, start time, duration, attempt number, error (the type only,
  as messages of plugins may hold secrets; logs get the message; FOCA's
  gateway errors and rejections keep theirs), annotations
  (`ctx.record()`) and the changes: keyed hashes before and after
  (`hmac-sha256:`, with a random key per process, so that they can be
  compared within a record, but not guessed offline) and the changed JSON
  Pointers, without values (at most 256, then `truncated: true`; arrays
  whose length changed as a whole); fields that do not apply are left
  out;
- `route` (candidates in and out), the client's backend `selection` (with
  `discarded` items), the forwarding `attempts` (backend, outcome, reason,
  remote ID, timings) and the `final_reason` (`forward_timeout`,
  `no_backend`, `outcome_unknown`) if forwarding failed. Backend URLs
  appear without user info, query and fragment.

Records hold no IP addresses, tokens, header values or claims, and hashes
leave out credentials, client addresses (`Forwarded`, `X-Forwarded-For`,
`X-Real-IP`), `Set-Cookie` of responses and
`plugins.provenance.secret_headers`. Hashes of values with
little entropy (e.g., short API keys, user names) can be guessed, so put
such headers in `secret_headers`, and declare secret plugin parameters as
`pydantic.SecretStr`, which records show as `**********`. Changed paths
are capped in number and length per call, and in size per run. Changed
paths name the keys of maps (e.g., of `env` or `tags`), so keys should
hold no personal data. Records take at most 1 MiB of JSON
(`foca.plugins.provenance.MAX_RECORD_SIZE`): larger ones lose, in this
order, the changed paths, the annotations and the `resolved` pipeline
(`resolved_hash` stays), and get `truncated: true`. Plugins must
not `record()` personal data. Data that is no I-JSON (e.g., integers beyond
2^53 - 1) is recorded as `error: not_json` instead of hashes (and of the
`resolved` pipeline); building and storing records never fails the
request. A record, shortened:

```json
{
  "engine": {"foca": "0.18.0", "plugin_api": 1,
             "app": {"name": "my-gateway", "version": "1.2.0"}},
  "request_id": "4f3c...",
  "pipeline": {"source": "preset", "preset": "encrypted-inputs",
               "requested": "tags[gateway.pipeline]",
               "resolved_hash": "sha256:9a1e..."},
  "plugins": {"sha256:77b0...": "org.example.tes.data.decrypt"},
  "executions": [
    {"step": "org.example.tes.data.decrypt", "phase": "request",
     "alternative": 0, "plugin": "sha256:77b0...",
     "plugin_id": "org.example.tes.data.decrypt", "outcome": "ok",
     "started_at": "2026-10-25T09:00:00.000Z", "duration_ms": 12.5,
     "annotations": {"volumes": 1},
     "changes": {"before": "hmac-sha256:01aa...",
                 "after": "hmac-sha256:c4d2...",
                 "paths": ["/payload/volumes"]}}
  ],
  "route": {"candidates_in": ["tes-a", "tes-b"],
            "candidates_out": ["tes-b", "tes-a"]},
  "selection": null,
  "attempts": [{"number": 1, "backend": "tes-b", "outcome": "accepted",
                "reason": null, "remote_id": "task-1",
                "started_at": "2026-10-25T09:00:00.020Z",
                "duration_ms": 80.1}],
  "final_reason": null
}
```

`foca.plugins.ProvenanceStore` stores the shared parts once, in
collections that the app owns and declares in its `db` config:

```python
store = ProvenanceStore(
    db.plugin_builds, db.pipelines, db.failed_runs  # names of the app
)
task["provenance"] = store.save(run)  # default/preset pipelines by hash
...
except GatewayError as error:
    store.save_failure(error.run, error)  # only if `record_failed` is on
```

- `plugin_builds` and `pipelines` are keyed by hash (`_id`) and need no
  index; only default and preset pipelines (admin-defined) are stored
  there, client pipelines stay inline and are deleted with the task. If
  writing them fails, the record keeps the pipeline and the build
  documents (`builds`) inline, with `stored: false`.
- Writes are synchronous: their timeouts are those of the app's MongoDB
  client (e.g., `serverSelectionTimeoutMS`, `socketTimeoutMS` and
  `w`/`wtimeoutMS` in the connection string, cf. `MONGO_URI`), and add
  to the time of the request.
- Failed runs (e.g., rejected requests, which have no task) are stored
  only with `plugins.provenance.record_failed: true`; give the collection
  a TTL index (`foca.plugins.provenance.FAILURE_INDEXES`, 30 days by
  default) for the retention you need. Failure records hold the same data
  as other records, plus the time and the error type; their `_id` is the
  request ID (else `run-` and the run's random ID), so storing a record
  again replaces it.
- The image digest of the app (env var `FOCA_IMAGE_DIGEST`, e.g., set by a
  Helm chart) is stored with the builds, if set.

## Problem types

Gateway errors are problem details with the extension member
`problem_type`:

| Problem type (`#…`) | Status | When |
|---|---|---|
| `rejected` | 400, 403, 409, 413, 422 | a plugin rejected the request |
| `plugin-step-failed` | 500 | all alternatives of a required step failed |
| `invalid-pipeline` | 400 | the pipeline tag is invalid or not allowed |
| `invalid-backends` | 400 | the backends tag is invalid or not allowed |
| `reserved-tag` | 400 | the client set another tag under the reserved prefix, or the tags are no map |
| `no-backend-available` | 503 | no backend accepted the request |
| `forward-timeout` | 503 | the forwarding attempts took longer than `forward_timeout` |
| `invalid-via` | 400 | the `Via` header would exceed 1024 characters with the gateway's entry |
| `loop-detected` | 508 | the request passed this gateway already |
| `gateway-error` | 500 | other errors of the gateway |
