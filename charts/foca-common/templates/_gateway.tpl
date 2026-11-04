{{- /*
Options for gateway apps built on FOCA's plugin engine, turned on by the app's
settings (see `foca-common.checkSettings`): plugin settings and backend lists
from camelCase values, and render-time checks of the backends and of the
security settings.
*/ -}}

{{/*
The keys of the settings of FOCA's plugins (`foca.plugins.config`) in the values
(camelCase) and in the config (snake_case), as a JSON object.
*/}}
{{- define "foca-common.pluginKeys" -}}
{{- toJson (dict
  "allowHttp" "allow_http"
  "allowPrivateAddresses" "allow_private_addresses"
  "clientBackends" "client_backends"
  "clientPipelines" "client_pipelines"
  "clientSelectable" "client_selectable"
  "forwardTimeout" "forward_timeout"
  "gatewayId" "gateway_id"
  "ifMissingPhase" "if_missing_phase"
  "keySecret" "key_secret"
  "maxAlternatives" "max_alternatives"
  "maxEntrySize" "max_entry_size"
  "maxItems" "max_items"
  "maxResponseSize" "max_response_size"
  "maxRouteEntries" "max_route_entries"
  "maxSize" "max_size"
  "maxSteps" "max_steps"
  "maxTtl" "max_ttl"
  "onFailure" "on_failure"
  "pipelineTimeout" "pipeline_timeout"
  "recordFailed" "record_failed"
  "secretHeaders" "secret_headers"
) -}}
{{- end -}}

{{/*
A value of the plugin settings (`value`) with the keys of the config (cf.
`foca-common.pluginKeys`), recursively; the settings of plugins (`config` and
`params`) are kept as they are. Rendered as a JSON object, with the value as
`value`.
*/}}
{{- define "foca-common.snakeKeys" -}}
{{- $keys := include "foca-common.pluginKeys" . | fromJson -}}
{{- $value := .value -}}
{{- if kindIs "map" $value -}}
{{- $converted := dict -}}
{{- range $key, $item := $value -}}
{{- if has $key (list "config" "params") -}}
{{- $_ := set $converted $key $item -}}
{{- else -}}
{{- $_ := set $converted (get $keys $key | default $key) (get (include "foca-common.snakeKeys" (dict "value" $item) | fromJson) "value") -}}
{{- end -}}
{{- end -}}
{{- $value = $converted -}}
{{- else if kindIs "slice" $value -}}
{{- $converted := list -}}
{{- range $item := $value -}}
{{- $converted = append $converted (get (include "foca-common.snakeKeys" (dict "value" $item) | fromJson) "value") -}}
{{- end -}}
{{- $value = $converted -}}
{{- end -}}
{{- toJson (dict "value" $value) -}}
{{- end -}}

{{/*
The settings of the values `plugins` that are set (not empty), with the keys of
the config (cf. `foca-common.snakeKeys`), as a JSON object. The identifiers of
plugins (`available`) and the names of presets (`presets`) are kept as they are.
Call with the root context.
*/}}
{{- define "foca-common.pluginsConfig" -}}
{{- $plugins := dict -}}
{{- $keys := include "foca-common.pluginKeys" . | fromJson -}}
{{- range $key, $value := .Values.plugins | default dict -}}
{{- if $value -}}
{{- $converted := dict -}}
{{- if has $key (list "available" "presets") -}}
{{- range $name, $item := $value -}}
{{- $_ := set $converted $name (get (include "foca-common.snakeKeys" (dict "value" $item) | fromJson) "value") -}}
{{- end -}}
{{- else -}}
{{- $converted = get (include "foca-common.snakeKeys" (dict "value" $value) | fromJson) "value" -}}
{{- end -}}
{{- $_ := set $plugins (get $keys $key | default $key) $converted -}}
{{- end -}}
{{- end -}}
{{- toJson $plugins -}}
{{- end -}}

{{/*
Fails on plugin settings set both in the values `plugins` and in `config.plugins`
(which would replace them); renders nothing. Call with the root context.
*/}}
{{- define "foca-common.checkPluginValues" -}}
{{- $configPlugins := get (.Values.config | default dict) "plugins" -}}
{{- if kindIs "map" $configPlugins -}}
{{- $keys := include "foca-common.pluginKeys" . | fromJson -}}
{{- range $key, $value := .Values.plugins | default dict -}}
{{- $name := get $keys $key | default $key -}}
{{- if and $value (hasKey $configPlugins $name) -}}
{{- fail (printf "set the plugin setting %s either in plugins.%s or in config.plugins.%s, not in both" $name $key $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Public URL of the app: `publicUrl`, or else from `hostName`, with HTTPS, as TLS is
usually terminated by the ingress, the route or a proxy in front of the cluster;
empty without both. Call with the root context.
*/}}
{{- define "foca-common.publicUrl" -}}
{{- if .Values.publicUrl -}}
{{- .Values.publicUrl -}}
{{- else if .Values.hostName -}}
{{- printf "https://%s/" .Values.hostName -}}
{{- end -}}
{{- end -}}

{{/*
The backends of `<backendsKey>.backends`, with the keys of the entries and of their
`basic` in snake_case, as the config has them, also those that only the app adds to
FOCA's entries (those of `labels` and `location` are kept), as a JSON list. Call with
a dict of the root context (`root`) and the key (`key`).
*/}}
{{- define "foca-common.backendsConfig" -}}
{{- $entries := list -}}
{{- range $entry := get (index .root.Values .key | default dict) "backends" | default list -}}
{{- $converted := dict -}}
{{- range $key, $value := $entry -}}
{{- if and (eq $key "basic") (kindIs "map" $value) -}}
{{- $basic := dict -}}
{{- range $name, $env := $value -}}
{{- $_ := set $basic (snakecase $name) $env -}}
{{- end -}}
{{- $value = $basic -}}
{{- end -}}
{{- $_ := set $converted (snakecase $key) $value -}}
{{- end -}}
{{- $entries = append $entries $converted -}}
{{- end -}}
{{- toJson $entries -}}
{{- end -}}

{{/*
Fails on backends set both in `<backendsKey>.backends` and in
`config.custom.<backendsKey>.backends`, on a `config.custom.<backendsKey>` that is
no map, and on service registries without a public URL (`publicUrl`, `hostName` or
`config.server.public_url`, which takes precedence, also if `null`), which FOCA
needs so that the app never discovers itself; renders nothing. Call with a dict of
the root context (`root`) and the key (`key`).
*/}}
{{- define "foca-common.checkBackends" -}}
{{- $key := .key -}}
{{- $config := .root.Values.config | default dict -}}
{{- $custom := get $config "custom" -}}
{{- $section := and (kindIs "map" $custom) (get $custom $key) -}}
{{- $server := get $config "server" -}}
{{- $publicUrl := include "foca-common.publicUrl" .root -}}
{{- if and (kindIs "map" $server) (hasKey $server "public_url") -}}
{{- $publicUrl = get $server "public_url" -}}
{{- end -}}
{{- $backends := include "foca-common.backendsConfig" . | fromJsonArray -}}
{{- if and (kindIs "map" $custom) (hasKey $custom $key) (not (kindIs "map" $section)) -}}
{{- fail (printf "config.custom.%s must be a map (the app refuses other values; leave it out for none)" $key) -}}
{{- end -}}
{{- if kindIs "map" $section -}}
{{- if and $backends (hasKey $section "backends") -}}
{{- fail (printf "set the backends either in %s.backends or in config.custom.%s.backends, not in both (config.custom.%s.backends must not be set, also not to null, with %s.backends)" $key $key $key $key) -}}
{{- end -}}
{{- if kindIs "slice" (get $section "backends") -}}
{{- $backends = concat $backends (get $section "backends") -}}
{{- end -}}
{{- end -}}
{{- range $entry := $backends -}}
{{- if and (kindIs "map" $entry) (eq (get $entry "type") "ga4gh-service-registry") (not $publicUrl) -}}
{{- fail (printf "hostName or publicUrl (or config.server.public_url) must be set if %s.backends or config.custom.%s.backends has registries (type: ga4gh-service-registry), so that the app never discovers itself" $key $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The config generated by the options of the app's settings, as a JSON object: the
section `plugins` with `plugins: true`; with `backendsKey`, the public URL
(`server.public_url`), if known, and the backends (`custom.<backendsKey>.backends`),
if any, and `custom` as a map, so that `config.custom: null` is like no custom config
(merged into this), not a null section, which the app would refuse. Call with the
context dict.
*/}}
{{- define "foca-common.gatewayConfig" -}}
{{- $generated := dict -}}
{{- if .settings.plugins -}}
{{- with include "foca-common.pluginsConfig" .root | fromJson -}}
{{- $_ := set $generated "plugins" . -}}
{{- end -}}
{{- end -}}
{{- with .settings.backendsKey -}}
{{- $key := . -}}
{{- with include "foca-common.publicUrl" $.root -}}
{{- $_ := set $generated "server" (dict "public_url" .) -}}
{{- end -}}
{{- $custom := dict -}}
{{- with include "foca-common.backendsConfig" (dict "root" $.root "key" $key) | fromJsonArray -}}
{{- $_ := set $custom $key (dict "backends" .) -}}
{{- end -}}
{{- $_ := set $generated "custom" $custom -}}
{{- end -}}
{{- toJson $generated -}}
{{- end -}}

{{/*
A boolean of the config, parsed as pydantic parses booleans (lax mode), as the app
does: `true` or `false`, the strings `1`, `on`, `t`, `true`, `y`, `yes` and `0`,
`off`, `f`, `false`, `n`, `no` (in any case, without whitespace), and the numbers 1
and 0. Renders "true" or "false"; fails otherwise. Call with a dict of the value
(`value`) and its key (`key`).
*/}}
{{- define "foca-common.parseBool" -}}
{{- $value := .value -}}
{{- $text := lower (toString $value) -}}
{{- $isNumber := or (kindIs "float64" $value) (kindIs "int64" $value) (kindIs "int" $value) -}}
{{- if kindIs "bool" $value -}}
{{- $value -}}
{{- else if and (kindIs "string" $value) (has $text (list "1" "on" "t" "true" "y" "yes")) -}}
true
{{- else if and (kindIs "string" $value) (has $text (list "0" "off" "f" "false" "n" "no")) -}}
false
{{- else if and $isNumber (has $text (list "0" "1")) -}}
{{- eq $text "1" -}}
{{- else if kindIs "invalid" $value -}}
{{- fail (printf "%s must be true or false, not null" .key) -}}
{{- else -}}
{{- fail (printf "%s must be true or false, not %v" .key $value) -}}
{{- end -}}
{{- end -}}

{{/*
With the setting `requireAccessControl`, as such apps on startup, refuses to render
unless access control is enabled and enforced (`security.access_control.enabled` and
`enforce`), without trusted token issuers, with a group mapping but no group claim
(global or of an issuer), or without a group mapped to the role `admin` (unless
admins are explicitly not required, `security.access_control.require_admin`); renders
nothing. The audiences of issuers are checked here, too, also if each issuer has one
of its own, so that the global one is not needed. Assumes what such apps' base configs
set: `enabled`, `enforce` and `require_admin` are true unless set otherwise in the
values. Call with the root context.
*/}}
{{- define "foca-common.checkAccessControl" -}}
{{- $config := .Values.config | default dict -}}
{{- $mapping := dict -}}
{{- $requireAdmin := "true" -}}
{{- $claimGroups := "" -}}
{{- $issuers := list -}}
{{- $enabled := "true" -}}
{{- $enforce := "true" -}}
{{- $security := get $config "security" -}}
{{- if kindIs "map" $security -}}
{{- $accessControl := get $security "access_control" -}}
{{- if kindIs "map" $accessControl -}}
{{- if hasKey $accessControl "enabled" -}}
{{- $enabled = include "foca-common.parseBool" (dict
  "value" (get $accessControl "enabled")
  "key" "config.security.access_control.enabled"
) -}}
{{- end -}}
{{- if hasKey $accessControl "enforce" -}}
{{- $enforce = include "foca-common.parseBool" (dict
  "value" (get $accessControl "enforce")
  "key" "config.security.access_control.enforce"
) -}}
{{- end -}}
{{- /* Only if set, as `get` returns an empty string for missing keys */ -}}
{{- if hasKey $accessControl "group_mapping" -}}
{{- $mapping = get $accessControl "group_mapping" -}}
{{- end -}}
{{- if hasKey $accessControl "require_admin" -}}
{{- $requireAdmin = include "foca-common.parseBool" (dict
  "value" (get $accessControl "require_admin")
  "key" "config.security.access_control.require_admin"
) -}}
{{- end -}}
{{- if hasKey $accessControl "claim_groups" -}}
{{- $claimGroups = get $accessControl "claim_groups" -}}
{{- if kindIs "invalid" $claimGroups -}}
{{- $claimGroups = "" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $auth := get $security "auth" -}}
{{- if kindIs "map" $auth -}}
{{- $issuers = get $auth "issuers" | default list -}}
{{- end -}}
{{- end -}}
{{- /*
Issuers are compared with the `iss` claim as written, so surrounding whitespace is
refused, too. Issuers are URLs, or maps with the URL (`issuer`) and settings of their
own (FOCA's `IssuerConfig`): their audiences (`audience`) and group claims
(`claim_groups`) replace the global ones for their tokens, unless unset or null, and
are checked as by the app; the app validates the other settings (`namespace`,
`userinfo_claims`). As in the app, group mappings need a group claim of at least one
issuer
*/ -}}
{{- $issuersError := "config.security.auth.issuers must list the trusted token issuers (the iss claim of their tokens, e.g., https://login.example.org/oidc/, without surrounding whitespace; or maps of it, as issuer, and settings of the issuer): the app trusts none by default" -}}
{{- if not (and (kindIs "slice" $issuers) $issuers) -}}
{{- fail $issuersError -}}
{{- end -}}
{{- $settings := list "issuer" "namespace" "audience" "claim_groups" "userinfo_claims" -}}
{{- $anyClaim := false -}}
{{- range $entry := $issuers -}}
{{- $issuer := $entry -}}
{{- $claim := $claimGroups -}}
{{- if kindIs "map" $entry -}}
{{- $issuer = get $entry "issuer" -}}
{{- /* First, so that a misspelt `issuer` is named, too */ -}}
{{- range $key := keys $entry | sortAlpha -}}
{{- if not (has $key $settings) -}}
{{- fail (printf "config.security.auth.issuers: %s of issuer %v is no setting of issuers (%s)" $key $entry (join ", " $settings)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not (and (kindIs "string" $issuer) (trim $issuer) (eq $issuer (trim $issuer))) -}}
{{- fail $issuersError -}}
{{- end -}}
{{- if kindIs "map" $entry -}}
{{- /* null, as unset, means the global setting (`get` returns "" for missing keys) */ -}}
{{- $audience := get $entry "audience" -}}
{{- if and (hasKey $entry "audience") (not (kindIs "invalid" $audience)) -}}
{{- $valid := and (kindIs "slice" $audience) $audience -}}
{{- range $client := ternary $audience (list) (kindIs "slice" $audience) -}}
{{- if not (and (kindIs "string" $client) (trim $client)) -}}
{{- $valid = false -}}
{{- end -}}
{{- end -}}
{{- if not $valid -}}
{{- fail (printf "config.security.auth.issuers: audience of issuer %s must list the accepted audiences (non-blank strings), or be null for the global audience" $issuer) -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $entry "claim_groups") (not (kindIs "invalid" (get $entry "claim_groups"))) -}}
{{- $claim = get $entry "claim_groups" -}}
{{- if not (and (kindIs "string" $claim) (eq $claim (trim $claim))) -}}
{{- fail (printf "config.security.auth.issuers: claim_groups of issuer %s must be the name of a claim (a string, without surrounding whitespace; empty for no groups), or null for the global claim" $issuer) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (kindIs "string" $claim) $claim (eq $claim (trim $claim)) -}}
{{- $anyClaim = true -}}
{{- end -}}
{{- end -}}
{{- /* `security` and `auth` are maps here, as the issuers are set */ -}}
{{- $globalAudience := get (get $security "auth") "audience" -}}
{{- if and (hasKey (get $security "auth") "audience") (not (kindIs "invalid" $globalAudience)) -}}
{{- $strings := kindIs "slice" $globalAudience -}}
{{- range $client := ternary $globalAudience (list) $strings -}}
{{- if not (kindIs "string" $client) -}}
{{- $strings = false -}}
{{- end -}}
{{- end -}}
{{- if not $strings -}}
{{- fail "config.security.auth.audience must list the accepted audiences (strings, e.g., client IDs), or be null" -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "string" $claimGroups) -}}
{{- fail "config.security.access_control.claim_groups must be the name of a claim (a string)" -}}
{{- end -}}
{{- if ne $claimGroups (trim $claimGroups) -}}
{{- fail "config.security.access_control.claim_groups must be the name of a claim, without surrounding whitespace" -}}
{{- end -}}
{{- if kindIs "invalid" $mapping -}}
{{- $mapping = dict -}}
{{- else if not (kindIs "map" $mapping) -}}
{{- fail "config.security.access_control.group_mapping must be a map of groups to roles" -}}
{{- end -}}
{{- if not (and (eq $enabled "true") (eq $enforce "true")) -}}
{{- fail "config.security.access_control.enabled and enforce must be true (as by default), as this app protects its resources with access control" -}}
{{- end -}}
{{- if and $mapping (not $anyClaim) -}}
{{- fail "config.security.access_control.claim_groups must name the claim with the groups of users (e.g., groups), as groups are mapped to roles (group_mapping), unless claim_groups of each issuer in config.security.auth.issuers does (an issuer's claim replaces the global one for its tokens; an empty one takes no groups); also add it to config.security.auth.userinfo_claims if the identity provider releases it via user info only" -}}
{{- end -}}
{{- if and (eq $requireAdmin "true") (not (has "admin" (values $mapping))) -}}
{{- fail "config.security.access_control.group_mapping must map a group to the role admin (e.g., the admin group of the identity provider), or set config.security.access_control.require_admin to false" -}}
{{- end -}}
{{- end -}}
