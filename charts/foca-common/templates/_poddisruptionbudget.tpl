{{/*
Number of the app's pods: `<valuesKey>.replicas`, 1 if not set. Call with
the context dict.
*/}}
{{- define "foca-common.replicas" -}}
{{- $values := index .root.Values .settings.valuesKey -}}
{{- int ($values.replicas | default 1) -}}
{{- end -}}

{{/*
PodDisruptionBudget of the app, if `<valuesKey>.podDisruptionBudget.enabled`
(true if not set) and the app has more than one pod (`<valuesKey>.replicas`):
voluntary disruptions (e.g., node drains) evict at most `maxUnavailable` pods
at once (1 if neither it nor `minAvailable` is set), or keep at least
`minAvailable`. With one pod, no budget is rendered, as it would block node
drains. Call with the context dict.
*/}}
{{- define "foca-common.podDisruptionBudget" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $budget := (index $root.Values $key).podDisruptionBudget | default dict -}}
{{- if and (dig "enabled" true $budget) (gt (int (include "foca-common.replicas" .)) 1) -}}
{{- $hasMin := not (kindIs "invalid" $budget.minAvailable) -}}
{{- $hasMax := not (kindIs "invalid" $budget.maxUnavailable) -}}
{{- if and $hasMin $hasMax -}}
{{- fail (printf "%s.podDisruptionBudget: set either minAvailable or maxUnavailable, not both" $key) -}}
{{- end }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  {{- if $hasMin }}
  minAvailable: {{ toJson $budget.minAvailable }}
  {{- else if $hasMax }}
  maxUnavailable: {{ toJson $budget.maxUnavailable }}
  {{- else }}
  maxUnavailable: 1
  {{- end }}
  selector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "app") | nindent 6 }}
{{ end -}}
{{- end -}}
