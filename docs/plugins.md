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
section](configuration/plugins.md) only; there is no runtime API.

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
| rejected | `Rejected(detail, status)` | stops at once; the client gets the status (400, 403, 409, 413 or 422) |

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

Outbound requests carry a `Via` header with the gateway's name
(`plugins.gateway_id`, by default the host of `server.public_url`), and
inbound requests whose `Via` header already names this gateway are refused
(`508 Loop Detected`). Backends at or below the gateway's own public URL are
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
- Identifiers of plugins outside FOCA's own repositories start with a
  prefix of their own (e.g., `org.example.`).
- Hooks are plain functions (`def`); FOCA 1.0 will also accept `async def`
  with the same arguments.

## Using the engine in apps

```python
from foca.plugins import PluginHost
from foca.plugins.adapter import (
    current_principal,
    inbound_from_request,
    problem,
    register_plugin_host,
)

# at startup
host = register_plugin_host(
    cnx_app, PluginHost.from_config(foca.conf, api="tes", validate=check_task)
)

# in the controller of POST /tasks, after the app's checks
run = host.start(inbound_from_request(body), current_principal())
run.request()
candidates = run.route(backends)
for attempt in run.attempts(candidates):
    ...  # send attempt.outbound; report failed(), unknown() or succeeded()
```

`foca.plugins.adapter` is the only module that uses Connexion and Flask; it
builds the plain request model and turns gateway errors (`GatewayError`)
into problem responses (`problem()`).
