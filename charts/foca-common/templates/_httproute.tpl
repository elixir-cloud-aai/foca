{{/*
Gateway API HTTPRoute for `hostName`, if `<valuesKey>.httpRoute.enabled`
(mutually exclusive with the Ingress, see `foca-common.validate`): attached
to an existing Gateway (`httpRoute.parentRefs`, e.g., its HTTPS listener,
which terminates TLS), it routes all paths to the app's Service, with the
filters of `httpRoute.filters` (e.g., an implementation's extension for body
size or rate limits). The checks of `foca-common.validate` (included by the
ConfigMap template) reject it together with an Ingress. Call with the
context dict.
*/}}
{{- define "foca-common.httpRoute" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $route := (index $root.Values $key).httpRoute | default dict -}}
{{- if $route.enabled -}}
{{- $hostName := required (printf "hostName must be set if %s.httpRoute.enabled" $key) $root.Values.hostName -}}
{{- $parentRefs := $route.parentRefs -}}
{{- if not $parentRefs -}}
{{- fail (printf "%s.httpRoute.parentRefs must list at least one Gateway (or listener) to attach the HTTPRoute to" $key) -}}
{{- end }}
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  {{- with $route.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  parentRefs:
    {{- toYaml $parentRefs | nindent 2 }}
  hostnames:
  - {{ $hostName | quote }}
  rules:
  - matches:
    - path:
        type: PathPrefix
        value: /
    {{- with $route.filters }}
    filters:
      {{- toYaml . | nindent 4 }}
    {{- end }}
    {{- /* Port numbers only (not names, as in the Ingress); see `foca-common.service` */}}
    backendRefs:
    - name: {{ include "foca-common.fullname" $root | quote }}
      port: 8080
{{ end -}}
{{- end -}}

{{/*
HTTPRoute redirecting HTTP to HTTPS (`301`) for `hostName`, if
`<valuesKey>.httpRoute.enabled` and `httpRoute.httpsRedirect.enabled`:
attached to the Gateway's HTTP listener (`httpsRedirect.parentRefs`), which
must differ from those of the app's HTTPRoute (see
`foca-common.parentRefsOverlap`); without annotations. Call with the context
dict.
*/}}
{{- define "foca-common.httpsRedirectRoute" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $route := (index $root.Values $key).httpRoute | default dict -}}
{{- $redirect := $route.httpsRedirect | default dict -}}
{{- if and $route.enabled $redirect.enabled -}}
{{- $hostName := required (printf "hostName must be set if %s.httpRoute.enabled" $key) $root.Values.hostName -}}
{{- $parentRefs := $redirect.parentRefs -}}
{{- if not $parentRefs -}}
{{- fail (printf "%s.httpRoute.httpsRedirect.parentRefs must list at least one Gateway listener for HTTP (e.g., with sectionName) to attach the redirect to" $key) -}}
{{- end -}}
{{- range $redirectRef := $parentRefs -}}
{{- range $appRef := $route.parentRefs | default list -}}
{{- if include "foca-common.parentRefsOverlap" (dict "root" $root "a" $redirectRef "b" $appRef) -}}
{{- fail (printf "%s.httpRoute.httpsRedirect.parentRefs must not attach to the listeners of %s.httpRoute.parentRefs (set sectionName in both, or port in both), or the redirect would redirect all requests to the app: %s, %s" $key $key (toJson $redirectRef) (toJson $appRef)) -}}
{{- end -}}
{{- end -}}
{{- end }}
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: {{ include "foca-common.httpsRedirectFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  parentRefs:
    {{- toYaml $parentRefs | nindent 2 }}
  hostnames:
  - {{ $hostName | quote }}
  rules:
  - filters:
    - type: RequestRedirect
      requestRedirect:
        scheme: https
        statusCode: 301
{{ end -}}
{{- end -}}

{{/*
Whether two parent references of HTTPRoutes may attach to the same listener
("true") or not (empty): they refer to the same Gateway (with the defaults
of the Gateway API for group, kind and namespace), and they neither both set
different `sectionName`s nor both different `port`s. A reference with only
a `sectionName` and one with only a `port` may select the same listener, so
they count as overlapping. Call with a dict of the root context (`root`)
and the references (`a` and `b`).
*/}}
{{- define "foca-common.parentRefsOverlap" -}}
{{- $ids := list -}}
{{- range $ref := list .a .b -}}
{{- $ids = append $ids (list ($ref.group | default "gateway.networking.k8s.io") ($ref.kind | default "Gateway") ($ref.namespace | default $.root.Release.Namespace) $ref.name) -}}
{{- end -}}
{{- if eq (toJson (index $ids 0)) (toJson (index $ids 1)) -}}
{{- $sections := and (not (kindIs "invalid" .a.sectionName)) (not (kindIs "invalid" .b.sectionName)) (ne (toString .a.sectionName) (toString .b.sectionName)) -}}
{{- $ports := and (not (kindIs "invalid" .a.port)) (not (kindIs "invalid" .b.port)) (ne (toJson .a.port) (toJson .b.port)) -}}
{{- if not (or $sections $ports) -}}
true
{{- end -}}
{{- end -}}
{{- end -}}
