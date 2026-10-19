# `log`

Logging configuration, passed on to Python's
[`logging.config.dictConfig()`](https://docs.python.org/3/library/logging.config.html#logging-config-dictschema).
If it cannot be applied, FOCA falls back to its defaults and logs a warning.

```yaml
log:
  version: 1
  disable_existing_loggers: false
  formatters:
    standard:
      class: logging.Formatter
      style: "{"
      format: "[{asctime}: {levelname:<8}] {message} [{name}]"
  handlers:
    console:
      class: logging.StreamHandler
      level: 20
      formatter: standard
      stream: ext://sys.stderr
  root:
    level: 10
    handlers: [console]
```

FOCA does not log secrets: it logs only the names of configuration sections,
only issuer and subject of validated tokens, and, via
[`log_traffic`](../utilities.md#logging-requests-and-responses), no response
bodies by default.

::: foca.models.config.LogConfig
    options:
      members: false

::: foca.models.config.LogFormatterConfig
    options:
      members: false

::: foca.models.config.LogHandlerConfig
    options:
      members: false

::: foca.models.config.LogRootConfig
    options:
      members: false
