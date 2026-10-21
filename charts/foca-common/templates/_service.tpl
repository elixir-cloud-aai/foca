{{/*
Service of the app (port 8080, `http`). Call with the context dict.
*/}}
{{- define "foca-common.service" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  ports:
  - name: http
    port: 8080
    protocol: TCP
    targetPort: http
  selector:
    {{- include "foca-common.selectorLabels" (dict "root" $root "component" "app") | nindent 4 }}
  sessionAffinity: None
  type: ClusterIP
{{ end -}}
