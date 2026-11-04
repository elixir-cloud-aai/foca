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

## Audit log

Changes of access rights are logged on level `INFO` to the logger `foca.audit`, with
who made them: rules changed via the [permissions
API](../access-control.md#securing-the-permissions-api) and roles changed with the
helpers for resource roles (`add_resource_role()`, `remove_resource_role()`,
`remove_resource()`, and the operations on owners, which use them). Roles rolled back
or restored are logged, too.

| `action` | Further fields |
|----------|----------------|
| `permission.create` | `ptype`, `rule` |
| `permission.update` | `ptype`, `rule`, `previous` (its `ptype` and `rule`) |
| `permission.delete` | `ptype`, `rule`, unless the rule was stored only just before |
| `resource_role.grant` | `subject`, `subject_type`, `role`, `ptype`; for `grant_role()`, also `replacing` (the roles it replaces, as the subject held them just before) and `already_granted` |
| `resource_role.revoke` | `subject`, `subject_type`, `role`, `ptype` |
| `resource_role.revoke_all` | `rules` (with their prefixed subjects), `ptype`, `swept` (whether rules that grant no role were removed, e.g., invalid ones or stored copies of configured ones, or rules stored meanwhile), `part` and `parts` (the rules come in events of up to 50), `incomplete` (whether removing failed, so the rest may remain), `uncertain` (the rule being removed when that happened, which may be gone) |

Each event also has:

| Field | Value |
|-------|-------|
| `action` | What was done |
| `target` | What it was done to: the identifier of the rule, or the path of the resource |
| `actor` | `subject` and `client` of the validated token, and the user that a service client names in the header [`auth.on_behalf_of_header`](auth.md) (`on_behalf_of`); `null` where not available, e.g., for changes outside of requests |
| `request_id` | [Identifier of the request](server.md#request-ids) or Celery task, or `null`; with [`trust_incoming`](server.md#request-ids), it comes from the request header, so clients can choose it unless a proxy replaces it |

These are in the `audit` field of the record, so that the [formatter `json`](#json-logs)
writes them as an object. The message has them as `name=value`, with values as JSON,
so that they can't forge fields or lines in text logs:

```text
action="resource_role.grant" target="/tasks/1" actor={"subject": "alice", "client": null, "on_behalf_of": null} request_id="f3a9" subject="bob" subject_type="user" role="owner" ptype="g2"
```

To write them somewhere of their own, give the logger its own handler:

```yaml
log:
  handlers:
    console: {}
    audit:
      class: logging.StreamHandler
      level: 20
      formatter: json
      stream: ext://sys.stdout
  loggers:
    foca.audit:
      level: 20
      handlers: [audit]
      propagate: false
```

Without `propagate: false`, events also go to the handlers of the root logger. If a
handler or filter of the logger raises, the operation still succeeds, and the failure is
logged to the logger `foca.audit_failures`. Handlers of Python's `logging` don't raise:
they write failures, such as a full disk, to the standard error, and the event is lost.
In Celery workers, the level of the root logger may be
too high for `INFO`, as they [set up logging themselves](#context-of-log-records).

Apps can log events of their own, which then have the same fields:

```python
from foca import audit

audit.log("task.cancel", task_id, reason=reason)
```

Fields named `action`, `target`, `actor` or `request_id` are left out, so that values
can't replace the actual ones. Don't pass secrets as fields.

The audit log isn't a transaction log: events are written after the change, so if the
database fails right after a change (the request then fails), there may be no event,
and with concurrent changes, their order may differ from that of the changes, and a
grant may be logged whose role a concurrent `grant_role()` replaced at once. Direct
use of the Casbin enforcer and configured rules changed on startup aren't logged here.

!!! warning "Personal data"
    The actor, and the subjects of roles and rules, are personal data. Under the GDPR,
    you need a legal basis for keeping them, a retention period for the audit log, and
    access to it limited to those who need it.
