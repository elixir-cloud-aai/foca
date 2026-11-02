{{/*
PodDisruptionBudget of the app, if `<valuesKey>.podDisruptionBudget.enabled`
(true if not set) and the app has more than one pod (`<valuesKey>.replicas`):
voluntary disruptions (e.g., node drains) evict at most `maxUnavailable` pods
at once (1 if neither it nor `minAvailable` is set), or keep at least
`minAvailable`. With one pod, no budget is rendered, as a budget cannot keep
a single pod available during a drain: the default one lets the drain evict
it, and one with `minAvailable` would block the drain. Values are checked
here, too, for app charts with an older schema. Call with the context dict.
*/}}
{{- define "foca-common.podDisruptionBudget" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $budget := (index $root.Values $key).podDisruptionBudget -}}
{{- if kindIs "invalid" $budget -}}
{{- $budget = dict -}}
{{- end -}}
{{- if not (kindIs "map" $budget) -}}
{{- fail (printf "%s.podDisruptionBudget must be a map" $key) -}}
{{- end -}}
{{- $enabled := $budget.enabled -}}
{{- if kindIs "invalid" $enabled -}}
{{- $enabled = true -}}
{{- end -}}
{{- if not (kindIs "bool" $enabled) -}}
{{- fail (printf "%s.podDisruptionBudget.enabled must be true or false" $key) -}}
{{- end -}}
{{- if and $enabled (gt (int (include "foca-common.replicas" .)) 1) -}}
{{- range $name := list "minAvailable" "maxUnavailable" -}}
{{- $bound := index $budget $name -}}
{{- if not (kindIs "invalid" $bound) -}}
{{- $number := or (kindIs "int" $bound) (kindIs "int64" $bound) (and (kindIs "float64" $bound) (eq $bound (floor $bound))) -}}
{{- $valid := and $number (ge (int $bound) 0) -}}
{{- if kindIs "string" $bound -}}
{{- $valid = regexMatch "^(100|[1-9]?[0-9])%$" $bound -}}
{{- end -}}
{{- if not $valid -}}
{{- fail (printf "%s.podDisruptionBudget.%s must be a whole number of at least 0 or a percentage up to 100%%" $key $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
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
  # Pods that are not ready (e.g., crash-looping after a bad upgrade) can
  # always be evicted, so that they don't block node drains
  unhealthyPodEvictionPolicy: AlwaysAllow
{{ end -}}
{{- end -}}
