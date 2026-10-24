# `server`

Basic settings of the Flask/Connexion app server.

```yaml
server:
  host: 127.0.0.1
  port: 8080
  debug: true
  testing: false
  max_content_length: 10485760
  public_url: https://api.example.org/my-app/
```

This config would create an application server at `127.0.0.1:8080` with
Flask's debug mode switched on, rejecting request bodies larger than 10 MiB,
and with the [public URL](#public-url) `https://api.example.org/my-app`.

`host` and `port` only apply to Flask's development server, started with
`app.run()`. In production, run the app with a WSGI server instead (see
[running in production](../deployment.md#running-in-production)), which
binds to the address and port it is configured with.

!!! warning "Debug mode"
    Debug mode includes an interactive debugger that allows executing
    arbitrary code. It is off by default and should only be enabled for local
    development; FOCA logs a warning if it is enabled on a host other than a
    loopback address (e.g., `0.0.0.0`).

!!! warning "Request body size"
    By default, FOCA does not limit the size of request bodies. As bodies may
    be read (e.g., for request validation) before requests are authenticated,
    set `max_content_length` (in bytes; Flask's `MAX_CONTENT_LENGTH`) to
    reject larger bodies with `413`, or limit body sizes in the reverse proxy
    or ingress in front of the app (e.g., `client_max_body_size` in NGINX).
    Bodies are checked before requests are handled (e.g., authenticated or
    validated), also for operations that don't read their body: bodies with a
    larger declared size (`Content-Length`) are rejected without reading them,
    and bodies without a declared size (e.g., `Transfer-Encoding: chunked`)
    are read up to one byte more than the limit to tell whether they are
    larger. Such bodies are kept in memory for the request, and parsing them
    (e.g., as form data) may need about as much memory again. Requests with
    an invalid declared size (not a plain decimal integer) are rejected with
    `400` without reading their body.

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
