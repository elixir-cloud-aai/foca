# Migrating to 0.23.0

FOCA 0.23.0 lets patches of API specifications remove keys. Apps and
deployments need to change only if:

- their [patches set values to `null`](#null-in-patches-removes-keys).

## `null` in patches removes keys

[Patches](configuration/api.md#patching-specifications) (`patches` of a
specification) are now applied as a JSON Merge Patch
([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396)): `null` removes a key, at
any depth, instead of setting its value to `null`. This lets patches, e.g.,
turn an API key scheme into an HTTP bearer scheme (`name: null`, `in: null`)
or replace a reference by a path item of their own (`$ref: null`).

Patches that set a value to `null` (e.g., `default: null` or `example: null`
in a schema) now remove it instead. Set such values in a file of `path`
instead: files of `path` keep `null` values by default. FOCA doesn't warn of
such removals; to find affected patches, look for `null` in them (e.g.,
`grep -n null patches/*.yaml`). Patches that set a whole mapping to `null`
to replace it in a later patch (e.g., a path item) keep working.

Files of `path` merged into the first one keep `null` as a value; to let them
remove keys, too, set `merge_null_removes: true` for the specification (see
[merging specification
files](configuration/api.md#merging-specification-files)).
