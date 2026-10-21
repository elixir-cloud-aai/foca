{{/*
Config overlay merged by FOCA into the base config in the image (via
`FOCA_CONFIG_OVERLAY`); values in `config` take precedence over the generated
values: those of the app's settings (`config`, e.g., the public URL from
`hostName`) and the host of the release's MongoDB Service, if it is
deployed. Generated or read sections (`server`) that are `null` or no map in
`config` are like no such section (FOCA would replace the base config's
section with them, and refuse it). Renders a YAML mapping; call with the
context dict.
*/}}
{{- define "foca-common.configOverlay" -}}
{{- $config := deepCopy (.root.Values.config | default dict) -}}
{{- $generated := deepCopy (.settings.config | default dict) -}}
{{- if include "foca-common.mongodbEnabled" .root -}}
{{- $generated = mergeOverwrite $generated (dict "db" (dict "host" (include "foca-common.mongodbFullname" .root))) -}}
{{- end -}}
{{- range $section := concat (keys $generated) (list "server") | uniq -}}
{{- if and (hasKey $config $section) (not (kindIs "map" (get $config $section))) -}}
{{- $_ := unset $config $section -}}
{{- end -}}
{{- end -}}
{{- mergeOverwrite $generated $config | toYaml -}}
{{- end -}}

{{/*
ConfigMap with the config overlay (`overlay.yaml`), mounted by the app's
Deployment, whose pods are rolled when it changes; also checks the values
and the release (`foca-common.validate`). Call with the context dict.
*/}}
{{- define "foca-common.configMap" -}}
{{- include "foca-common.validate" . -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ printf "%s-config" (include "foca-common.fullname" .root) | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" .root "component" "app") | nindent 4 }}
data:
  overlay.yaml: |
    {{- include "foca-common.configOverlay" . | nindent 4 }}
{{ end -}}
