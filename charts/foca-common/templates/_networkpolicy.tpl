{{/*
NetworkPolicy of the app, if `<valuesKey>.networkPolicy.enabled`: only the
peers in `ingressFrom` (e.g., the pods of the ingress controller or Gateway;
there are no defaults, as they depend on the cluster) may connect to the
app; connections from the pod's node (the kubelet's probes) are always
allowed. Egress is not restricted, as the app connects to MongoDB and to the
token issuers (OpenID Connect discovery, JWKS). Call with the context dict.
*/}}
{{- define "foca-common.networkPolicy" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $policy := (index $root.Values $key).networkPolicy | default dict -}}
{{- if $policy.enabled -}}
{{- $from := $policy.ingressFrom -}}
{{- /* An empty list (allowed by the schema, e.g., as a default) or none (`null`) */ -}}
{{- if not $from -}}
{{- fail (printf "%s.networkPolicy.ingressFrom must list at least one NetworkPolicy peer (no peers would allow all sources), e.g., the pods of the ingress controller or Gateway (see https://foca.readthedocs.io/en/latest/helm-chart/#networkpolicy-peers); set %s.networkPolicy.enabled to false to allow all sources" $key $key) -}}
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
