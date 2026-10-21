{{/*
NetworkPolicy of the app, if `<valuesKey>.networkPolicy.enabled`: only the
peers in `ingressFrom` (e.g., the namespace of the ingress controller) may
connect to the app; connections from the pod's node (the kubelet's probes)
are always allowed. Egress is not restricted, as the app connects to MongoDB
and to the token issuers (OpenID Connect discovery, JWKS). Call with the
context dict.
*/}}
{{- define "foca-common.networkPolicy" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $policy := (index $root.Values $key).networkPolicy | default dict -}}
{{- if $policy.enabled -}}
{{- $from := $policy.ingressFrom -}}
{{- /* The schema rejects an empty list, but not `null` (no list) */ -}}
{{- if not $from -}}
{{- fail (printf "%s.networkPolicy.ingressFrom must list at least one NetworkPolicy peer (no peers would allow all sources); set %s.networkPolicy.enabled to false to allow all sources" $key $key) -}}
{{- end }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "app") | nindent 6 }}
  policyTypes:
  - Ingress
  ingress:
  - from:
      {{- toYaml $from | nindent 4 }}
    ports:
    - port: 8080
      protocol: TCP
{{ end -}}
{{- end -}}
