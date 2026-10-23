{{/*
OpenShift Route for `hostName` (all paths), unless `<valuesKey>.route.enabled`
is false, without an Ingress or HTTPRoute, if the cluster serves Routes (see
`foca-common.routeEnabled`); TLS is terminated at the router, which
redirects HTTP to HTTPS. Annotations from `<valuesKey>.route.annotations`
(e.g., the router's rate limits). Call with the context dict.
*/}}
{{- define "foca-common.route" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.routeEnabled" . -}}
{{- $hostName := required "hostName must be set if an OpenShift Route is created" $root.Values.hostName -}}
{{- $route := (index $root.Values .settings.valuesKey).route | default dict }}
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  {{- with $route.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  host: {{ $hostName | quote }}
  port:
    targetPort: http
  tls:
    insecureEdgeTerminationPolicy: Redirect
    termination: edge
  to:
    kind: Service
    name: {{ include "foca-common.fullname" $root | quote }}
    weight: 100
  wildcardPolicy: None
{{ end -}}
{{- end -}}
