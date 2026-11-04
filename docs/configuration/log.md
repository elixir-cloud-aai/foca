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

Besides what is shown here, `formatters` can have `datefmt` and `defaults` (values of
fields that a record may lack; Python's logging ignores them before Python 3.12), and
handlers can have `filters`.

## Loggers and filters

`loggers` sets the levels, handlers and filters of further loggers by name, so that
apps don't need to do that in code. `filters` adds filters by name, passing further
fields to the factory given as `()`. Names of handlers and filters that don't exist
are refused when the configuration is read.

```yaml
log:
  filters:
    no_health:
      (): my_app.logging.NoHealthChecks
      paths: [/health]
  loggers:
    pymongo:
      level: 30
    my_app.audit:
      level: 20
      handlers: [console]
      propagate: false
      filters: [no_health]
```

A filter of a logger only sees the records of this very logger, not those of its
children, as in Python's `logging`; use filters of handlers to see them all.

## Context of log records

The built-in filter `context`
(`foca.utils.logging.ContextFilter`) adds these
fields to the records of all handlers without their own `filters`. To opt out, set
`filters: []` for a handler. A filter or formatter of your own named `context` or
`json` replaces the built-in one.

| Field | Value |
|-------|-------|
| `request_id` | [Identifier of the request](server.md#request-ids), or of the Celery task |
| `operation_id` | Operation of the request, as in the specification |
| `subject` | Subject of the validated token |
| `client` | Client of the validated token (claim `client_id` or `azp`) |

They are empty if not available. The default format doesn't use them; add those you
need to yours, with `defaults` if handlers without the filter use the format:

```yaml
log:
  formatters:
    standard:
      format: "[{asctime}: {levelname:<8}] {request_id} {message} [{name}]"
```

Workers of Celery set up logging themselves, so this configuration doesn't apply
there by default. With
[`worker_hijack_root_logger`](https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-hijack-root-logger)
off, it applies to the handlers of the root logger, where `request_id` is the
identifier of the task. Loggers from `get_task_logger()` keep Celery's own handler,
so use `logging.getLogger(__name__)` in tasks to have the context.

## JSON logs

With the built-in formatter `json`
(`foca.utils.logging.JsonFormatter`), each record
is written as one JSON object per line, e.g., for log aggregators:

```yaml
log:
  handlers:
    console:
      formatter: json
```

```json
{"timestamp": "2026-01-31T12:00:00.123456+00:00", "level": "INFO", "logger": "my_app", "message": "Task created.", "request_id": "4f3c", "operation_id": "my_app.createTask", "subject": "alice", "client": ""}
```

Objects also have `exc_info` and `stack_info`, if the record has them, and the fields
given via `extra`, unless they're named as the others. What can't be written as JSON
is written as text.

!!! note "Personal data"
    `subject` usually identifies a person, and `json` always writes it. Take this
    into account for the retention of and access to logs. To leave it out, write a
    filter or formatter of your own.

## What FOCA logs

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

::: foca.models.config.LogFilterConfig
    options:
      members: false

::: foca.models.config.LogLoggerConfig
    options:
      members: false
