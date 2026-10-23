{{/*
Ingress for `hostName`, if `<valuesKey>.ingress.enabled`, for any ingress
controller: the chart sets no controller-specific annotations (e.g., for
body sizes or rate limits), which `ingress.annotations` add. These take
precedence over the annotations requesting a certificate from cert-manager
(`ingress.https`); `null` removes them. Call with the context dict.
*/}}
{{- define "foca-common.ingress" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $ingress := (index $root.Values $key).ingress | default dict -}}
{{- if $ingress.enabled -}}
{{- $hostName := required (printf "hostName must be set if %s.ingress.enabled" $key) $root.Values.hostName -}}
{{- $https := $ingress.https | default dict -}}
{{- $annotations := dict -}}
{{- if $https.enabled -}}
{{- $_ := set $annotations "cert-manager.io/cluster-issuer" (required (printf "%s.ingress.https.issuer must be set if %s.ingress.https.enabled" $key $key) $https.issuer) -}}
{{- $_ := set $annotations "kubernetes.io/tls-acme" "true" -}}
{{- end -}}
{{- range $name, $value := $ingress.annotations | default dict -}}
{{- if kindIs "invalid" $value -}}
{{- $_ := unset $annotations $name -}}
{{- else -}}
{{- $_ := set $annotations $name $value -}}
{{- end -}}
{{- end }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  {{- with $annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  {{- with $ingress.className }}
  ingressClassName: {{ . | quote }}
  {{- end }}
  rules:
  - host: {{ $hostName | quote }}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {{ include "foca-common.fullname" $root | quote }}
            port:
              name: http
{{- if $https.enabled }}
  tls:
  - hosts:
    - {{ $hostName | quote }}
    secretName: {{ printf "%s-ingress-secret" (include "foca-common.fullname" $root) | quote }}
{{- end }}
{{ end -}}
{{- end -}}
