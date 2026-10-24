# Migrating to 0.18.0

FOCA 0.18.0 keeps one cached Casbin enforcer per app instance instead of
loading all policy rules for each enforced request, so requests cost about
the same with many rules as with few (cf. [scaling](access-control.md#scaling)).
Most apps need no changes to run with FOCA 0.18.0; check the breaking
changes below if your app uses access control and uses the enforcer
directly or changes rules in the database directly, and the other changes
if your app creates FOCA's Casbin adapter itself. Upgrading from 0.16 or
earlier? Start with [Migrating to 0.17.0](migration-0.17.md).

## Breaking changes

### Shared enforcer

`get_enforcer()` (in `foca.security.access_control.enforcement`) now
returns the enforcer shared by the threads of the app instance, kept up to
date with the stored rules, instead of a new enforcer with all rules loaded
for each call.

- If the app serves requests in several threads (e.g., gunicorn's
  `--threads`), use the new context manager `locked_enforcer()` instead, so
  that the enforcer is not updated while in use:

    ```python
    from foca.security.access_control.enforcement import locked_enforcer

    with locked_enforcer() as enforcer:
        allowed = enforcer.enforce(subject, path, method)
    ```

- Do not store the enforcer for later use or change its rules other than
  via its methods that also store them (e.g., `add_policy()`): changes made
  via the adapter are recorded and applied to the enforcers of all app
  instances, while, e.g., `enforcer.clear_policy()` would change the
  shared enforcer of one instance only. Updating rules (e.g., via
  `update_policy()`), which FOCA's adapter does not support, now raises
  `NotImplementedError` before changing the enforcer (before, it raised an
  `AttributeError`, or changed only the enforcer of the call, without
  storing the change).

### Rules changed in the database directly

App instances now notice changed rules via a record of the changes, in
collection `<collection_name>_changes` next to the policy collection (e.g.,
`policy_rules_changes`), which FOCA's adapter updates on each write. Rules
added, changed or removed in the database directly (e.g., with `mongosh`)
are therefore no longer applied from the next request. After such changes,
call `mark_changed()` on the app's adapter
(`current_app.config["casbin_adapter"]`), or restart the app; all rules are
then loaded again on the next request of each instance.

During a rolling update from 0.17, instances of 0.17 do not record their
changes, so instances of 0.18 may miss rules they change: upgrade all
instances before granting or revoking roles, or call `mark_changed()` once
the update is complete.

## Other changes

### Indexes and adapter

- FOCA's adapter creates two more indexes on the policy collection (on
  `ptype`, `v0` and `v1`, and on `ptype` and `v2`), on its first write, to
  read changed rules quickly. If creating them fails (e.g., as the database
  user may not create indexes), a warning is logged and rules are read
  without them.
- `Adapter` takes a new optional argument `changes_collection` for the
  collection recording changes (default: the policy collection's name with
  suffix `_changes`, in the same database), and has new methods:
  `mark_changed()`, `get_changes()` (the recorded changes),
  `load_filtered_rules()` (stored rules matching a filter) and
  `remove_policy_by_id()`, which the permissions API now uses to delete
  rules, so that their removal is recorded.

### Logging

Casbin no longer logs all stored rules (including user identifiers of
resource-scoped roles) at level `INFO` when the enforcer is created; only
the model is logged.
