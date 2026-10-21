{{/*
OpenShift Route for `hostName`, without an Ingress if the cluster serves
Routes (see `foca-common.routeEnabled`); TLS is terminated at the router.
Call with the context dict.
*/}}
{{- define "foca-common.route" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.routeEnabled" . -}}
{{- $hostName := required "hostName must be set if an OpenShift Route is created" $root.Values.hostName }}
apiVersion: route.openshift.io/v1
kind: Route
metadata:
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
