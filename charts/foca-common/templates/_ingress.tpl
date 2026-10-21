{{/*
Ingress for `hostName`, if `<valuesKey>.ingress.enabled`. Annotations from
`ingress.annotations` take precedence over those set by the chart: the app's
size limit of request bodies for ingress-nginx (in bytes; see
`foca-common.maxContentLength`), and those requesting a certificate from
cert-manager; `null` removes them. Call with the context dict.
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
{{- with include "foca-common.maxContentLength" . -}}
{{- $_ := set $annotations "nginx.ingress.kubernetes.io/proxy-body-size" . -}}
{{- end -}}
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
