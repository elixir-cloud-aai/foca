# `server`

Basic settings of the Flask/Connexion app server.

```yaml
server:
  host: 127.0.0.1
  port: 8080
  debug: true
  testing: false
```

This config would create an application server at `127.0.0.1:8080` with
Flask's debug mode switched on.

!!! warning "Debug mode"
    Debug mode includes an interactive debugger that allows executing
    arbitrary code. It is off by default and should only be enabled for local
    development; FOCA logs a warning if it is enabled on a host other than a
    loopback address (e.g., `0.0.0.0`).

::: foca.models.config.ServerConfig
    options:
      members: false
