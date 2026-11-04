{{/*
HorizontalPodAutoscaler of the app, if `<valuesKey>.autoscaling.enabled`,
between `minReplicas` (2 if not set) and `maxReplicas` (4 if not set), on CPU
and memory only: `targetCPUUtilizationPercentage` and
`targetMemoryUtilizationPercentage`, or CPU at 80% if neither is set. Call with
the context dict.
*/}}
{{- define "foca-common.hpa" -}}
{{- include "foca-common.checkSettings" . -}}
{{- if eq (include "foca-common.autoscalingEnabled" .) "true" -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $autoscaling := (index $root.Values $key).autoscaling -}}
{{- $min := include "foca-common.minReplicas" . | int -}}
{{- $max := $autoscaling.maxReplicas -}}
{{- if kindIs "invalid" $max -}}
{{- $max = 4 -}}
{{- end -}}
{{- $cpu := $autoscaling.targetCPUUtilizationPercentage -}}
{{- $memory := $autoscaling.targetMemoryUtilizationPercentage -}}
{{- if and (kindIs "invalid" $cpu) (kindIs "invalid" $memory) -}}
{{- $cpu = 80 -}}
{{- end -}}
{{- $check := dict "key" (printf "%s.autoscaling.maxReplicas" $key) "value" $max -}}
{{- $max = include "foca-common.checkPositive" $check | int -}}
{{- if lt $max $min -}}
{{- fail (printf "%s.autoscaling.maxReplicas must not be less than minReplicas" $key) -}}
{{- end }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "foca-common.fullname" $root | quote }}
  minReplicas: {{ $min }}
  maxReplicas: {{ $max }}
  metrics:
  {{- range $metric := list (list "cpu" $cpu "targetCPUUtilizationPercentage") (list "memory" $memory "targetMemoryUtilizationPercentage") }}
  {{- $target := index $metric 1 }}
  {{- if not (kindIs "invalid" $target) }}
  - type: Resource
    resource:
      name: {{ index $metric 0 }}
      target:
        type: Utilization
        averageUtilization: {{ include "foca-common.checkPositive" (dict "key" (printf "%s.autoscaling.%s" $key (index $metric 2)) "value" $target) }}
  {{- end }}
  {{- end }}
{{ end -}}
{{- end -}}
