# Migrating to 0.20.0

FOCA 0.20.0 changes how gateway plugins key some of their stored data and
lets operators key the plugin store's hashes with a secret.
Most apps need no changes; check the breaking change below if your
gateway app records failed runs (`plugins.provenance.record_failed`) and
reads these records by their `_id`, and the other change if it keeps the
plugin store in a database.
Upgrading from 0.17 or earlier? Start with [Migrating to
0.18.0](migration-0.18.md).

## Breaking changes

### Failure records keyed by run ID

Records of failed pipeline runs (`ProvenanceStore.save_failure()`, only
with `plugins.provenance.record_failed: true`) were keyed (`_id`) by the
client-supplied `X-Request-ID`, so a client could reuse an ID to overwrite
an earlier record or to collide with the records of other clients. They
are now keyed by `run-` and the run's server-generated random ID; the
request ID stays in the record as the field `request_id`, for correlation.

Nothing needs to be changed in the app itself. Code, queries or
dashboards that look up failure records by `_id` and a request ID need to
query the field `request_id` instead (add an index on it if you query
often). Records written by FOCA 0.19.0 keep their old keys until they are
deleted (e.g., by the TTL index of the collection, cf.
`foca.plugins.provenance.FAILURE_INDEXES`; without one, never), so while
they exist, lookups by request ID must cover both: `_id` (old records) and
`request_id` (all records).

## Other changes

### Secret for the keys of the plugin store

`ctx.store.key_for()` hashes parts that may be personal data (e.g., a
user's identifier) with plain SHA-256: anyone with access to the database
or a backup who knows an identifier can compute the key and find that
user's entries. The new setting `plugins.defaults.store.key_secret` (or,
if it is not set, the environment variable
`FOCA_PLUGINS_STORE_KEY_SECRET`) keys the hashes (HMAC-SHA256, prefix
`hk:` instead of `h:`). See [plugin
store](configuration/plugins.md#plugin-store) for how to set it, and how
to rotate it.

- Without the secret, nothing changes, except that gateway apps whose
  plugins keep their store entries in the database log a warning at
  startup.
- **Setting the secret makes existing entries that plugins keyed with
  `key_for()` unreachable** (e.g., per-user budgets start anew), as their
  keys change; they expire after at most `max_ttl` (default: 30 days). The
  same holds when you change or remove the secret later.
- All processes and replicas of the app must have the same secret; set it
  from a secret store (e.g., a Kubernetes Secret as environment variable),
  never in a committed configuration file. Roll all replicas at once or
  accept that, during the update, processes with the old and the new secret
  don't see each other's entries.
