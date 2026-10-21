# `server`

Basic settings of the Flask/Connexion app server.

```yaml
server:
  host: 127.0.0.1
  port: 8080
  debug: true
  testing: false
  max_content_length: 10485760
```

This config would create an application server at `127.0.0.1:8080` with
Flask's debug mode switched on, rejecting request bodies larger than 10 MiB.

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
    are read up to one byte more than the limit (and kept in memory for the
    request) to tell whether they are larger.

::: foca.models.config.ServerConfig
    options:
      members: false
