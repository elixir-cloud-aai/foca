{{/*
PodDisruptionBudget of the app, if `<valuesKey>.podDisruptionBudget.enabled`
(true if not set) and the app has more than one pod (`<valuesKey>.replicas`):
voluntary disruptions (e.g., node drains) evict at most `maxUnavailable` pods
at once (1 if neither it nor `minAvailable` is set), or keep at least
`minAvailable`. With one pod, no budget is rendered: no budget keeps a single
pod available during a drain, and one with `minAvailable` would block it.
Call with the context dict.
*/}}
{{- define "foca-common.podDisruptionBudget" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $budget := (index $root.Values $key).podDisruptionBudget | default dict -}}
{{- $enabled := dig "enabled" true $budget -}}
{{- if not (kindIs "bool" $enabled) -}}
{{- fail (printf "%s.podDisruptionBudget.enabled must be true or false" $key) -}}
{{- end -}}
{{- if and $enabled (gt (int (include "foca-common.replicas" .)) 1) -}}
{{- $hasMin := not (kindIs "invalid" $budget.minAvailable) -}}
{{- $hasMax := not (kindIs "invalid" $budget.maxUnavailable) -}}
{{- /* The schema rejects this, too; checked for app charts with an older schema */ -}}
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
