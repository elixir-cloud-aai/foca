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

## Backend lists

Gateway apps configure their backends as one ordered list of typed
entries, under a key of their own (e.g., proTES: `custom.tes.backends`),
validated by `foca.plugins.backends.BackendListConfig`:

```yaml
backends:
  - type: ga4gh-tes
    url: https://tes-a.example.org
    url_schemes: [https]           # default; dev/test setups may add http
    auth: client_credentials       # client_credentials | basic | none
    labels: {region: eu-north}
    location: {latitude: 60.17, longitude: 24.94}
  - type: ga4gh-service-registry
    id: cloud-registry
    url: https://registry.example.org/ga4gh/registry/v1
    url_schemes: [https]           # default; tests may add http
    refresh_interval: 300
    max_stale: 3600                # then no candidates until a good read
    timeout: 10
    versions: ["1.1"]              # API versions, as prefixes
    environments: [prod]           # any if empty
    auth: client_credentials       # for discovered backends: or none
    allow_private_addresses: false           # hosts of discovered backends
    registry_allow_private_addresses: false  # the registry's own host
  - type: ga4gh-tes
    url: https://tes-fallback.example.org
    auth: basic
    basic: {username_env: TES_FALLBACK_USER, password_env: TES_FALLBACK_PASSWORD}
```

Entry `id`s default to the normalized URL and must be unique; URLs must not
have user info. Apps name the key with
`GatewayApp(backends=lambda conf: conf.custom.tes.backends)`: `create_app()`
then checks the list (lists with registry entries need `server.public_url`,
so that the gateway never discovers itself), and
`foca.plugins.runtime.get_backend_list()` returns it, with the app's API and
public URL. `BackendList(entries, api="tes", public_url=...)` turns the
entries into the candidates of a request, in order:

- `expand()`: configured backends and the backends discovered in each
  registry, in the registry's order. Backends with the same URL are listed
  once, at the first position, with the settings of a configured entry if
  there is one; backends at or below the gateway's public URL (same host,
  port and path, any scheme; default ports count as the same) are left
  out. `Backend.auth` names the credentials (registry entries pass theirs to
  their backends).
- `entry_for(backend)`: the entry behind a candidate, e.g., for the `basic`
  environment variables, when the app adds credentials after the `forward`
  phase.
- `find(url)`: a backend by URL, re-reading the registries (at most every
  10 s) if no candidate has it, e.g., for status polling and cancellation in
  other processes.
- `snapshots()`: the time of the last good registry reads; for the reads
  that candidates come from, e.g., for provenance, pass a mapping:
  `expand(snapshots=reads)`.
- `describe()`: identifier, URL, source, group and labels of each
  candidate, without credentials, e.g., for the service info.

Registries are the trust anchor: only services that the registry lists as
verified (`GET /verifications`, an extension of the ELIXIR Cloud Registry)
with their current URL and API type are used, if their version and
environment are accepted, their URLs are `https` and their hosts have public
addresses only (unless `allow_private_addresses`); malformed items are
skipped and logged. Registries are read without access token and without
following redirects, at public addresses only unless
`registry_allow_private_addresses` (e.g., a registry in the same cluster).
Each process caches the backends of each registry for `refresh_interval`
seconds and keeps the last good list while a registry is unreachable or
answers invalidly (retrying after 30 s at most), for `max_stale` seconds at
most, so that revoked backends are not used for long; before the first good
read, and after `max_stale`, a registry yields no candidates.

## Default routing

FOCA's built-in route step `foca.routing.ordered` (ordered failover) is
the default pipeline, and `client_pipelines.append` adds it to clients'
pipelines without a route step (as a step that may be skipped). It keeps
the order of the backend list and shuffles the backends of each service
registry among themselves (`random.SystemRandom`), so that the registry's
own order does not decide the load; there is no round-robin state. Its
parameters:

- `labels`: labels that candidates must have (e.g.,
  `{"region": "eu-north"}`). Labels in `plugins.pipeline` don't bind
  client pipelines and presets, which replace it, and
  `client_pipelines.prepend` frames only those, not `plugins.pipeline`
  (which runs for requests without a pipeline tag): pinning that must
  always hold belongs in both (e.g., a step that rejects or filters).
  `if_missing_phase` of appended steps looks at the client's or preset's
  own steps only, so a route step in `prepend` keeps the appended
  router;
- `seed`: a seed for reproducible orders, for tests only: refused unless
  the plugin's admin setting `allow_seed` is on.

The candidates are then tried in that order: the next one only if a backend
certainly did not accept the request, none after an unknown outcome (e.g.,
a `5xx` or read timeout), until the list or `forward_timeout` ends (`503`).
Provenance records the candidates before and after routing, the registry
read times (`run.route(candidates, snapshots=backend_list.snapshots())`)
and each attempt.

Built-in plugins (identifiers starting with `foca.`, which are reserved)
need no entry in `plugins.available`; an entry sets only `enabled`,
`roles`, `client_selectable` and `config` (no pins). With `enabled: false`,
a default pipeline that still lists the router stops the app at startup.
Other route plugins (e.g., distance routing) replace it in `pipeline` or
presets.

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
- Send HTTP requests with `ctx.http` (a `foca.utils.http.GuardedClient`
  bound to the call's deadline): it connects to public addresses only,
  uses `https`, follows no redirects and limits response sizes (cf.
  `plugins.defaults.http`). Its errors are `foca.utils.http.HttpError`s
  (with a `reason`); raise or let them propagate: the alternative then
  fails.
- `check_params(params)` may refuse parameters that the admin settings
  don't allow (raise `foca.plugins.InvalidParams`; other errors are plugin
  errors, which stop the app or fail the request); it runs when pipelines
  are resolved.
- One instance serves all requests of a process, from several threads:
  keep per-request data in `ctx.state`, not on `self`.
- `setup()` may run more than once per process, so it must be idempotent.
- Identifiers of plugins outside FOCA's own repositories start with a
  prefix of their own (e.g., `org.example.`).
- Hooks are plain functions (`def`); FOCA 1.0 will also accept `async def`
  with the same arguments.

### Testing plugins

`foca.plugins.testing` runs plugins through the real engine, without app,
database or network:

```python
from foca.plugins.testing import (
    FakeHttp,
    PluginHarness,
    check_plugin_contract,
    make_inbound,
)


def test_images():
    harness = PluginHarness(
        AllowedImages, config={"registries": ["quay.io"]}, params={}
    )
    task = {"executors": [{"image": "quay.io/x"}]}
    assert harness.run_request(make_inbound(task)).payload == task
    assert harness.executions[0].outcome == "ok"


def test_contract():
    check_plugin_contract(AllowedImages, config={"registries": ["quay.io"]})
```

- `PluginHarness(PluginClass, config=..., params=..., api="tes")` builds a
  pipeline with one step of the plugin; `run_request()`, `run_route()`,
  `run_forward()` and `run_response()` run the earlier phases first and
  return the output of theirs; `executions` holds the hook calls
  (outcome, error type, annotations, changes) and `run` the whole run
  (e.g., `run.provenance()`). Other arguments of `PluginHost` pass through
  (e.g., the app's `validate`).
- Builders give valid inputs: `make_principal()`, `make_inbound()`,
  `make_backend()`, `make_outbound()` and `make_response()`.
- `ctx.http` is a `FakeHttp`: add responses (`http.add("GET", url,
  json=...)`, or `error=` one of `foca.utils.http`'s errors), pass it as
  `http=`, and check `http.calls`; requests without a response fail, and
  the deadline applies as with the real client. The harness passes it to
  `PluginHost(http_client=...)`, a factory of the `ctx.http` clients: a
  deliberate injection point for tests (e.g., apps' tests with fake
  clients), which code sets and configuration cannot.
- `check_plugin_contract(PluginClass, ...)` raises a `ContractViolation`
  that lists every problem: invalid class or metadata (cf. `apis`,
  `maturity`, `capabilities`), no title, `Config` or `Params` without a
  JSON Schema, imports of web frameworks in the plugin's module (a lint,
  not a guarantee: `import` statements and `__import__()` or
  `import_module()` with literal names, in that module only; a module
  without source is a problem too), and hook calls that fail (`error`,
  `invalid_output`, `timeout`) on the sample inputs (`inbound=`,
  `candidates=`, `response=`, `http=`), once and from 20 threads with
  one instance. Phases after `request` run without the plugin's
  `request` hook, so a rejected sample hides nothing. Rejecting and
  `NotApplicable` are fine, except rejections in the `response` phase,
  which count as errors. Calls of `ctx.record()` beyond its limits raise
  `ValueError`, which fails the call, so the check reports them (records
  larger than `MAX_RECORD_SIZE` are cut when stored, not rejected). The
  engine itself makes sure that hooks can't change `ctx.original`, that
  failed calls change nothing and that routes return candidates only
  (other backends give `invalid_output`).
- `foca.plugins.pytest_plugin` has fixtures (enable them with
  `pytest_plugins = ["foca.plugins.pytest_plugin"]` in `conftest.py`):
  `plugin_contract` is `check_plugin_contract`, for the tests of plugin
  packages; with `fake_plugin_entry_points`, apps test the real allowlist
  path: it registers plugin classes as installed entry points of
  `foca.plugins` for one test, so `plugins.available` loads them as in
  production (`register_fake_plugins(monkeypatch, ...)` does the same
  with another monkeypatch).

### Releasing plugins

- Publish the distribution (e.g., on PyPI) with its entry points; admins
  pin it in `plugins.available.<id>` (`distribution`, `version`), and
  provenance records the version, the `RECORD` digest and the image
  digest.
- Raise `api_version` only for a new major version of the plugin API;
  set `maturity = "deprecated"` (and say what replaces the plugin in its
  description) one release before removing a plugin.
- Run the contract check in the plugin's CI, against the oldest and the
  newest FOCA version it supports.

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

## Gateway tags

Gateways record where a request went in the tags of the task or run (TES
and WES string maps), under the reserved prefix (`plugins.tags.prefix`,
default `gateway.`), so that clients and dashboards see it with
`GET /tasks/{id}` (views `BASIC`, `FULL`) or `GET /runs/{id}`:

| Key | Value | Written |
|---|---|---|
| `gateway.pipeline_hash` | `sha256:` of the resolved pipeline (RFC 8785) | at creation |
| `gateway.pipeline_source` | `default`, `preset:<name>` or `client` | at creation |
| `gateway.plugins` | comma-separated `id@version` of the plugins that ran in the `request` phase, whatever their outcome (the provenance has all phases) | at creation |
| `gateway.provenance` | path of the provenance endpoint, if the app serves one | at creation |
| `gateway.backend` | base URL of the backend that accepted (or may have) | after forwarding |
| `gateway.backend_task_id` | identifier at the backend, if it accepted | after forwarding |
| `gateway.forwarded_at` | RFC 3339 UTC time of that attempt | after forwarding |
| `gateway.attempts` | number of attempts, e.g., `3` | after forwarding |
| `gateway.route` | compact JSON of the last `max_route_entries` attempts: `url`, `outcome`, `reason` | after forwarding |
| `gateway.route_truncated` | `true` if `gateway.route` omits earlier attempts or cuts the URL of the latest | after forwarding |

Values are at most 1 KiB (`gateway.route`: 4 KiB, earliest attempts dropped
first); URLs have no user info or query, and failures are reason codes,
never messages; `gateway.pipeline_hash` is left out for pipelines without
hash (no I-JSON), and `gateway.plugins` is empty if its first entry does
not fit. References that do not fit (`gateway.provenance`,
`gateway.backend_task_id`) are left out rather than cut, and lone
surrogates become `?`. FOCA provides the tags, and apps write them: they merge
`foca.plugins.tags.creation_tags(run)` into new tasks after the `request`
phase and `routing_tags(run)` after forwarding, and relay requests without
any key under the prefix (`strip_for_forwarding(tags, prefix)`), so nested
gateways write their own:

```python
from foca.plugins.tags import creation_tags, without_server_tags

prefix = host.conf.tags.prefix
task["tags"] = {
    **without_server_tags(request.payload.get("tags"), prefix),
    **creation_tags(run),
}
```

Dashboards can trust these tags: `request` hooks whose output adds,
changes or removes a key under the prefix (also the client's own
`gateway.pipeline` and `gateway.backends`) give `invalid_output`, so
plugins cannot forge them, and `without_server_tags()` drops all keys but
the client's before the merge, as a second line of defence. This applies
to payloads with a `tags` map (TES); apps of other APIs (e.g., WES, whose
tags are a form field) apply `without_server_tags()` themselves.

Clients may set only `gateway.pipeline` and `gateway.backends`; requests
with other keys under the prefix are refused (`400`). Clients that submit
a fetched task again (e.g., to re-run it) must drop the other `gateway.*`
keys first.

## Plugin listing

Clients that choose pipelines per request (e.g., SDKs, dashboards) list
what they may choose with `GET <base>/plugins` and
`GET <base>/plugins/{plugin_id}`, both authenticated. The listing holds
exactly what selection accepts from the caller: the plugins it may use in
its own pipelines (if it may send them: `client_pipelines` enabled, its
`roles`, `inline` allowed; then loaded, `client_selectable`, and allowed
for one of the caller's roles), with title, description, group and
subgroup (from identifiers `<group>.<subgroup>.<name>`), phases,
capabilities, maturity, distribution, version, third-party data transfers
and the JSON Schema of the parameters, never the admin settings; and the
presets it may choose (`presets` allowed, and the roles of their plugins).
It also holds the steps of the default pipeline, the limits of
`client_pipelines` (with whether the caller has anything to choose, and
the identifiers of the `prepend` and `append` steps) and
`client_backends`, and the tag prefix. Callers without anything to choose
get no steps, as step identifiers default to plugin identifiers. Plugins that the caller may not use give `404`
(`plugin-not-found`), whatever the reason.

Apps serve the listing by adding FOCA's OpenAPI 3 specification
(`foca.plugins.api.SPEC_PATH`) to the files of their API specification, so
that the paths get the app's base path, security and access control. List
it first, so that the app's own `info` and `servers` win (later files update
earlier ones):

```python
from pathlib import Path

from foca import Foca
from foca.plugins.api import SPEC_PATH

foca = Foca(config_file="config.yaml")
spec = foca.conf.api.specs[0]
files = spec.path if isinstance(spec.path, list) else [spec.path]
spec.path = [Path(SPEC_PATH), *files]
app = foca.create_app()
```

With access control enforced, allow the listing in the app's policies;
FOCA adds no policies of its own, e.g.:

```yaml
security:
  access_control:
    policies:
      - [p, user, "/ga4gh/tes/v1/plugins", GET]
      - [p, user, "/ga4gh/tes/v1/plugins/:plugin_id", GET]
      - [p, user, "/ga4gh/tes/v1/tasks/:id/provenance", GET]
```

These patterns need a model that matches paths with `keyMatch2` (e.g.,
FOCA's `resource_role_model.conf`), where `:plugin_id` matches one path
segment (a trailing `*`, e.g., `plugins*`, does not match
`/plugins/<id>`); with `default_model.conf`, which matches paths exactly,
list each path explicitly.

The public service info gets generic descriptors only: apps add
`foca.plugins.api.service_info_fields(plugins_url=...)` to the fields of
their `service_info.runtime_fields` function, which gives

```json
{"gateway": {"client_pipelines": true, "client_backends": false,
             "capabilities": ["routing", "tagging"],
             "plugins_url": "https://gw.example.org/ga4gh/tes/v1/plugins"}}
```

without plugin identifiers, distributions or versions, which would map the
code a gateway runs to unauthenticated callers. Before the plugin host of
the process is in use (e.g., when FOCA checks the service info on app
creation), the capabilities come from the plugin classes, so that no
plugin is set up before workers fork.

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
| `plugin-not-found` | 404 | the plugin is unknown or not available to the caller |
| `loop-detected` | 508 | the request passed this gateway already |
| `gateway-error` | 500 | other errors of the gateway |
