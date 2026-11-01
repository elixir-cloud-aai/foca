# Migrating to 0.20.0

FOCA 0.20.0 changes how gateway plugins key some of their stored data.
Most apps need no changes; check the sections below if your gateway app
records failed runs (`plugins.provenance.record_failed`).
Upgrading from 0.17 or earlier? Start with [Migrating to
0.17.0](migration-0.17.md).

## Other changes

### Failure records keyed by run ID

Records of failed pipeline runs (`ProvenanceStore.save_failure()`, only
with `plugins.provenance.record_failed: true`) were keyed (`_id`) by the
client-supplied `X-Request-ID`, so a client could reuse an ID to overwrite
an earlier record or to collide with the records of other clients. They
are now keyed by `run-` and the run's server-generated random ID; the
request ID stays in the record as the field `request_id`, for correlation.

Nothing needs to be changed in the app. Records written by FOCA 0.19.0
keep their old keys until they expire (cf. the TTL index of the
collection, `foca.plugins.provenance.FAILURE_INDEXES`); queries or
dashboards that look up failure records by `_id` and a request ID need to
query the field `request_id` instead.
