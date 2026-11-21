# `server`

Basic settings of the Flask/Connexion app server.

```yaml
server:
  host: 127.0.0.1
  port: 8080
  debug: true
  testing: false
  max_content_length: 1048576
  max_form_memory_size: 100000
  public_url: https://api.example.org/my-app/
```

This config would create an application server at `127.0.0.1:8080` with
Flask's debug mode switched on, rejecting request bodies larger than 1 MiB
and URL-encoded form data larger than 100 kB, and with the [public
URL](#public-url) `https://api.example.org/my-app`.

`host` and `port` only apply to Flask's development server, started with
`app.run()`. In production, run the app with a WSGI server instead (see
[running in production](../deployment.md#running-in-production)), which
binds to the address and port it is configured with.

!!! warning "Debug mode"
    Debug mode includes an interactive debugger that allows executing
    arbitrary code. It is off by default and should only be enabled for local
    development; FOCA logs a warning if it is enabled on a host other than a
    loopback address (e.g., `0.0.0.0`).

## Request body size

Request bodies may be read (e.g., for request validation) before requests are
authenticated: Connexion parses the body of every request, also of
operations without a body, before it checks the request's token. FOCA
therefore limits the size of request bodies by default, and rejects larger
bodies with `413` (problem details with the title `Content Too Large`):

| Setting | Default | Limits |
| --- | --- | --- |
| `max_content_length` | `10485760` (10 MiB) | all request bodies (Flask's `MAX_CONTENT_LENGTH`) |
| `max_form_memory_size` | `500000` (500 kB, as in Flask) | form data: whole URL-encoded bodies (`application/x-www-form-urlencoded`), and each non-file field of multipart bodies (`multipart/form-data`) (Flask's `MAX_FORM_MEMORY_SIZE`) |

Sizes are in bytes. The lower limit applies to URL-encoded bodies, which
Werkzeug parses all at once, without applying Flask's limits of form data.
Set a limit to `null` to remove it, e.g., `max_content_length: null` if the
reverse proxy or ingress in front of the app limits body sizes (e.g.,
`client_max_body_size` in NGINX), or raise it for apps that accept larger
bodies (e.g., uploads).

!!! warning "Bodies are parsed before authentication"
    Bodies up to the limits are still read and parsed before requests are
    authenticated. Without limits (`null`), anonymous clients can make the
    app read and parse bodies of any size, or URL-encoded bodies up to
    `max_content_length`, before they are rejected as unauthenticated.
    Remove a limit only if a reverse proxy or ingress limits body sizes.

Bodies are checked before requests are handled (e.g., authenticated or
validated), also for operations that don't read their body:

- Bodies with a larger declared size (`Content-Length`) are rejected without
  reading them.
- Bodies without a declared size (e.g., `Transfer-Encoding: chunked`) are
  read up to one byte more than the limit, to tell whether they are larger.
  Such bodies are kept in memory for the request, and parsing them (e.g., as
  form data) may need about as much memory again.
- Requests with an invalid declared size (not a plain decimal integer) are
  rejected with `400` without reading their body.

This check is middleware: it is the one place of FOCA that reads the raw
input of requests (`wsgi.input`) rather than [the snapshot of the current
request](../utilities.md#reading-the-current-request), and it moves to ASGI
middleware with FOCA's release on ASGI.

## Public URL

Behind a reverse proxy or ingress, the URL that clients use differs from the
one the app sees (e.g., scheme, host, port and a path prefix of the proxy).
Set `public_url` to the URL of the app as clients see it, so that the app
can build URLs for clients, e.g., for `Location` headers or links in
responses (cf. [public URLs](../utilities.md#building-public-urls)):

```yaml
server:
  public_url: https://api.example.org/my-app/
```

The URL must be an absolute `http` or `https` URL, without user info, query
or fragment, and is normalized (e.g., to
`https://api.example.org/my-app`; dot segments and repeated slashes are
resolved; cf.
[checking and normalizing URLs](../utilities.md#checking-and-normalizing-urls)).
Include the path prefix of the proxy (here, `/my-app`) only if the proxy
removes it from the requests it forwards; if the app gets it as its root
path instead (`SCRIPT_NAME`, e.g., set by the WSGI server), leave it out, as
the root path is appended to public URLs. By default, `public_url` is not
set, and building public URLs raises an error: FOCA does not build them
from the `Host` and `X-Forwarded-*` headers of requests, which clients
control unless the proxy sets them.

::: foca.models.config.ServerConfig
    options:
      members: false

## Request IDs

Each request has an identifier, for finding its log lines. It's available from
`foca.context.request_id()` and in [log records](log.md#context-of-log-records), and
sent back in a response header. It's for correlating logs only, not a trace.

```yaml
server:
  request_id:
    header: X-Request-ID
    trust_incoming: false
```

By default, each request gets a new random identifier, and what clients send in the
header isn't used as the identifier. `header` can't be a standard header such as
`Content-Length`.

Set `trust_incoming: true` only if a proxy in front of the app replaces what clients
send in the header or rejects it, and clients can't reach the app past that proxy, e.g.,
to have the proxy's identifier in the app's logs. A proxy that only checks the format
isn't enough. With the option on, the app takes the identifier from the request header
if it has at most 128 letters, digits and `._:-`, and otherwise generates one. Without
such a proxy, clients would choose what's in the response header, in log records, in
[audit events](log.md#audit-log) and in the [records of
gateways](../plugins.md#provenance), and what groups their requests in the logs. Check
what your proxy does: not all of them replace the header, and ingress-nginx, for one,
passes on a client's value.

If you change `header`, also change `access_log_format` for [Gunicorn's access
log](../deployment.md).
